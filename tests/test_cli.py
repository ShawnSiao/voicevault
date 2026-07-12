from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from voicevault.cli import build_parser, main
from voicevault.app_db import AppDatabase
from voicevault.collection_jobs import CollectionService
from voicevault.coverage import page_date_range_to_utc
from voicevault.events import create_event
from voicevault.importers import load_statements_from_kb
from voicevault.index import VoiceVaultIndex
from voicevault.kb import init_kb
from voicevault.person_archive import PersonRepository, PlatformAccountRepository
from voicevault.runtime import RuntimeRecord, RuntimeRegistry
from voicevault.server import create_server
from voicevault.role_skill import distill_role_skill, write_role_skill


class CliTests(unittest.TestCase):
    def test_role_parser_accepts_skill_distillation_and_agent_ask_options(self) -> None:
        parser = build_parser()

        distill_args = parser.parse_args(
            [
                "role",
                "distill",
                "--kb",
                "E:\\knowledge-base\\voicevault",
                "--role",
                "sample-investor",
                "--limit",
                "8",
                "--json",
            ]
        )
        ask_args = parser.parse_args(
            [
                "role",
                "ask",
                "--kb",
                "E:\\knowledge-base\\voicevault",
                "--role",
                "sample-investor",
                "--query",
                "英伟达利润率怎么看",
                "--symbol",
                "NVDA",
                "--dry-run",
                "--json",
            ]
        )
        agents_args = parser.parse_args(
            [
                "role",
                "agents",
                "--kb",
                "E:\\knowledge-base\\voicevault",
                "--json",
            ]
        )
        readiness_args = parser.parse_args(
            [
                "role",
                "readiness",
                "--kb",
                "E:\\knowledge-base\\voicevault",
                "--require-live",
                "--json",
            ]
        )

        self.assertEqual(distill_args.command, "role")
        self.assertEqual(distill_args.role_command, "distill")
        self.assertEqual(distill_args.role, "sample-investor")
        self.assertEqual(distill_args.limit, 8)
        self.assertEqual(ask_args.role_command, "ask")
        self.assertEqual(ask_args.query, "英伟达利润率怎么看")
        self.assertEqual(ask_args.symbol, "NVDA")
        self.assertTrue(ask_args.dry_run)
        self.assertEqual(agents_args.role_command, "agents")
        self.assertEqual(readiness_args.role_command, "readiness")
        self.assertTrue(readiness_args.require_live)

    def test_role_distill_ask_and_agents_json_outputs_skill_prompt_and_quality_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            distill_stdout = io.StringIO()
            with contextlib.redirect_stdout(distill_stdout):
                distill_exit = main(
                    [
                        "role",
                        "distill",
                        "--kb",
                        str(kb.root),
                        "--role",
                        "sample-investor",
                        "--json",
                    ]
                )
            distill_payload = json.loads(distill_stdout.getvalue())

            ask_stdout = io.StringIO()
            with contextlib.redirect_stdout(ask_stdout):
                ask_exit = main(
                    [
                        "role",
                        "ask",
                        "--kb",
                        str(kb.root),
                        "--role",
                        "sample-investor",
                        "--query",
                        "How would the role reason about NVDA margin?",
                        "--symbol",
                        "NVDA",
                        "--dry-run",
                        "--json",
                    ]
                )
            ask_payload = json.loads(ask_stdout.getvalue())
            agents_stdout = io.StringIO()
            with contextlib.redirect_stdout(agents_stdout):
                agents_exit = main(
                    [
                        "role",
                        "agents",
                        "--kb",
                        str(kb.root),
                        "--json",
                    ]
                )
            agents_payload = json.loads(agents_stdout.getvalue())
            readiness_stdout = io.StringIO()
            with contextlib.redirect_stdout(readiness_stdout):
                readiness_exit = main(
                    [
                        "role",
                        "readiness",
                        "--kb",
                        str(kb.root),
                        "--json",
                    ]
                )
            readiness_payload = json.loads(readiness_stdout.getvalue())
            live_gate_stdout = io.StringIO()
            with contextlib.redirect_stdout(live_gate_stdout):
                live_gate_exit = main(
                    [
                        "role",
                        "readiness",
                        "--kb",
                        str(kb.root),
                        "--require-live",
                        "--json",
                    ]
                )
            live_gate_payload = json.loads(live_gate_stdout.getvalue())

            self.assertEqual(distill_exit, 0)
            self.assertEqual(distill_payload["skill"]["artifact_type"], "voicevault_role_skill")
            self.assertTrue(Path(distill_payload["skill_json"]).is_file())
            self.assertTrue(Path(distill_payload["skill_markdown"]).is_file())
            self.assertEqual(ask_exit, 0)
            self.assertEqual(ask_payload["llm"]["status"], "not_called")
            self.assertEqual(ask_payload["prompt_bundle"]["answer_type"], "role_agent_prompt")
            self.assertTrue(Path(ask_payload["role_agent_json"]).is_file())
            self.assertEqual(agents_exit, 0)
            self.assertEqual(agents_payload["summary"]["prompt_only"], 1)
            self.assertIn("quality", agents_payload)
            self.assertIn("runtime", agents_payload)
            self.assertIn("readiness", agents_payload)
            self.assertEqual(readiness_exit, 0)
            self.assertFalse(readiness_payload["live_ok"])
            self.assertEqual(readiness_payload["summary"]["roles_prompt_ready"], 1)
            self.assertEqual(live_gate_exit, 1)
            self.assertFalse(live_gate_payload["ok"])
            self.assertEqual(live_gate_payload["roles"][0]["status"], "blocked_runtime")

    def test_compare_parser_accepts_role_comparison_options(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            [
                "compare",
                "--kb",
                "E:\\knowledge-base\\voicevault",
                "--query",
                "英伟达 AI",
                "--roles",
                "auto",
                "--symbol",
                "NVDA",
                "--topic",
                "ai",
                "--limit",
                "3",
                "--evidence-limit",
                "2",
            ]
        )

        self.assertEqual(args.command, "compare")
        self.assertEqual(args.kb, "E:\\knowledge-base\\voicevault")
        self.assertEqual(args.query, "英伟达 AI")
        self.assertEqual(args.roles, "auto")
        self.assertEqual(args.symbol, "NVDA")
        self.assertEqual(args.topic, "ai")
        self.assertEqual(args.limit, 3)
        self.assertEqual(args.evidence_limit, 2)

    def test_compare_json_outputs_role_comparison_and_export_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            _add_growth_role(kb)
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "compare",
                        "--kb",
                        str(kb.root),
                        "--query",
                        "NVDA margin AI",
                        "--symbol",
                        "NVDA",
                        "--roles",
                        "auto",
                        "--limit",
                        "3",
                        "--evidence-limit",
                        "2",
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["comparison"]["comparison_type"], "local_evidence_role_comparison")
            self.assertGreaterEqual(payload["comparison"]["coverage"]["role_count"], 2)
            self.assertTrue(Path(payload["comparison_json"]).is_file())
            self.assertTrue(Path(payload["comparison_markdown"]).is_file())

    def test_comparisons_parser_accepts_review_options(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            [
                "comparisons",
                "review",
                "--kb",
                "E:\\knowledge-base\\voicevault",
                "--query",
                "英伟达 AI",
                "--status",
                "adopted",
                "--reviewer",
                "codex-product-review",
                "--notes",
                "Approved for release handoff.",
                "--json",
            ]
        )

        self.assertEqual(args.command, "comparisons")
        self.assertEqual(args.comparisons_command, "review")
        self.assertEqual(args.kb, "E:\\knowledge-base\\voicevault")
        self.assertEqual(args.query, "英伟达 AI")
        self.assertEqual(args.status, "adopted")
        self.assertEqual(args.reviewer, "codex-product-review")
        self.assertEqual(args.notes, "Approved for release handoff.")

    def test_evaluations_parser_accepts_answer_regression_options(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            [
                "evaluations",
                "answers",
                "--kb",
                "E:\\knowledge-base\\voicevault",
                "--json",
            ]
        )

        self.assertEqual(args.command, "evaluations")
        self.assertEqual(args.evaluations_command, "answers")
        self.assertEqual(args.kb, "E:\\knowledge-base\\voicevault")
        self.assertTrue(args.json)

    def test_evaluations_parser_accepts_answer_regression_batch_options(self) -> None:
        parser = build_parser()

        export_args = parser.parse_args(
            [
                "evaluations",
                "export",
                "--kb",
                "E:\\knowledge-base\\voicevault",
                "--out",
                "E:\\tmp\\answer-regression.json",
                "--json",
            ]
        )
        import_args = parser.parse_args(
            [
                "evaluations",
                "import",
                "--kb",
                "E:\\knowledge-base\\voicevault",
                "--input",
                "E:\\tmp\\answer-regression.json",
                "--yes",
                "--updated-by",
                "qa-import",
                "--json",
            ]
        )

        self.assertEqual(export_args.command, "evaluations")
        self.assertEqual(export_args.evaluations_command, "export")
        self.assertEqual(export_args.out, "E:\\tmp\\answer-regression.json")
        self.assertEqual(import_args.evaluations_command, "import")
        self.assertEqual(import_args.input, "E:\\tmp\\answer-regression.json")
        self.assertTrue(import_args.yes)
        self.assertEqual(import_args.updated_by, "qa-import")

    def test_evaluations_export_and_import_json_manage_fixed_question_batches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_kb = init_kb(Path(temp_dir) / "source-kb")
            target_kb = init_kb(Path(temp_dir) / "target-kb")
            suite_path = source_kb.content_dir / "evaluations" / "questions.json"
            suite_path.parent.mkdir(parents=True, exist_ok=True)
            suite_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "questions": [
                            {
                                "id": "nvda-margin",
                                "query": "NVDA margin",
                                "role_id": "sample-investor",
                                "symbol": "NVDA",
                                "expected_role_id": "sample-investor",
                                "source_url": "https://example.com/regression/nvda-margin",
                                "rationale": "Protects exported regression coverage.",
                                "updated_by": "qa-owner",
                                "min_evidence": 1,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            export_path = Path(temp_dir) / "answer-regression-export.json"
            target_suite_path = target_kb.content_dir / "evaluations" / "questions.json"
            before_target_suite = target_suite_path.read_text(encoding="utf-8") if target_suite_path.exists() else ""

            export_stdout = io.StringIO()
            with contextlib.redirect_stdout(export_stdout):
                export_exit = main(
                    [
                        "evaluations",
                        "export",
                        "--kb",
                        str(source_kb.root),
                        "--out",
                        str(export_path),
                        "--json",
                    ]
                )
            exported = json.loads(export_stdout.getvalue())

            dry_run_stdout = io.StringIO()
            with contextlib.redirect_stdout(dry_run_stdout):
                dry_run_exit = main(
                    [
                        "evaluations",
                        "import",
                        "--kb",
                        str(target_kb.root),
                        "--input",
                        str(export_path),
                        "--dry-run",
                        "--json",
                    ]
                )
            dry_run = json.loads(dry_run_stdout.getvalue())
            after_dry_run_suite = target_suite_path.read_text(encoding="utf-8") if target_suite_path.exists() else ""

            apply_stdout = io.StringIO()
            with contextlib.redirect_stdout(apply_stdout):
                apply_exit = main(
                    [
                        "evaluations",
                        "import",
                        "--kb",
                        str(target_kb.root),
                        "--input",
                        str(export_path),
                        "--yes",
                        "--updated-by",
                        "qa-import",
                        "--json",
                    ]
                )
            applied = json.loads(apply_stdout.getvalue())

            self.assertEqual(export_exit, 0)
            self.assertEqual(exported["question_count"], 1)
            self.assertTrue(export_path.is_file())
            self.assertEqual(dry_run_exit, 0)
            self.assertFalse(dry_run["applied"])
            self.assertEqual(dry_run["summary"]["create"], 1)
            self.assertEqual(after_dry_run_suite, before_target_suite)
            self.assertEqual(apply_exit, 0)
            self.assertTrue(applied["applied"])
            self.assertEqual(applied["summary"]["create"], 1)
            self.assertTrue(target_suite_path.is_file())

    def test_evaluations_answers_json_outputs_fixed_question_regression(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "answer",
                            "--kb",
                            str(kb.root),
                            "--query",
                            "NVDA margin",
                            "--role",
                            "sample-investor",
                            "--symbol",
                            "NVDA",
                        ]
                    ),
                    0,
                )
            suite_path = kb.content_dir / "evaluations" / "questions.json"
            suite_path.parent.mkdir(parents=True, exist_ok=True)
            suite_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "questions": [
                            {
                                "id": "nvda-margin",
                                "query": "NVDA margin",
                                "role_id": "sample-investor",
                                "symbol": "NVDA",
                                "expected_role_id": "sample-investor",
                                "min_evidence": 1,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(["evaluations", "answers", "--kb", str(kb.root), "--json"])

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["summary"]["passed"], 1)
            self.assertEqual(payload["items"][0]["status"], "pass")

    def test_comparisons_review_and_list_json_manage_review_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            _add_growth_role(kb)
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "compare",
                            "--kb",
                            str(kb.root),
                            "--query",
                            "NVDA margin AI",
                            "--symbol",
                            "NVDA",
                            "--roles",
                            "auto",
                        ]
                    ),
                    0,
                )

            review_stdout = io.StringIO()
            with contextlib.redirect_stdout(review_stdout):
                review_exit = main(
                    [
                        "comparisons",
                        "review",
                        "--kb",
                        str(kb.root),
                        "--query",
                        "NVDA margin AI",
                        "--status",
                        "adopted",
                        "--reviewer",
                        "codex-product-review",
                        "--notes",
                        "Approved for release handoff.",
                        "--json",
                    ]
                )
            reviewed = json.loads(review_stdout.getvalue())

            list_stdout = io.StringIO()
            with contextlib.redirect_stdout(list_stdout):
                list_exit = main(
                    [
                        "comparisons",
                        "list",
                        "--kb",
                        str(kb.root),
                        "--review-status",
                        "adopted",
                        "--json",
                    ]
                )
            listed = json.loads(list_stdout.getvalue())

            self.assertEqual(review_exit, 0)
            self.assertEqual(reviewed["comparison"]["review"]["status"], "adopted")
            self.assertEqual(reviewed["comparison"]["review"]["reviewer"], "codex-product-review")
            self.assertEqual(list_exit, 0)
            self.assertEqual(listed["summary"]["adopted"], 1)
            self.assertEqual(listed["comparisons"][0]["review_status"], "adopted")

    def test_route_parser_accepts_role_routing_options(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            [
                "route",
                "--kb",
                "E:\\knowledge-base\\voicevault",
                "--query",
                "英伟达 AI",
                "--symbol",
                "NVDA",
                "--topic",
                "ai",
                "--limit",
                "3",
            ]
        )

        self.assertEqual(args.command, "route")
        self.assertEqual(args.kb, "E:\\knowledge-base\\voicevault")
        self.assertEqual(args.query, "英伟达 AI")
        self.assertEqual(args.symbol, "NVDA")
        self.assertEqual(args.topic, "ai")
        self.assertEqual(args.limit, 3)

    def test_route_json_outputs_suggested_role(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "route",
                        "--kb",
                        str(kb.root),
                        "--query",
                        "NVDA margin",
                        "--symbol",
                        "NVDA",
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["suggested_role_id"], "sample-investor")
            self.assertGreaterEqual(payload["route_count"], 1)

    def test_serve_parser_accepts_local_workbench_options(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            [
                "serve",
                "--kb",
                "E:\\knowledge-base\\voicevault",
                "--root",
                "E:\\projects\\public-voice-archive",
                "--host",
                "127.0.0.1",
                "--port",
                "8765",
                "--data-dir",
                "E:\\runtime\\voicevault",
            ]
        )

        self.assertEqual(args.command, "serve")
        self.assertEqual(args.kb, "E:\\knowledge-base\\voicevault")
        self.assertEqual(args.root, "E:\\projects\\public-voice-archive")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8765)
        self.assertEqual(args.data_dir, "E:\\runtime\\voicevault")

    def test_collection_claim_parser_accepts_runtime_handoff_options(self) -> None:
        args = build_parser().parse_args(
            [
                "collection",
                "claim",
                "--handoff",
                "opaque-handoff",
                "--collector",
                "collector-a",
                "--data-dir",
                "E:\\runtime\\voicevault",
            ]
        )

        self.assertEqual(args.command, "collection")
        self.assertEqual(args.collection_command, "claim")
        self.assertEqual(args.handoff, "opaque-handoff")
        self.assertEqual(args.collector, "collector-a")

        submit = build_parser().parse_args(
            [
                "collection", "submit", "--job", "job-a", "--collector", "collector-a",
                "--handoff-version", "2", "--manifest-sha256", "a" * 64,
                "--data-dir", "E:\\runtime\\voicevault",
            ]
        )
        self.assertEqual(submit.collection_command, "submit")
        self.assertEqual(submit.handoff_version, 2)
        self.assertEqual(submit.manifest_sha256, "a" * 64)

    def test_legacy_import_cli_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            role = root / "legacy" / "content" / "roles" / "researcher"
            role.mkdir(parents=True)
            (role / "statements.csv").write_text(
                "statement_id,role_id,source_type,source_url,published_at,captured_at,title,body,symbols,topics,stance,time_horizon,confidence,notes,source_platform,source_user_id,source_author\n"
                "s1,researcher,post,https://xueqiu.com/123/1,2026-07-01T00:00:00Z,2026-07-02T00:00:00Z,One,alpha view,,,unclear,unknown,low,,xueqiu,123,Alice\n",
                encoding="utf-8",
            )
            command = [
                "legacy", "import", "--kb", str(root / "legacy"),
                "--data-dir", str(root / "runtime"),
            ]
            outputs = []
            for _ in range(2):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    self.assertEqual(main(command), 0)
                outputs.append(json.loads(stdout.getvalue()))

        self.assertEqual(outputs[0]["imported"]["posts"], 1)
        self.assertEqual(outputs[1]["imported"]["posts"], 0)
        self.assertEqual(outputs[1]["existing"]["posts"], 1)

    def test_question_parser_accepts_evidence_and_submit_runtime_options(self) -> None:
        evidence = build_parser().parse_args(
            ["question", "evidence", "--run", "question-a", "--data-dir", "E:\\runtime\\voicevault"]
        )
        submit = build_parser().parse_args(
            ["question", "submit", "--run", "question-a", "--result", "answer.json"]
        )
        self.assertEqual((evidence.command, evidence.question_command), ("question", "evidence"))
        self.assertEqual(evidence.run, "question-a")
        self.assertEqual((submit.question_command, submit.result), ("submit", "answer.json"))

    def test_question_evidence_and_submit_use_quoted_local_resource_api(self) -> None:
        runtime = RuntimeRecord(
            1, "instance-a", "http://127.0.0.1:43210", 123, "2026-07-11T08:00:00Z"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            result_path = Path(temp_dir) / "answer.json"
            result_path.write_text(json.dumps({"combined_answer": "answer"}), encoding="utf-8")
            stdout = io.StringIO()
            with patch("voicevault.cli.RuntimeRegistry.discover", return_value=runtime), patch(
                "voicevault.cli._get_local_json", return_value={"bundle": {"evidence": []}}
            ) as get, patch(
                "voicevault.cli._post_local_json", return_value={"run": {"status": "succeeded"}}
            ) as post, contextlib.redirect_stdout(stdout):
                evidence_exit = main(["question", "evidence", "--run", "opaque/part?#fragment"])
                submit_exit = main(
                    ["question", "submit", "--run", "opaque/part?#fragment", "--result", str(result_path)]
                )

        self.assertEqual((evidence_exit, submit_exit), (0, 0))
        self.assertEqual(
            get.call_args.args[0],
            "http://127.0.0.1:43210/api/question-runs/opaque%2Fpart%3F%23fragment/evidence",
        )
        self.assertEqual(
            post.call_args.args,
            (
                "http://127.0.0.1:43210/api/question-runs/opaque%2Fpart%3F%23fragment/answer",
                {"combined_answer": "answer"},
            ),
        )

    def test_collection_claim_discovers_runtime_claims_once_and_returns_stable_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "runtime"
            database = AppDatabase(data_dir=data_dir)
            database.initialize()
            person = PersonRepository(database).create("Alice")
            account = PlatformAccountRepository(database).bind(
                person.person_id,
                platform="xueqiu",
                external_user_id="12345",
                archive_basis_confirmed_at="2026-07-11T00:00:00Z",
            )
            service = CollectionService(
                database,
                instance_id="instance-a",
                clock=lambda: datetime.now(timezone.utc),
                handoff_ttl=timedelta(minutes=5),
                lease_ttl=timedelta(minutes=2),
            )
            job = service.create_job(
                account.account_id, page_date_range_to_utc("2026-07-01", "2026-07-02"), mode="normal"
            )
            kb = init_kb(root / "kb")
            server = create_server(
                kb,
                port=0,
                app_database=database,
                collection_service=service,
                runtime_registry=RuntimeRegistry(data_dir=data_dir),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(thread.join, 5)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "collection",
                        "claim",
                        "--handoff",
                        job.handoffs[0].handoff_id,
                        "--collector",
                        "collector-a",
                        "--data-dir",
                        str(data_dir),
                    ]
                )
            payload = json.loads(stdout.getvalue())

            replay_stdout = io.StringIO()
            with contextlib.redirect_stdout(replay_stdout):
                replay_exit = main(
                    [
                        "collection",
                        "claim",
                        "--handoff",
                        job.handoffs[0].handoff_id,
                        "--collector",
                        "collector-b",
                        "--data-dir",
                        str(data_dir),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["collector_id"], "collector-a")
            self.assertEqual(payload["job"]["status"], "claimed")
            self.assertEqual(
                payload["exchange_dir"],
                str((data_dir / "jobs" / job.job_id / "out").absolute()),
            )
            self.assertEqual(payload["manifest"]["account"]["external_user_id"], "12345")
            self.assertIn("lease", payload)
            self.assertNotIn("cookie", stdout.getvalue().lower())
            self.assertNotIn("api_key", stdout.getvalue().lower())
            self.assertEqual(replay_exit, 1)
            self.assertIn("collection_handoff_gone", replay_stdout.getvalue())

    def test_collection_claim_runtime_missing_is_nonzero_and_clear(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    ["collection", "claim", "--handoff", "missing", "--data-dir", temp_dir]
                )
        self.assertEqual(exit_code, 1)
        self.assertIn("runtime record not found", stdout.getvalue().lower())

    def test_collection_claim_quotes_opaque_handoff_as_one_path_segment(self) -> None:
        runtime = RuntimeRecord(
            1, "instance-a", "http://127.0.0.1:43210", 123, "2026-07-11T08:00:00Z"
        )
        response = {
            "job": {"job_id": "job-a", "status": "claimed"},
            "manifest": {"job_id": "job-a"},
            "lease": {"collector_id": "collector-a", "expires_at": "2026-07-11T08:02:00Z"},
        }
        stdout = io.StringIO()
        with patch("voicevault.cli.RuntimeRegistry.discover", return_value=runtime), patch(
            "voicevault.cli._post_local_json", return_value=response
        ) as post, contextlib.redirect_stdout(stdout):
            exit_code = main(
                [
                    "collection",
                    "claim",
                    "--handoff",
                    "opaque/part?#fragment",
                    "--collector",
                    "collector-a",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            post.call_args.args[0],
            "http://127.0.0.1:43210/api/collection-handoffs/opaque%2Fpart%3F%23fragment/claim",
        )

    def test_collection_submit_posts_digest_only_envelope_and_quotes_job_id(self) -> None:
        runtime = RuntimeRecord(
            1, "instance-a", "http://127.0.0.1:43210", 123, "2026-07-11T08:00:00Z"
        )
        response = {
            "ok": True,
            "job": {"job_id": "opaque/job?#fragment", "status": "succeeded"},
            "submission": {"submission_id": "submission-a", "replayed": False},
        }
        stdout = io.StringIO()
        with patch("voicevault.cli.RuntimeRegistry.discover", return_value=runtime), patch(
            "voicevault.cli._post_local_json", return_value=response
        ) as post, contextlib.redirect_stdout(stdout):
            exit_code = main(
                [
                    "collection", "submit", "--job", "opaque/job?#fragment",
                    "--collector", "collector-a", "--handoff-version", "2",
                    "--manifest-sha256", "a" * 64,
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            post.call_args.args,
            (
                "http://127.0.0.1:43210/api/collection-jobs/opaque%2Fjob%3F%23fragment/submit",
                {"collector_id": "collector-a", "handoff_version": 2, "manifest_sha256": "a" * 64},
            ),
        )
        self.assertEqual(json.loads(stdout.getvalue()), response)

    def test_serve_with_resource_api_rejects_non_loopback_host_with_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            kb = init_kb(root / "kb")
            repo_root = Path(__file__).resolve().parents[1]
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "voicevault",
                    "serve",
                    "--kb",
                    str(kb.root),
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "0",
                    "--data-dir",
                    str(root / "runtime"),
                ],
                cwd=repo_root,
                env={
                    **os.environ,
                    "PYTHONPATH": os.pathsep.join(
                        filter(None, [str(repo_root / "src"), os.environ.get("PYTHONPATH", "")])
                    ),
                },
                capture_output=True,
                text=True,
                timeout=2,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("loopback", (completed.stdout + completed.stderr).lower())

    def test_dashboard_json_outputs_static_dashboard_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["build", "--kb", str(kb.root)]), 0)
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(["dashboard", "--kb", str(kb.root), "--json"])

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertTrue(Path(payload["path"]).is_file())
            self.assertEqual(payload["kind"], "static_html")

    def test_ui_json_outputs_local_workbench_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["sync", "--kb", str(kb.root)]), 0)
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(["ui", "--kb", str(kb.root), "--root", str(repo), "--json"])

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["kind"], "static_ui")
            self.assertTrue(Path(payload["index_html"]).is_file())
            self.assertTrue(Path(payload["data_json"]).is_file())
            data = json.loads(Path(payload["data_json"]).read_text(encoding="utf-8"))
            self.assertEqual(data["repo_root"], str(repo.resolve()))

    def test_sync_json_reports_errors_and_status_reads_last_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            (kb.inbox_captures_dir / "bad.jsonl").write_text("{bad-json}\n", encoding="utf-8")
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(["sync", "--kb", str(kb.root), "--json"])

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 1)
            self.assertEqual(len(payload["errors"]), 1)
            self.assertIn("bad.jsonl", payload["errors"][0]["source_file"])

            status_stdout = io.StringIO()
            with contextlib.redirect_stdout(status_stdout):
                status_exit_code = main(["sync", "--kb", str(kb.root), "--status", "--json"])

            status = json.loads(status_stdout.getvalue())
            self.assertEqual(status_exit_code, 0)
            self.assertFalse(status["ok"])
            self.assertEqual(status["last_result"]["errors"], payload["errors"])

    def test_sync_archive_and_capture_status_json_report_capture_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            (kb.inbox_captures_dir / "batch.jsonl").write_text(
                json.dumps(
                    {
                        "role_id": "capture-source",
                        "platform": "x",
                        "url": "https://x.com/capture/status/1",
                        "published_at": "2026-05-30",
                        "text": "NVDA capture lifecycle.",
                        "symbols": ["NVDA"],
                        "topics": ["capture-lifecycle"],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            sync_stdout = io.StringIO()

            with contextlib.redirect_stdout(sync_stdout):
                sync_exit_code = main(["sync", "--kb", str(kb.root), "--archive", "--json"])

            sync_payload = json.loads(sync_stdout.getvalue())
            self.assertEqual(sync_exit_code, 0)
            self.assertEqual(len(sync_payload["archived_files"]), 1)

            status_stdout = io.StringIO()
            with contextlib.redirect_stdout(status_stdout):
                status_exit_code = main(["capture", "status", "--kb", str(kb.root), "--json"])

            status = json.loads(status_stdout.getvalue())
            self.assertEqual(status_exit_code, 0)
            self.assertEqual(status["summary"]["processed"], 1)
            self.assertEqual(status["summary"]["failed"], 0)
            self.assertEqual(status["pending_count"], 0)

    def test_capture_validate_json_reports_valid_and_invalid_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            valid_path = Path(temp_dir) / "valid.jsonl"
            bad_path = Path(temp_dir) / "bad.jsonl"
            valid_path.write_text(
                json.dumps({"role_id": "capture-source", "platform": "x", "text": "Valid capture."})
                + "\n",
                encoding="utf-8",
            )
            bad_path.write_text("{bad-json}\n", encoding="utf-8")

            valid_stdout = io.StringIO()
            with contextlib.redirect_stdout(valid_stdout):
                valid_exit = main(["capture", "validate", "--path", str(valid_path), "--json"])
            valid_payload = json.loads(valid_stdout.getvalue())

            bad_stdout = io.StringIO()
            with contextlib.redirect_stdout(bad_stdout):
                bad_exit = main(["capture", "validate", "--path", str(bad_path), "--json"])
            bad_payload = json.loads(bad_stdout.getvalue())

            self.assertEqual(valid_exit, 0)
            self.assertTrue(valid_payload["ok"])
            self.assertEqual(valid_payload["records"], 1)
            self.assertEqual(bad_exit, 1)
            self.assertFalse(bad_payload["ok"])
            self.assertIn("invalid JSONL record", bad_payload["errors"][0])

    def test_capture_append_json_writes_valid_capture_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "capture",
                        "append",
                        "--kb",
                        str(kb.root),
                        "--role",
                        "adapter-source",
                        "--platform",
                        "x",
                        "--url",
                        "https://x.com/adapter/status/1",
                        "--title",
                        "Adapter capture",
                        "--text",
                        "NVDA adapter capture.",
                        "--symbols",
                        "NVDA",
                        "--topics",
                        "adapter",
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertTrue(Path(payload["path"]).is_file())
            self.assertTrue(payload["validation"]["ok"])
            self.assertEqual(payload["validation"]["records"], 1)

    def test_sample_remove_json_removes_seed_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(["sample", "remove", "--kb", str(kb.root), "--json"])

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["removed_roles"], ["sample-investor"])
            self.assertFalse((kb.roles_dir / "sample-investor").exists())

    def test_sample_remove_dry_run_json_preserves_seed_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(["sample", "remove", "--kb", str(kb.root), "--dry-run", "--json"])

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["dry_run"])
            self.assertEqual(payload["removed_roles"], ["sample-investor"])
            self.assertIn("removed_exports", payload)
            self.assertTrue((kb.roles_dir / "sample-investor").exists())

    def test_roles_create_json_outputs_generated_profile_draft(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "roles",
                        "create",
                        "--kb",
                        str(kb.root),
                        "--role",
                        "public-analyst",
                        "--display-name",
                        "Public Analyst",
                        "--platform",
                        "x",
                        "--source-url",
                        "https://x.com/public_analyst",
                        "--tags",
                        "semiconductors,macro",
                        "--notes",
                        "待补充公开 statement。",
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["role_id"], "public-analyst")
            self.assertTrue(Path(payload["generated_profile_path"]).is_file())
            self.assertEqual(payload["profile_status"], "generated_unreviewed")

    def test_roles_coverage_json_reports_multi_role_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(["roles", "coverage", "--kb", str(kb.root), "--json"])

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["min_reviewed_roles"], 2)
            self.assertEqual(payload["reviewed_roles_with_statements"], 1)
            self.assertEqual(payload["ready_role_ids"], ["sample-investor"])
            self.assertIn("remediation", payload)

    def test_sources_create_list_and_run_json_manage_capture_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            create_stdout = io.StringIO()

            with contextlib.redirect_stdout(create_stdout):
                create_exit = main(
                    [
                        "sources",
                        "create",
                        "--kb",
                        str(kb.root),
                        "--source",
                        "x-public-analyst",
                        "--role",
                        "public-analyst",
                        "--platform",
                        "x",
                        "--source-url",
                        "https://x.com/public_analyst",
                        "--display-name",
                        "Public Analyst on X",
                        "--symbols",
                        "NVDA",
                        "--topics",
                        "ai-infrastructure",
                        "--json",
                    ]
                )

            created = json.loads(create_stdout.getvalue())
            self.assertEqual(create_exit, 0)
            self.assertEqual(created["source_id"], "x-public-analyst")
            self.assertTrue(Path(created["config_path"]).is_file())

            list_stdout = io.StringIO()
            with contextlib.redirect_stdout(list_stdout):
                list_exit = main(["sources", "list", "--kb", str(kb.root), "--json"])
            listed = json.loads(list_stdout.getvalue())
            self.assertEqual(list_exit, 0)
            self.assertEqual(listed["summary"]["total"], 1)
            self.assertEqual(listed["sources"][0]["source_id"], "x-public-analyst")

            run_stdout = io.StringIO()
            with contextlib.redirect_stdout(run_stdout):
                run_exit = main(
                    [
                        "sources",
                        "run",
                        "--kb",
                        str(kb.root),
                        "--source",
                        "x-public-analyst",
                        "--text",
                        "NVDA source-run capture.",
                        "--title",
                        "Source run",
                        "--json",
                    ]
                )
            run = json.loads(run_stdout.getvalue())
            self.assertEqual(run_exit, 0)
            self.assertEqual(run["written"], 1)
            self.assertTrue(Path(run["capture_path"]).is_file())

    def test_sources_status_json_reports_recent_source_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "sources",
                            "create",
                            "--kb",
                            str(kb.root),
                            "--source",
                            "x-public-analyst",
                            "--role",
                            "public-analyst",
                            "--platform",
                            "x",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    main(
                        [
                            "sources",
                            "run",
                            "--kb",
                            str(kb.root),
                            "--source",
                            "x-public-analyst",
                            "--text",
                            "Dry run.",
                            "--dry-run",
                        ]
                    ),
                    0,
                )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["sources", "status", "--kb", str(kb.root), "--json"])

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["summary"]["dry_run"], 1)
            self.assertEqual(payload["summary"]["active_without_runs"], 0)
            self.assertEqual(payload["runs"][0]["source_id"], "x-public-analyst")
            self.assertEqual(payload["runs"][0]["status"], "dry_run")

    def test_sources_run_json_records_disabled_source_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "sources",
                            "create",
                            "--kb",
                            str(kb.root),
                            "--source",
                            "x-disabled",
                            "--role",
                            "public-analyst",
                            "--platform",
                            "x",
                            "--disabled",
                        ]
                    ),
                    0,
                )

            run_stdout = io.StringIO()
            with contextlib.redirect_stdout(run_stdout):
                run_exit = main(
                    [
                        "sources",
                        "run",
                        "--kb",
                        str(kb.root),
                        "--source",
                        "x-disabled",
                        "--text",
                        "Will fail.",
                        "--json",
                    ]
                )

            run = json.loads(run_stdout.getvalue())
            self.assertEqual(run_exit, 1)
            self.assertEqual(run["status"], "failed")
            self.assertIn("not active", run["error"])
            self.assertEqual(run["source_id"], "x-disabled")

    def test_sources_enqueue_jobs_and_run_job_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "sources",
                            "create",
                            "--kb",
                            str(kb.root),
                            "--source",
                            "x-public-analyst",
                            "--role",
                            "public-analyst",
                            "--platform",
                            "x",
                        ]
                    ),
                    0,
                )

            enqueue_stdout = io.StringIO()
            with contextlib.redirect_stdout(enqueue_stdout):
                enqueue_exit = main(
                    [
                        "sources",
                        "enqueue",
                        "--kb",
                        str(kb.root),
                        "--source",
                        "x-public-analyst",
                        "--json",
                    ]
                )
            enqueued = json.loads(enqueue_stdout.getvalue())
            job_id = enqueued["jobs"][0]["job_id"]

            jobs_stdout = io.StringIO()
            with contextlib.redirect_stdout(jobs_stdout):
                jobs_exit = main(["sources", "jobs", "--kb", str(kb.root), "--json"])
            jobs = json.loads(jobs_stdout.getvalue())

            run_stdout = io.StringIO()
            with contextlib.redirect_stdout(run_stdout):
                run_exit = main(
                    [
                        "sources",
                        "run",
                        "--kb",
                        str(kb.root),
                        "--job",
                        job_id,
                        "--text",
                        "Dry-run job capture.",
                        "--dry-run",
                        "--json",
                    ]
                )
            run = json.loads(run_stdout.getvalue())

            completed_stdout = io.StringIO()
            with contextlib.redirect_stdout(completed_stdout):
                completed_exit = main(["sources", "jobs", "--kb", str(kb.root), "--status", "completed", "--json"])
            completed = json.loads(completed_stdout.getvalue())

            self.assertEqual(enqueue_exit, 0)
            self.assertEqual(enqueued["created"], 1)
            self.assertEqual(jobs_exit, 0)
            self.assertEqual(jobs["summary"]["pending"], 1)
            self.assertEqual(jobs["jobs"][0]["job_id"], job_id)
            self.assertEqual(run_exit, 0)
            self.assertEqual(run["source_id"], "x-public-analyst")
            self.assertEqual(run["job"]["status"], "completed")
            self.assertEqual(completed_exit, 0)
            self.assertEqual(completed["summary"]["completed"], 1)
            self.assertEqual(completed["jobs"][0]["job_id"], job_id)

    def test_sources_retry_json_moves_failed_job_to_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "sources",
                            "create",
                            "--kb",
                            str(kb.root),
                            "--source",
                            "x-public-analyst",
                            "--role",
                            "public-analyst",
                            "--platform",
                            "x",
                        ]
                    ),
                    0,
                )

            enqueue_stdout = io.StringIO()
            with contextlib.redirect_stdout(enqueue_stdout):
                enqueue_exit = main(["sources", "enqueue", "--kb", str(kb.root), "--json"])
            job_id = json.loads(enqueue_stdout.getvalue())["jobs"][0]["job_id"]

            fail_stdout = io.StringIO()
            with contextlib.redirect_stdout(fail_stdout):
                fail_exit = main(["sources", "run", "--kb", str(kb.root), "--job", job_id, "--json"])

            retry_stdout = io.StringIO()
            with contextlib.redirect_stdout(retry_stdout):
                retry_exit = main(
                    [
                        "sources",
                        "retry",
                        "--kb",
                        str(kb.root),
                        "--job",
                        job_id,
                        "--due-at",
                        "soon",
                        "--json",
                    ]
                )
            retry = json.loads(retry_stdout.getvalue())

            self.assertEqual(enqueue_exit, 0)
            self.assertEqual(fail_exit, 1)
            self.assertEqual(json.loads(fail_stdout.getvalue())["job"]["status"], "failed")
            self.assertEqual(retry_exit, 0)
            self.assertEqual(retry["job"]["status"], "pending")
            self.assertEqual(retry["job"]["due_at"], "soon")
            self.assertEqual(retry["summary"]["pending"], 1)
            self.assertEqual(retry["summary"]["failed"], 0)

    def test_sources_drain_json_runs_pending_local_jsonl_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            input_path = Path(temp_dir) / "public-feed.jsonl"
            input_path.write_text(
                '{"text":"Queued CLI adapter record.","source_url":"https://x.com/public/status/5"}\n',
                encoding="utf-8",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "sources",
                            "create",
                            "--kb",
                            str(kb.root),
                            "--source",
                            "local-jsonl-source",
                            "--role",
                            "public-analyst",
                            "--platform",
                            "x",
                            "--adapter",
                            "local-jsonl",
                            "--adapter-config",
                            json.dumps({"input_path": str(input_path)}),
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    main(["sources", "enqueue", "--kb", str(kb.root), "--source", "local-jsonl-source"]),
                    0,
                )

            drain_stdout = io.StringIO()
            with contextlib.redirect_stdout(drain_stdout):
                drain_exit = main(["sources", "drain", "--kb", str(kb.root), "--dry-run", "--json"])
            drained = json.loads(drain_stdout.getvalue())
            capture_path = kb.inbox_captures_dir / "source-local-jsonl-source.jsonl"

            self.assertEqual(drain_exit, 0)
            self.assertEqual(drained["processed"], 1)
            self.assertEqual(drained["completed"], 1)
            self.assertEqual(drained["failed"], 0)
            self.assertTrue(drained["dry_run"])
            self.assertEqual(drained["jobs"][0]["status"], "completed")
            self.assertEqual(drained["jobs"][0]["written"], 0)
            self.assertEqual(drained["summary"]["pending"], 0)
            self.assertFalse(capture_path.exists())

    def test_sources_create_local_jsonl_adapter_and_run_without_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            input_path = Path(temp_dir) / "public-feed.jsonl"
            input_path.write_text(
                '{"text":"Adapter CLI record.","title":"Adapter CLI item","source_url":"https://x.com/public/status/2"}\n',
                encoding="utf-8",
            )
            create_stdout = io.StringIO()

            with contextlib.redirect_stdout(create_stdout):
                create_exit = main(
                    [
                        "sources",
                        "create",
                        "--kb",
                        str(kb.root),
                        "--source",
                        "local-jsonl-source",
                        "--role",
                        "public-analyst",
                        "--platform",
                        "x",
                        "--adapter",
                        "local-jsonl",
                        "--adapter-config",
                        json.dumps({"input_path": str(input_path)}),
                        "--json",
                    ]
                )
            created = json.loads(create_stdout.getvalue())

            run_stdout = io.StringIO()
            with contextlib.redirect_stdout(run_stdout):
                run_exit = main(
                    [
                        "sources",
                        "run",
                        "--kb",
                        str(kb.root),
                        "--source",
                        "local-jsonl-source",
                        "--dry-run",
                        "--json",
                    ]
                )
            run = json.loads(run_stdout.getvalue())

            self.assertEqual(create_exit, 0)
            self.assertEqual(created["adapter_config"]["input_path"], str(input_path))
            self.assertEqual(run_exit, 0)
            self.assertEqual(run["record_count"], 1)
            self.assertEqual(run["records"][0]["title"], "Adapter CLI item")
            self.assertEqual(run["written"], 0)

    def test_sources_validate_json_reports_adapter_config_health(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            input_path = Path(temp_dir) / "public-feed.jsonl"
            input_path.write_text(
                '{"text":"Adapter validation CLI record.","title":"Adapter validation item"}\n',
                encoding="utf-8",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "sources",
                            "create",
                            "--kb",
                            str(kb.root),
                            "--source",
                            "local-jsonl-source",
                            "--role",
                            "public-analyst",
                            "--platform",
                            "x",
                            "--adapter",
                            "local-jsonl",
                            "--adapter-config",
                            json.dumps({"input_path": str(input_path)}),
                        ]
                    ),
                    0,
                )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["sources", "validate", "--kb", str(kb.root), "--json"])

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["summary"]["ready"], 1)
            self.assertEqual(payload["sources"][0]["record_count"], 1)
            self.assertEqual(payload["sources"][0]["status"], "ready")

    def test_sources_normalize_json_updates_source_for_local_jsonl_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            input_path = Path(temp_dir) / "export.csv"
            input_path.write_text(
                "text,url\nCLI normalized statement.,https://x.com/public/status/3\n",
                encoding="utf-8",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "sources",
                            "create",
                            "--kb",
                            str(kb.root),
                            "--source",
                            "x-public-analyst",
                            "--role",
                            "public-analyst",
                            "--platform",
                            "x",
                        ]
                    ),
                    0,
                )

            normalize_stdout = io.StringIO()
            with contextlib.redirect_stdout(normalize_stdout):
                normalize_exit = main(
                    [
                        "sources",
                        "normalize",
                        "--kb",
                        str(kb.root),
                        "--source",
                        "x-public-analyst",
                        "--input",
                        str(input_path),
                        "--update-source",
                        "--json",
                    ]
                )
            normalized = json.loads(normalize_stdout.getvalue())

            run_stdout = io.StringIO()
            with contextlib.redirect_stdout(run_stdout):
                run_exit = main(
                    [
                        "sources",
                        "run",
                        "--kb",
                        str(kb.root),
                        "--source",
                        "x-public-analyst",
                        "--dry-run",
                        "--json",
                    ]
                )
            run = json.loads(run_stdout.getvalue())

            self.assertEqual(normalize_exit, 0)
            self.assertEqual(normalized["record_count"], 1)
            self.assertTrue(Path(normalized["output_path"]).is_file())
            self.assertTrue(normalized["updated_source"])
            self.assertEqual(run_exit, 0)
            self.assertEqual(run["record_count"], 1)
            self.assertEqual(run["records"][0]["text"], "CLI normalized statement.")

    def test_accounts_create_list_and_collect_rss_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            feed_path = Path(temp_dir) / "feed.xml"
            feed_path.write_text(
                "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
                "<rss version=\"2.0\"><channel><title>Public Analyst</title>"
                "<item><guid>cli-post-1</guid><title>CLI RSS item</title>"
                "<link>https://example.com/cli/1</link>"
                "<pubDate>Tue, 09 Jun 2026 08:00:00 GMT</pubDate>"
                "<description>CLI RSS public text.</description></item>"
                "</channel></rss>",
                encoding="utf-8",
            )

            create_stdout = io.StringIO()
            with contextlib.redirect_stdout(create_stdout):
                create_exit = main(
                    [
                        "accounts",
                        "create",
                        "--kb",
                        str(kb.root),
                        "--account",
                        "rss-public-analyst",
                        "--platform",
                        "rss",
                        "--platform-account-id",
                        "public-analyst",
                        "--role",
                        "public-analyst",
                        "--display-name",
                        "Public Analyst",
                        "--feed-url",
                        str(feed_path),
                        "--json",
                    ]
                )
            created = json.loads(create_stdout.getvalue())

            list_stdout = io.StringIO()
            with contextlib.redirect_stdout(list_stdout):
                list_exit = main(["accounts", "list", "--kb", str(kb.root), "--json"])
            listed = json.loads(list_stdout.getvalue())

            collect_stdout = io.StringIO()
            with contextlib.redirect_stdout(collect_stdout):
                collect_exit = main(
                    [
                        "accounts",
                        "collect",
                        "--kb",
                        str(kb.root),
                        "--account",
                        "rss-public-analyst",
                        "--sync",
                        "--json",
                    ]
                )
            collected = json.loads(collect_stdout.getvalue())

            self.assertEqual(create_exit, 0)
            self.assertEqual(created["collection_mode"], "rss")
            self.assertEqual(list_exit, 0)
            self.assertEqual(listed["summary"]["total"], 1)
            self.assertEqual(listed["accounts"][0]["account_id"], "rss-public-analyst")
            self.assertEqual(collect_exit, 0)
            self.assertTrue(collected["ok"])
            self.assertEqual(collected["written"], 1)
            self.assertTrue(Path(collected["capture_path"]).is_file())
            self.assertEqual(collected["sync"]["notes_written"], 1)

    def test_event_list_json_outputs_event_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(["event", "list", "--kb", str(kb.root), "--json"])

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload[0]["event_id"], "example-nvda-margin")
            self.assertTrue(Path(payload[0]["path"]).is_file())

    def test_profile_promote_outputs_reviewed_profile_path_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            reviewed_profile = kb.roles_dir / "sample-investor" / "profile.md"
            reviewed_profile.unlink()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["build", "--kb", str(kb.root)]), 0)
                self.assertEqual(main(["profile", "generate", "--role", "sample-investor", "--kb", str(kb.root)]), 0)
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "profile",
                        "promote",
                        "--role",
                        "sample-investor",
                        "--kb",
                        str(kb.root),
                        "--reviewer",
                        "codex-product-review",
                        "--note",
                        "Reviewed against indexed evidence.",
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertTrue(Path(payload["profile_path"]).is_file())
            self.assertEqual(payload["role_id"], "sample-investor")
            text = Path(payload["profile_path"]).read_text(encoding="utf-8")
            self.assertIn("reviewed_by: codex-product-review", text)
            self.assertIn("review_note: Reviewed against indexed evidence.", text)

    def test_collect_outputs_evidence_pack_path_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["build", "--kb", str(kb.root)]), 0)
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "collect",
                        "--kb",
                        str(kb.root),
                        "--title",
                        "NVDA Margin Evidence",
                        "--query",
                        "NVDA margin",
                        "--symbol",
                        "NVDA",
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertTrue(Path(payload["path"]).is_file())
            self.assertEqual(payload["title"], "NVDA Margin Evidence")

    def test_reports_list_json_outputs_evidence_pack_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["build", "--kb", str(kb.root)]), 0)
                self.assertEqual(
                    main(
                        [
                            "collect",
                            "--kb",
                            str(kb.root),
                            "--title",
                            "NVDA Margin Evidence",
                            "--query",
                            "NVDA margin",
                            "--symbol",
                            "NVDA",
                        ]
                    ),
                    0,
                )
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(["reports", "list", "--kb", str(kb.root), "--json"])

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload[0]["title"], "NVDA Margin Evidence")
            self.assertTrue(Path(payload[0]["path"]).is_file())
            self.assertEqual(payload[0]["query"], "NVDA margin")

    def test_search_json_outputs_ranked_statement_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["build", "--kb", str(kb.root)]), 0)
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "search",
                        "--kb",
                        str(kb.root),
                        "--query",
                        "NVDA margin",
                        "--symbol",
                        "NVDA",
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["query"], "NVDA margin")
            self.assertGreaterEqual(payload["count"], 1)
            self.assertEqual(payload["results"][0]["role_id"], "sample-investor")

    def test_answer_json_outputs_cited_answer_and_export_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["build", "--kb", str(kb.root)]), 0)
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "answer",
                        "--kb",
                        str(kb.root),
                        "--query",
                        "NVDA margin",
                        "--symbol",
                        "NVDA",
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["answer"]["answer_type"], "local_evidence_answer")
            self.assertTrue(payload["answer"]["citations"])
            self.assertTrue(Path(payload["answer_json"]).is_file())
            self.assertTrue(Path(payload["answer_markdown"]).is_file())

    def test_answers_list_and_prune_manage_invalid_exports(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["build", "--kb", str(kb.root)]), 0)
                self.assertEqual(
                    main(["answer", "--kb", str(kb.root), "--query", "NVDA margin", "--symbol", "NVDA"]),
                    0,
                )
                self.assertEqual(
                    main(["answer", "--kb", str(kb.root), "--query", "unmatched rare query"]),
                    0,
                )
            list_stdout = io.StringIO()

            with contextlib.redirect_stdout(list_stdout):
                list_exit = main(["answers", "list", "--kb", str(kb.root), "--status", "invalid", "--json"])

            listed = json.loads(list_stdout.getvalue())
            self.assertEqual(list_exit, 0)
            self.assertEqual(listed["summary"]["invalid"], 1)
            self.assertEqual(listed["answers"][0]["status"], "no_evidence")

            preview_stdout = io.StringIO()
            with contextlib.redirect_stdout(preview_stdout):
                preview_exit = main(["answers", "prune", "--kb", str(kb.root), "--status", "invalid", "--json"])
            preview = json.loads(preview_stdout.getvalue())

            self.assertEqual(preview_exit, 0)
            self.assertTrue(preview["dry_run"])
            self.assertEqual(preview["removed"], 0)
            self.assertTrue(Path(preview["answers"][0]["answer_json"]).is_file())

            prune_stdout = io.StringIO()
            with contextlib.redirect_stdout(prune_stdout):
                prune_exit = main(["answers", "prune", "--kb", str(kb.root), "--status", "invalid", "--yes", "--json"])
            pruned = json.loads(prune_stdout.getvalue())

            self.assertEqual(prune_exit, 0)
            self.assertFalse(pruned["dry_run"])
            self.assertEqual(pruned["removed"], 1)
            self.assertFalse(Path(pruned["answers"][0]["answer_json"]).exists())

    def test_doctor_repair_json_creates_missing_capture_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            for path in kb.inbox_captures_dir.iterdir():
                path.unlink()
            kb.inbox_captures_dir.rmdir()
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(["doctor", "--kb", str(kb.root), "--repair", "--json"])

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 1)
            self.assertTrue(kb.inbox_captures_dir.is_dir())
            self.assertIn(str(kb.inbox_captures_dir), payload["created_dirs"])
            self.assertIn("Index has not been built.", payload["warnings"])

    def test_analyze_json_outputs_machine_readable_export_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            capture_path = kb.inbox_captures_dir / "captures.jsonl"
            capture_path.write_text(
                json.dumps(
                    {
                        "role_id": "growth-analyst",
                        "platform": "x",
                        "author": "Growth Analyst",
                        "url": "https://x.com/growth/status/1",
                        "published_at": "2026-05-29T12:00:00Z",
                        "captured_at": "2026-05-30T01:00:00Z",
                        "title": "NVDA demand",
                        "text": "NVDA AI infrastructure demand remains durable.",
                        "symbols": ["NVDA"],
                        "topics": ["ai-infrastructure"],
                        "stance": "bullish",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            event_path = create_event(
                kb,
                event_id="2026-05-30-nvda-demand",
                title="NVIDIA Demand",
                date="2026-05-30",
                symbols=["NVDA"],
                topics=["ai-infrastructure"],
                summary="Investors debate AI infrastructure demand durability.",
            )

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["sync", "--kb", str(kb.root)]), 0)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "analyze",
                        "--kb",
                        str(kb.root),
                        "--event",
                        str(event_path),
                        "--roles",
                        "growth-analyst",
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["event_id"], "2026-05-30-nvda-demand")
            self.assertEqual(payload["role_count"], 1)
            self.assertTrue(Path(payload["analysis_json"]).is_file())
            self.assertTrue(Path(payload["analysis_markdown"]).is_file())

    def test_analyses_list_json_outputs_analysis_export_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["build", "--kb", str(kb.root)]), 0)
                self.assertEqual(
                    main(
                        [
                            "analyze",
                            "--kb",
                            str(kb.root),
                            "--event",
                            str(kb.events_dir / "example-event.md"),
                            "--roles",
                            "all",
                        ]
                    ),
                    0,
                )
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(["analyses", "list", "--kb", str(kb.root), "--json"])

            payload = json.loads(stdout.getvalue())

            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["summary"]["total"], 1)
            self.assertEqual(payload["summary"]["ready"], 1)
            self.assertEqual(payload["summary"]["malformed"], 0)
            self.assertEqual(payload["analyses"][0]["event_id"], "example-nvda-margin")
            self.assertEqual(payload["analyses"][0]["role_summaries"][0]["role_id"], "sample-investor")
            self.assertIn("analysis.json", payload["analyses"][0]["analysis_json"])
            self.assertGreaterEqual(len(payload["analyses"][0]["evidence_summaries"]), 1)
            self.assertTrue(payload["analyses"][0]["evidence_summaries"][0]["source_url"])
            self.assertTrue(payload["analyses"][0]["role_summaries"][0]["supporting_evidence"][0]["excerpt"])


def _add_growth_role(kb) -> None:
    role_dir = kb.roles_dir / "growth-investor"
    role_dir.mkdir(parents=True)
    (role_dir / "profile.md").write_text(
        "---\n"
        "role_id: growth-investor\n"
        "display_name: Growth Investor\n"
        "status: reviewed\n"
        "---\n"
        "\n"
        "# Role Profile\n",
        encoding="utf-8",
        newline="\n",
    )
    (role_dir / "statements.csv").write_text(
        "statement_id,role_id,source_type,source_url,published_at,captured_at,title,body,symbols,topics,stance,time_horizon,confidence,notes\n"
        "growth-001,growth-investor,post,https://example.com/growth-nvda,2026-05-27,2026-05-28,NVDA AI demand,AI infrastructure demand can offset short term margin pressure for NVIDIA,NVDA,ai-infrastructure,bullish,long_term,medium,\n"
        "growth-002,growth-investor,post,https://example.com/growth-margin,2026-05-26,2026-05-27,NVDA margin context,Margin pressure matters less if AI revenue growth keeps compounding,NVDA,earnings,bullish,long_term,medium,\n",
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    unittest.main()
