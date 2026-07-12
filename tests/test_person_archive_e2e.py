from __future__ import annotations

import hashlib
import tempfile
import unittest
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from tests.test_collection_results import (
    ACCOUNT_ID,
    COLLECTOR_ID,
    EXECUTION_START,
    HEARTBEAT_AT,
    JOB_ID,
    NOW,
    PERSON_ID,
    REQUEST_END,
    REQUEST_START,
    SEGMENT_ID,
    ResultFixture,
)
from voicevault.answer_provider import (
    FakeAnswerProvider,
    PersonView,
    ProposedAnswer,
    ProposedCitation,
)
from voicevault.app_db import AppDatabase
from voicevault.collection_jobs import CollectionService
from voicevault.collection_submit import CollectionSubmissionService
from voicevault.coverage import UtcInterval
from voicevault.fulltext_index import LocalFullTextIndexProvider
from voicevault.index_service import IndexService
from voicevault.person_archive import PersonRepository, PlatformAccountRepository
from voicevault.question_service import QuestionService
from voicevault.questions import QuestionRepository
from voicevault.retrieval import RetrievalRepository, RetrievalRequest
from voicevault.retrieval_service import RetrievalService
from voicevault.vector_index import LocalVectorIndexProvider


PERSON_B = "66666666-6666-4666-8666-666666666666"
ACCOUNT_B = "77777777-7777-4777-8777-777777777777"
GENERATION_A = "88888888-8888-4888-8888-888888888888"
GENERATION_B = "99999999-9999-4999-8999-999999999999"


class PersonArchiveEndToEndTests(unittest.TestCase):
    def test_fixture_collection_to_multi_person_cited_answer_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            database = AppDatabase(data_dir=data_dir)
            database.initialize()

            with patch("voicevault.person_archive.uuid.uuid4", return_value=uuid.UUID(PERSON_ID)):
                person_a = PersonRepository(database).create("Alice")
            with patch("voicevault.person_archive.uuid.uuid4", return_value=uuid.UUID(ACCOUNT_ID)):
                account_a = PlatformAccountRepository(database).bind(
                    person_a.person_id,
                    platform="xueqiu",
                    external_user_id="123456",
                    archive_basis_confirmed_at=EXECUTION_START,
                )

            collection = CollectionService(
                database,
                instance_id="instance-a",
                clock=lambda: EXECUTION_START,
                handoff_ttl=timedelta(days=1),
                lease_ttl=timedelta(days=1),
            )
            with patch(
                "voicevault.collection_jobs.uuid.uuid4",
                side_effect=(uuid.UUID(JOB_ID), uuid.UUID(SEGMENT_ID)),
            ), patch("voicevault.collection_jobs.secrets.token_urlsafe", return_value="handoff-a"):
                job = collection.create_job(
                    account_a.account_id,
                    UtcInterval(REQUEST_START, REQUEST_END),
                    mode="normal",
                )
            claimed = collection.claim(job.handoffs[0].handoff_id, COLLECTOR_ID)
            with database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE collection_jobs
                    SET status = 'running', remote_action_count = 1,
                        last_heartbeat_at = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (HEARTBEAT_AT.isoformat(), HEARTBEAT_AT.isoformat(), JOB_ID),
                )
                connection.execute(
                    "UPDATE collection_segments SET status = 'running', updated_at = ? WHERE job_id = ?",
                    (HEARTBEAT_AT.isoformat(), JOB_ID),
                )
            fixture = ResultFixture(data_dir.parent, data_dir.name)
            manifest_sha256 = fixture.write()
            submitted = CollectionSubmissionService(database, clock=lambda: NOW).submit(
                claimed.job.job_id,
                collector_id=COLLECTOR_ID,
                handoff_version=1,
                manifest_sha256=manifest_sha256,
            )
            self.assertEqual(submitted.job_status, "succeeded")
            self.assertEqual(submitted.post_count, 3)

            self._insert_second_person(database)
            fulltext = LocalFullTextIndexProvider(data_dir)
            vector = LocalVectorIndexProvider(data_dir)
            index = IndexService(
                database, fulltext, vector, None, clock=lambda: NOW
            )
            with patch(
                "voicevault.index_service.uuid.uuid4", return_value=uuid.UUID(GENERATION_A)
            ):
                self.assertEqual(index.rebuild_person(PERSON_ID).status, "degraded")
            with patch(
                "voicevault.index_service.uuid.uuid4", return_value=uuid.UUID(GENERATION_B)
            ):
                self.assertEqual(index.rebuild_person(PERSON_B).status, "degraded")

            retrieval = RetrievalService(
                database,
                RetrievalRepository(),
                fulltext,
                vector,
                None,
                clock=lambda: NOW,
            )
            pending = retrieval.create_run(
                RetrievalRequest(
                    "voice",
                    (PERSON_ID, PERSON_B),
                    limit=10,
                    min_hits_per_person=1,
                )
            )
            evidence_set = retrieval.execute(pending.run_id)
            self.assertEqual(evidence_set.status, "succeeded")
            self.assertEqual({hit.person_id for hit in evidence_set.hits}, {PERSON_ID, PERSON_B})

            questions = QuestionService(
                database,
                QuestionRepository(),
                clock=lambda: NOW,
            )
            question = questions.create(evidence_set.run_id, provider="openai_compatible")
            bundle = question.bundle
            citations = tuple(
                ProposedCitation(item.evidence_id, item.person_id, item.excerpt)
                for item in bundle.evidence
            )
            person_views = tuple(
                PersonView(
                    person.person_id,
                    f"{person.display_name} has archived evidence.",
                    tuple(
                        item.evidence_id
                        for item in bundle.evidence
                        if item.person_id == person.person_id
                    ),
                    False,
                )
                for person in bundle.persons
            )
            proposed = ProposedAnswer(
                combined_answer="Both archived people discuss voice.",
                combined_citation_ids=tuple(item.evidence_id for item in bundle.evidence),
                consensus=("Both have matching archived evidence.",),
                disagreements=(),
                person_views=person_views,
                insufficient_person_ids=(),
                limitations=("Fixture-only acceptance flow.",),
                citations=citations,
            )
            questions.providers["openai_compatible"] = FakeAnswerProvider(proposed)
            completed = questions.execute(question.run_id)

            self.assertEqual(completed.status, "succeeded")
            self.assertEqual(len(completed.result["person_views"]), 2)
            self.assertTrue(completed.result["citations"])
            self.assertTrue(
                all(item["canonical_url"] for item in completed.result["citations"])
            )

    @staticmethod
    def _insert_second_person(database: AppDatabase) -> None:
        now = NOW.isoformat()
        text = "voice second person"
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO persons VALUES (?, 'Bob', ?, ?)", (PERSON_B, now, now)
            )
            connection.execute(
                """
                INSERT INTO platform_accounts(
                    account_id, person_id, platform, external_user_id,
                    archive_basis_confirmed_at, created_at, updated_at
                ) VALUES (?, ?, 'example', 'bob', ?, ?, ?)
                """,
                (ACCOUNT_B, PERSON_B, now, now, now),
            )
            connection.execute(
                "INSERT INTO posts VALUES ('post-b', ?, 'external-b', ?, 'https://example.test/b', ?)",
                (ACCOUNT_B, now, now),
            )
            connection.execute(
                "INSERT INTO post_revisions VALUES ('revision-b', 'post-b', ?, ?, ?, NULL)",
                (digest, text, now),
            )
            connection.execute(
                "INSERT INTO content_dispositions VALUES ('post-b', 'active', NULL, ?, NULL)",
                (now,),
            )


if __name__ == "__main__":
    unittest.main()
