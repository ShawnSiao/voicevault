from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_answer_provider import proposed
from tests import test_questions as question_fixtures
from voicevault.answer_provider import (
    FakeAnswerProvider,
    InvalidProviderOutput,
    PersonView,
    ProposedAnswer,
    ProposedCitation,
    ProviderUnavailable,
)
from voicevault.app_db import AppDatabase
from voicevault.question_service import QuestionService
from voicevault.questions import QuestionRepository


NOW = question_fixtures.NOW


class QuestionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database = AppDatabase(data_dir=Path(self.temp_dir.name))
        self.database.initialize()
        question_fixtures.QuestionRepositoryTests._insert_retrieval_fixture(
            self, "retrieval-a", status="succeeded"
        )
        self.service = QuestionService(
            self.database,
            QuestionRepository(),
            providers={"openai_compatible": FakeAnswerProvider(proposed())},
            clock=lambda: NOW,
        )

    def test_valid_answer_preserves_person_order_and_backfills_citation_metadata(self) -> None:
        with patch("voicevault.question_service.uuid.uuid4", return_value="question-a"):
            pending = self.service.create("retrieval-a")
        completed = self.service.submit(pending.run_id, proposed())

        self.assertEqual(completed.status, "succeeded")
        result = completed.result
        self.assertEqual(result["person_views"][0]["person_id"], "person-a")
        citation = result["citations"][0]
        self.assertEqual(citation["canonical_url"], "https://example.test/a")
        self.assertEqual(citation["platform"], "xueqiu")
        self.assertEqual(citation["post_id"], "post-a")
        self.assertEqual(citation["revision_id"], "revision-a")
        self.assertNotIn("local_path", repr(result))

    def test_unknown_cross_person_excerpt_and_disposition_are_citation_invalid(self) -> None:
        cases = (
            ProposedCitation("E999", "person-a", "alpha"),
            ProposedCitation("E1", "person-b", "alpha evidence"),
            ProposedCitation("E1", "person-a", "invented quote"),
        )
        for index, citation in enumerate(cases):
            with self.subTest(citation=citation):
                run = self.service.create("retrieval-a")
                candidate = ProposedAnswer(
                    combined_answer="Claim",
                    combined_citation_ids=(citation.evidence_id,),
                    consensus=(),
                    disagreements=(),
                    person_views=(PersonView("person-a", "Claim", (citation.evidence_id,), False),),
                    insufficient_person_ids=(),
                    limitations=(),
                    citations=(citation,),
                )
                invalid = self.service.submit(run.run_id, candidate)
                self.assertEqual(invalid.status, "citation_invalid")
                self.assertEqual(invalid.error["code"], "citation_invalid")

        run = self.service.create("retrieval-a")
        with self.database.transaction() as connection:
            connection.execute("UPDATE content_dispositions SET state = 'suppressed', reason = 'hidden' WHERE post_id = 'post-a'")
        invalid = self.service.submit(run.run_id, proposed())
        self.assertEqual(invalid.status, "citation_invalid")

    def test_every_selected_person_has_an_ordered_view_and_missing_person_is_insufficient(self) -> None:
        with self.database.transaction() as connection:
            connection.execute("INSERT INTO persons VALUES ('person-b', 'Bob', ?, ?)", (NOW.isoformat(), NOW.isoformat()))
            request = json.loads(connection.execute("SELECT request_json FROM retrieval_runs WHERE run_id = 'retrieval-a'").fetchone()[0])
            request["person_ids"] = ["person-a", "person-b"]
            connection.execute("UPDATE retrieval_runs SET request_json = ? WHERE run_id = 'retrieval-a'", (json.dumps(request, sort_keys=True, separators=(",", ":")),))
            connection.execute("INSERT INTO retrieval_run_persons VALUES ('retrieval-a', 'person-b', 1, NULL, 'missing', 'none')")
        run = self.service.create("retrieval-a")
        valid = ProposedAnswer(
            combined_answer="Alice has evidence; Bob does not.",
            combined_citation_ids=("E1",),
            consensus=(),
            disagreements=(),
            person_views=(
                PersonView("person-a", "Alice favors patience.", ("E1",), False),
                PersonView("person-b", "", (), True),
            ),
            insufficient_person_ids=("person-b",),
            limitations=(),
            citations=(ProposedCitation("E1", "person-a", "alpha evidence"),),
        )
        self.assertEqual(self.service.submit(run.run_id, valid).status, "succeeded")

        reordered = ProposedAnswer(
            combined_answer=valid.combined_answer,
            combined_citation_ids=valid.combined_citation_ids,
            consensus=(),
            disagreements=(),
            person_views=tuple(reversed(valid.person_views)),
            insufficient_person_ids=valid.insufficient_person_ids,
            limitations=(),
            citations=valid.citations,
        )
        second = self.service.create("retrieval-a")
        self.assertEqual(self.service.submit(second.run_id, reordered).status, "citation_invalid")

    def test_openai_provider_failure_is_sanitized_and_persisted(self) -> None:
        service = QuestionService(
            self.database,
            QuestionRepository(),
            providers={"openai_compatible": FakeAnswerProvider(error=ProviderUnavailable("private api response"))},
            clock=lambda: NOW,
        )
        run = service.create("retrieval-a", provider="openai_compatible")
        with self.assertRaises(ProviderUnavailable):
            service.execute(run.run_id)
        failed = service.get(run.run_id)
        self.assertEqual((failed.status, failed.error["code"]), ("failed", "provider_unavailable"))
        self.assertNotIn("private", json.dumps(dict(failed.error)))

    def test_runtime_malformed_provider_values_fail_as_invalid_output(self) -> None:
        class RuntimeProvider:
            def __init__(self, value) -> None:
                self.value = value

            def answer(self, bundle):
                return self.value

        for value in (None, proposed().to_mapping()):
            with self.subTest(value_type=type(value).__name__):
                service = QuestionService(
                    self.database,
                    QuestionRepository(),
                    providers={"openai_compatible": RuntimeProvider(value)},
                    clock=lambda: NOW,
                )
                run = service.create("retrieval-a", provider="openai_compatible")

                with self.assertRaises(InvalidProviderOutput):
                    service.execute(run.run_id)

                failed = service.get(run.run_id)
                self.assertEqual(
                    (failed.status, failed.error["code"]),
                    ("failed", "invalid_provider_output"),
                )


if __name__ == "__main__":
    unittest.main()
