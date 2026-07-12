from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .answer_provider import (
    AnswerProvider,
    Disagreement,
    InvalidProviderOutput,
    PersonView,
    ProposedAnswer,
    ProviderUnavailable,
)
from .app_db import AppDatabase
from .questions import EvidenceBundle, QuestionRepository, QuestionRun


@dataclass(frozen=True)
class ValidatedCitation:
    evidence_id: str
    person_id: str
    excerpt: str
    account_id: str
    platform: str
    post_id: str
    revision_id: str
    chunk_id: str
    canonical_url: str | None
    published_at: str | None
    captured_at: str
    observation_status: str | None
    observed_at: str | None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "person_id": self.person_id,
            "excerpt": self.excerpt,
            "account_id": self.account_id,
            "platform": self.platform,
            "post_id": self.post_id,
            "revision_id": self.revision_id,
            "chunk_id": self.chunk_id,
            "canonical_url": self.canonical_url,
            "published_at": self.published_at,
            "captured_at": self.captured_at,
            "observation_status": self.observation_status,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class AnswerResult:
    combined_answer: str
    consensus: tuple[str, ...]
    disagreements: tuple[Disagreement, ...]
    person_views: tuple[PersonView, ...]
    insufficient_person_ids: tuple[str, ...]
    limitations: tuple[str, ...]
    citations: tuple[ValidatedCitation, ...]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "combined_answer": self.combined_answer,
            "consensus": list(self.consensus),
            "disagreements": [item.to_mapping() for item in self.disagreements],
            "person_views": [item.to_mapping() for item in self.person_views],
            "insufficient_person_ids": list(self.insufficient_person_ids),
            "limitations": list(self.limitations),
            "citations": [item.to_mapping() for item in self.citations],
        }


class QuestionService:
    def __init__(
        self,
        database: AppDatabase,
        repository: QuestionRepository,
        *,
        providers: Mapping[str, AnswerProvider] | None = None,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(database, AppDatabase):
            raise TypeError("Question database must be an AppDatabase.")
        if not isinstance(repository, QuestionRepository):
            raise TypeError("Question repository is invalid.")
        if not callable(clock):
            raise TypeError("Question clock must be callable.")
        self.database = database
        self.repository = repository
        self.providers = dict(providers or {})
        self.clock = clock

    def create(self, retrieval_run_id: str, *, provider: str = "codex_task") -> QuestionRun:
        run_id = str(uuid.uuid4())
        with self.database.transaction(immediate=True) as connection:
            return self.repository.create(
                connection,
                run_id,
                retrieval_run_id,
                provider=provider,
                created_at=self._now(),
            )

    def get(self, run_id: str) -> QuestionRun:
        with self.database.connect() as connection:
            return self.repository.get(connection, run_id)

    def get_bundle(self, run_id: str) -> EvidenceBundle:
        return self.get(run_id).bundle

    def fail_incomplete(self, run_id: str, code: str) -> QuestionRun:
        if not isinstance(code, str) or not code.strip():
            raise ValueError("Question failure code is required.")
        with self.database.transaction(immediate=True) as connection:
            return self.repository.fail(
                connection,
                run_id,
                {"code": code.strip()},
                completed_at=self._now(),
            )

    def submit(self, run_id: str, proposed: ProposedAnswer) -> QuestionRun:
        if not isinstance(proposed, ProposedAnswer):
            raise TypeError("Proposed answer is invalid.")
        with self.database.transaction(immediate=True) as connection:
            run = self.repository.get(connection, run_id)
            issues, result = self._validate(connection, run, proposed)
            if issues:
                return self.repository.invalidate(
                    connection,
                    run_id,
                    proposed.to_mapping(),
                    {"code": "citation_invalid", "issues": sorted(set(issues))},
                    completed_at=self._now(),
                )
            return self.repository.succeed(
                connection,
                run_id,
                proposed.to_mapping(),
                result.to_mapping(),
                completed_at=self._now(),
            )

    def execute(self, run_id: str) -> QuestionRun:
        with self.database.transaction(immediate=True) as connection:
            running = self.repository.mark_running(
                connection, run_id, started_at=self._now()
            )
        provider = self.providers.get(running.provider)
        if provider is None:
            error: Exception = ProviderUnavailable()
        else:
            try:
                proposed = provider.answer(running.bundle)
                if not isinstance(proposed, ProposedAnswer):
                    raise InvalidProviderOutput()
                try:
                    proposed = ProposedAnswer.from_mapping(proposed.to_mapping())
                except InvalidProviderOutput:
                    raise
                except Exception:
                    raise InvalidProviderOutput() from None
            except (ProviderUnavailable, InvalidProviderOutput) as exc:
                error = exc
            except Exception:
                error = ProviderUnavailable()
            else:
                return self.submit(run_id, proposed)
        code = getattr(error, "code", "provider_unavailable")
        with self.database.transaction(immediate=True) as connection:
            self.repository.fail(
                connection,
                run_id,
                {"code": code},
                completed_at=self._now(),
            )
        if code == "invalid_provider_output":
            raise InvalidProviderOutput() from None
        raise ProviderUnavailable() from None

    def reconcile_incomplete(self) -> int:
        with self.database.transaction(immediate=True) as connection:
            run_ids = tuple(
                row["run_id"]
                for row in connection.execute(
                    "SELECT run_id FROM question_runs WHERE status = 'running' ORDER BY created_at, run_id"
                )
            )
            for run_id in run_ids:
                self.repository.interrupt(
                    connection,
                    run_id,
                    {"code": "service_restarted"},
                    completed_at=self._now(),
                )
            return len(run_ids)

    @staticmethod
    def _validate(
        connection: sqlite3.Connection,
        run: QuestionRun,
        proposed: ProposedAnswer,
    ) -> tuple[list[str], AnswerResult]:
        issues: list[str] = []
        expected_people = tuple(item.person_id for item in run.bundle.persons)
        actual_people = tuple(item.person_id for item in proposed.person_views)
        if actual_people != expected_people:
            issues.append("person_views_out_of_order")
        if any(person_id not in expected_people for person_id in proposed.insufficient_person_ids):
            issues.append("unknown_insufficient_person")
        if len(set(proposed.insufficient_person_ids)) != len(proposed.insufficient_person_ids):
            issues.append("duplicate_insufficient_person")

        evidence_by_id = {item.evidence_id: item for item in run.bundle.evidence}
        citations_by_id: dict[str, Any] = {}
        for citation in proposed.citations:
            if citation.evidence_id in citations_by_id:
                issues.append("duplicate_citation")
            citations_by_id[citation.evidence_id] = citation
            evidence = evidence_by_id.get(citation.evidence_id)
            if evidence is None:
                issues.append("unknown_evidence")
            elif evidence.person_id != citation.person_id:
                issues.append("cross_person_citation")

        referenced = set(proposed.combined_citation_ids)
        for disagreement in proposed.disagreements:
            if any(person_id not in expected_people for person_id in disagreement.person_ids):
                issues.append("unknown_disagreement_person")
            referenced.update(disagreement.citation_ids)
        for view in proposed.person_views:
            referenced.update(view.citation_ids)
            if view.insufficient:
                if view.view.strip() or view.citation_ids or view.person_id not in proposed.insufficient_person_ids:
                    issues.append("invalid_insufficient_view")
            else:
                if not view.view.strip() or not view.citation_ids or view.person_id in proposed.insufficient_person_ids:
                    issues.append("unsupported_person_view")
            for evidence_id in view.citation_ids:
                citation = citations_by_id.get(evidence_id)
                if citation is None:
                    issues.append("unknown_view_citation")
                elif citation.person_id != view.person_id:
                    issues.append("cross_person_view_citation")
        if referenced - set(citations_by_id):
            issues.append("unknown_citation_reference")

        person_by_id = {item.person_id: item for item in run.bundle.persons}
        for person_id, person in person_by_id.items():
            matching = next((item for item in proposed.person_views if item.person_id == person_id), None)
            if matching is None:
                continue
            if not person.has_evidence and not matching.insufficient:
                issues.append("missing_evidence_inference")

        validated: list[ValidatedCitation] = []
        for citation in proposed.citations:
            evidence = evidence_by_id.get(citation.evidence_id)
            if evidence is None:
                continue
            row = connection.execute(
                """
                SELECT qe.*, p.canonical_url AS current_url, p.published_at AS current_published_at,
                       revision.content_text, disposition.state AS current_disposition,
                       observation.status AS current_observation_status,
                       observation.observed_at AS current_observed_at
                FROM question_evidence qe
                JOIN posts p ON p.post_id = qe.post_id
                JOIN post_revisions revision ON revision.revision_id = qe.revision_id
                JOIN knowledge_chunks chunk
                  ON chunk.chunk_id = qe.chunk_id AND chunk.revision_id = qe.revision_id
                JOIN platform_accounts account
                  ON account.account_id = qe.account_id
                 AND account.person_id = qe.person_id
                 AND account.platform = qe.platform
                JOIN content_dispositions disposition ON disposition.post_id = qe.post_id
                LEFT JOIN post_observations observation
                  ON observation.observation_id = (
                      SELECT latest.observation_id FROM post_observations latest
                      WHERE latest.post_id = qe.post_id
                      ORDER BY latest.observed_at DESC, latest.observation_id DESC LIMIT 1
                  )
                WHERE qe.run_id = ? AND qe.evidence_id = ?
                """,
                (run.run_id, citation.evidence_id),
            ).fetchone()
            if row is None:
                issues.append("source_lineage_invalid")
                continue
            if row["current_disposition"] != "active":
                issues.append("content_not_active")
            if citation.excerpt not in evidence.excerpt or citation.excerpt not in row["content_text"]:
                issues.append("excerpt_mismatch")
            validated.append(
                ValidatedCitation(
                    evidence_id=citation.evidence_id,
                    person_id=citation.person_id,
                    excerpt=citation.excerpt,
                    account_id=row["account_id"],
                    platform=row["platform"],
                    post_id=row["post_id"],
                    revision_id=row["revision_id"],
                    chunk_id=row["chunk_id"],
                    canonical_url=row["current_url"],
                    published_at=row["current_published_at"],
                    captured_at=row["captured_at"],
                    observation_status=row["current_observation_status"],
                    observed_at=row["current_observed_at"],
                )
            )
        return issues, AnswerResult(
            combined_answer=proposed.combined_answer,
            consensus=proposed.consensus,
            disagreements=proposed.disagreements,
            person_views=proposed.person_views,
            insufficient_person_ids=proposed.insufficient_person_ids,
            limitations=proposed.limitations,
            citations=tuple(validated),
        )

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Question clock must return an aware datetime.")
        return value.astimezone(timezone.utc)
