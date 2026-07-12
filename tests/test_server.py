from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from voicevault import __version__
from voicevault.app_db import AppDatabase
from voicevault.action_runs import record_action_run, read_action_runs
from voicevault.importers import load_statements_from_kb
from voicevault.index import VoiceVaultIndex
from voicevault.kb import init_kb
from voicevault.server import create_server


class LocalServerTests(unittest.TestCase):
    def test_resource_server_uses_a_thirty_minute_collection_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            server = create_server(
                init_kb(root / "voicevault"),
                host="127.0.0.1",
                port=0,
                app_database=AppDatabase(data_dir=root / "runtime"),
            )
            try:
                self.assertEqual(
                    server.collection_service.lease_ttl, timedelta(minutes=30)
                )
            finally:
                server.server_close()

    def test_resource_api_rejects_non_loopback_bind_but_legacy_server_remains_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            kb = init_kb(root / "voicevault")
            database = AppDatabase(data_dir=root / "runtime")

            resource_server = None
            try:
                with self.assertRaisesRegex(ValueError, "loopback"):
                    resource_server = create_server(
                        kb, host="0.0.0.0", port=0, app_database=database
                    )
            finally:
                if resource_server is not None:
                    resource_server.server_close()

            legacy = create_server(kb, host="0.0.0.0", port=0)
            try:
                self.assertEqual(legacy.server_address[0], "0.0.0.0")
            finally:
                legacy.server_close()

    def test_legacy_post_body_remains_available_when_resource_api_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            kb = init_kb(root / "voicevault")
            repo = root / "repo"
            repo.mkdir()
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            with _running_server(kb, repo, app_database=AppDatabase(data_dir=root / "runtime")) as base_url:
                payload = _post_json(
                    f"{base_url}/api/answer",
                    {"query": "NVDA margin", "symbol": "NVDA", "limit": 2},
                )

            self.assertTrue(payload["ok"])
            self.assertEqual(payload["answer"]["query"], "NVDA margin")

    def test_status_endpoint_reports_local_workbench_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            repo = Path(temp_dir) / "repo"
            repo.mkdir()

            with _running_server(kb, repo) as base_url:
                payload = _get_json(f"{base_url}/api/status")

            self.assertTrue(payload["ok"])
            self.assertEqual(payload["product"]["version"], __version__)
            self.assertEqual(payload["knowledge_base"], str(kb.root))
            self.assertTrue(Path(payload["ui"]["index_html"]).is_file())
            self.assertTrue(Path(payload["ui"]["data_json"]).is_file())

    def test_answer_endpoint_returns_and_archives_local_evidence_answer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            with _running_server(kb, repo) as base_url:
                payload = _post_json(
                    f"{base_url}/api/answer",
                    {"query": "NVDA margin", "symbol": "NVDA", "limit": 2},
                )

            self.assertTrue(payload["ok"])
            self.assertEqual(payload["answer"]["query"], "NVDA margin")
            self.assertGreaterEqual(payload["answer"]["coverage"]["evidence_count"], 1)
            self.assertTrue(Path(payload["answer_json"]).is_file())
            self.assertTrue(Path(payload["answer_markdown"]).is_file())
            self.assertTrue(Path(payload["ui"]["data_json"]).is_file())
            self.assertEqual(payload["ui"]["data"]["summary"]["answer_exports"], 1)
            self.assertEqual(payload["action_run"]["action_type"], "answer")
            self.assertEqual(payload["action_run"]["status"], "completed")
            self.assertEqual(payload["ui"]["data"]["summary"]["action_runs"], 1)
            self.assertEqual(payload["ui"]["data"]["summary"]["action_run_failed"], 0)
            self.assertEqual(payload["ui"]["data"]["action_runs"]["runs"][0]["run_id"], payload["action_run"]["run_id"])
            self.assertIn("next_actions", payload["ui"]["data"])

    def test_answer_endpoint_can_auto_route_to_suggested_role(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            with _running_server(kb, repo) as base_url:
                payload = _post_json(
                    f"{base_url}/api/answer",
                    {"query": "NVDA margin", "symbol": "NVDA", "auto_route": True, "limit": 2},
                )

            archived = json.loads(Path(payload["answer_json"]).read_text(encoding="utf-8"))
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["selection_mode"], "auto")
            self.assertEqual(payload["selected_role_id"], "sample-investor")
            self.assertEqual(payload["role_routing"]["suggested_role_id"], "sample-investor")
            self.assertEqual(payload["answer"]["role_answer"]["role_id"], "sample-investor")
            self.assertEqual(payload["answer"]["role_answer"]["mode"], "single_role")
            self.assertEqual(archived["selected_role_id"], "sample-investor")
            self.assertEqual(archived["role_routing"]["suggested_role_id"], "sample-investor")
            self.assertEqual(archived["role_answer"]["role_id"], "sample-investor")

    def test_role_skill_and_agent_endpoints_distill_and_build_external_llm_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            with _running_server(kb, repo) as base_url:
                distilled = _post_json(
                    f"{base_url}/api/role/distill",
                    {"role_id": "sample-investor", "limit": 4},
                )
                asked = _post_json(
                    f"{base_url}/api/role/ask",
                    {
                        "role_id": "sample-investor",
                        "query": "How would this role reason about NVDA margin?",
                        "symbol": "NVDA",
                        "dry_run": True,
                    },
                )

            self.assertTrue(distilled["ok"])
            self.assertEqual(distilled["skill"]["artifact_type"], "voicevault_role_skill")
            self.assertTrue(Path(distilled["skill_json"]).is_file())
            self.assertEqual(distilled["ui"]["data"]["summary"]["role_skills_ready"], 1)
            self.assertTrue(asked["ok"])
            self.assertEqual(asked["llm"]["status"], "not_called")
            self.assertEqual(asked["prompt_bundle"]["answer_type"], "role_agent_prompt")
            self.assertTrue(Path(asked["role_agent_json"]).is_file())
            self.assertEqual(asked["action_run"]["action_type"], "role_agent")
            self.assertEqual(asked["ui"]["data"]["summary"]["role_agent_exports"], 1)

    def test_role_agent_endpoint_records_failed_llm_error_for_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            env = {
                key: ""
                for key in [
                    "VOICEVAULT_LLM_ENDPOINT",
                    "VOICEVAULT_LLM_BASE_URL",
                    "VOICEVAULT_LLM_API_KEY",
                    "VOICEVAULT_LLM_MODEL",
                ]
            }
            with patch.dict(os.environ, env, clear=False):
                with _running_server(kb, repo) as base_url:
                    try:
                        _post_json(
                            f"{base_url}/api/role/ask",
                            {
                                "role_id": "sample-investor",
                                "query": "How would this role reason about NVDA margin?",
                                "symbol": "NVDA",
                                "dry_run": False,
                            },
                        )
                    except HTTPError as exc:
                        failed = json.loads(exc.read().decode("utf-8"))
                    else:
                        self.fail("Expected role agent LLM call to fail without an endpoint")

            self.assertFalse(failed["ok"])
            self.assertEqual(failed["llm"]["status"], "failed")
            self.assertEqual(failed["action_run"]["status"], "failed")
            self.assertTrue(failed["action_run"]["error"])
            self.assertIn("VOICEVAULT_LLM_ENDPOINT", failed["action_run"]["error"])

    def test_compare_endpoint_returns_and_archives_multi_role_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            _add_growth_role(kb)
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            with _running_server(kb, repo) as base_url:
                payload = _post_json(
                    f"{base_url}/api/compare",
                    {"query": "NVDA margin AI", "symbol": "NVDA", "roles": "auto", "limit": 3, "evidence_limit": 2},
                )

            archived = json.loads(Path(payload["comparison_json"]).read_text(encoding="utf-8"))
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["comparison"]["comparison_type"], "local_evidence_role_comparison")
            self.assertGreaterEqual(payload["comparison"]["coverage"]["role_count"], 2)
            self.assertGreaterEqual(payload["comparison"]["coverage"]["evidence_count"], 2)
            self.assertEqual(archived["query"], "NVDA margin AI")
            self.assertTrue(Path(payload["comparison_markdown"]).is_file())
            self.assertTrue(Path(payload["ui"]["data_json"]).is_file())
            self.assertEqual(payload["ui"]["data"]["summary"]["comparison_exports"], 1)
            self.assertEqual(payload["ui"]["data"]["summary"]["draft_comparison_exports"], 1)
            self.assertEqual(payload["action_run"]["action_type"], "compare")
            self.assertEqual(payload["action_run"]["status"], "completed")
            self.assertEqual(payload["ui"]["data"]["summary"]["action_runs"], 1)
            self.assertTrue(
                any(item["action_type"] == "review_comparison" for item in payload["ui"]["data"]["next_actions"])
            )

    def test_comparison_review_endpoint_marks_archived_comparison_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            _add_growth_role(kb)
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            with _running_server(kb, repo) as base_url:
                created = _post_json(
                    f"{base_url}/api/compare",
                    {"query": "NVDA margin AI", "symbol": "NVDA", "roles": "auto", "limit": 3, "evidence_limit": 2},
                )
                reviewed = _post_json(
                    f"{base_url}/api/comparison/review",
                    {
                        "query": "NVDA margin AI",
                        "status": "adopted",
                        "reviewer": "codex-product-review",
                        "notes": "Approved in local workbench.",
                    },
                )

            archived = json.loads(Path(created["comparison_json"]).read_text(encoding="utf-8"))
            self.assertTrue(reviewed["ok"])
            self.assertEqual(reviewed["comparison"]["review"]["status"], "adopted")
            self.assertEqual(reviewed["comparison"]["review"]["reviewer"], "codex-product-review")
            self.assertEqual(archived["review"]["status"], "adopted")
            self.assertEqual(archived["review"]["notes"], "Approved in local workbench.")
            self.assertTrue(Path(reviewed["ui"]["data_json"]).is_file())
            self.assertEqual(reviewed["ui"]["data"]["summary"]["adopted_comparison_exports"], 1)
            self.assertEqual(reviewed["ui"]["data"]["summary"]["draft_comparison_exports"], 0)
            self.assertEqual(reviewed["action_run"]["action_type"], "comparison_review")
            self.assertEqual(reviewed["action_run"]["status"], "completed")
            self.assertEqual(reviewed["ui"]["data"]["summary"]["action_runs"], 2)
            self.assertEqual(reviewed["ui"]["data"]["action_runs"]["runs"][0]["run_id"], reviewed["action_run"]["run_id"])

    def test_comparison_review_endpoint_records_failed_action_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            _add_growth_role(kb)
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            with _running_server(kb, repo) as base_url:
                _post_json(
                    f"{base_url}/api/compare",
                    {"query": "NVDA margin AI", "symbol": "NVDA", "roles": "auto", "limit": 3, "evidence_limit": 2},
                )
                with self.assertRaises(HTTPError) as raised:
                    _post_json(
                        f"{base_url}/api/comparison/review",
                        {
                            "query": "NVDA margin AI",
                            "status": "invalid-status",
                            "reviewer": "codex-product-review",
                        },
                    )

            error = raised.exception
            body = json.loads(error.read().decode("utf-8"))
            action_runs = read_action_runs(kb)
            self.assertEqual(error.code, 400)
            self.assertFalse(body["ok"])
            self.assertIn("Unknown comparison review status", body["error"])
            self.assertEqual(action_runs["summary"]["failed"], 1)
            self.assertEqual(action_runs["runs"][0]["action_type"], "comparison_review")
            self.assertEqual(action_runs["runs"][0]["status"], "failed")

    def test_action_run_retry_endpoint_replays_failed_answer_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            failed = record_action_run(
                kb,
                action_type="answer",
                status="failed",
                payload={"query": "NVDA margin", "symbol": "NVDA", "limit": 2},
                error="temporary local failure",
                source="local_api",
            )

            with _running_server(kb, repo) as base_url:
                payload = _post_json(f"{base_url}/api/action-runs/retry", {"run_id": failed["run_id"]})

            action_runs = read_action_runs(kb)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["retried_from"]["run_id"], failed["run_id"])
            self.assertEqual(payload["action_run"]["action_type"], "answer")
            self.assertEqual(payload["action_run"]["status"], "completed")
            self.assertEqual(payload["action_run"]["source"], "action_retry")
            self.assertEqual(payload["action_run"]["result"]["retry_of"], failed["run_id"])
            self.assertTrue(Path(payload["answer_json"]).is_file())
            self.assertFalse(action_runs["runs"][1]["retryable"])
            self.assertEqual(action_runs["runs"][1]["resolved_by"], payload["action_run"]["run_id"])
            self.assertEqual(action_runs["summary"]["total"], 2)
            self.assertEqual(action_runs["summary"]["failed"], 1)
            self.assertEqual(action_runs["summary"]["retryable_failed"], 0)
            self.assertEqual(action_runs["summary"]["completed"], 1)
            self.assertEqual(payload["ui"]["data"]["summary"]["action_runs"], 2)
            self.assertEqual(payload["ui"]["data"]["summary"]["action_run_failed"], 1)
            self.assertEqual(payload["ui"]["data"]["summary"]["action_run_retryable_failed"], 0)

    def test_onboarding_endpoints_create_public_role_source_and_ingest_statement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            with _running_server(kb, repo) as base_url:
                created = _post_json(
                    f"{base_url}/api/onboarding/role-source",
                    {
                        "role_id": "public-operator",
                        "display_name": "Public Operator",
                        "source_id": "public-operator-blog",
                        "platform": "blog",
                        "source_url": "https://example.com/operator",
                        "symbols": ["NVDA"],
                        "topics": ["ai-infrastructure"],
                        "tags": ["public"],
                    },
                )
                ingested = _post_json(
                    f"{base_url}/api/onboarding/statement",
                    {
                        "source_id": "public-operator-blog",
                        "title": "AI infrastructure capacity",
                        "text": "AI infrastructure capacity can matter more than near term margin pressure for NVDA operators.",
                        "source_url": "https://example.com/operator/ai-capacity",
                        "published_at": "2026-05-31T08:00:00Z",
                        "symbols": ["NVDA"],
                        "topics": ["ai-infrastructure"],
                        "stance": "bullish",
                        "time_horizon": "long_term",
                        "confidence": "medium",
                        "sync": True,
                        "archive": True,
                        "generate_profile": True,
                        "promote_profile": True,
                        "reviewer": "local-ui",
                        "review_note": "Public statement reviewed through local onboarding.",
                    },
                )

            ui_data = json.loads(Path(ingested["ui"]["data_json"]).read_text(encoding="utf-8"))
            role_rows = {item["role_id"]: item for item in ui_data["roles"]}
            coverage_rows = {item["role_id"]: item for item in ingested["role_coverage"]["roles"]}
            answer = _post_local_answer(kb, "NVDA infrastructure", role_id="public-operator")
            next_action_types = {item["action_type"] for item in ingested["next_actions"]}
            answer_action = next(item for item in ingested["next_actions"] if item["action_type"] == "answer")
            compare_action = next(item for item in ingested["next_actions"] if item["action_type"] == "compare")

            self.assertTrue(created["ok"])
            self.assertEqual(created["role"]["role_id"], "public-operator")
            self.assertEqual(created["source"]["source_id"], "public-operator-blog")
            self.assertTrue(Path(created["source"]["config_path"]).is_file())
            self.assertTrue(ingested["ok"])
            self.assertEqual(ingested["source_run"]["written"], 1)
            self.assertGreaterEqual(ingested["sync"]["notes_written"], 1)
            self.assertTrue(Path(ingested["generated_profile_path"]).is_file())
            self.assertTrue(Path(ingested["profile_path"]).is_file())
            self.assertEqual(coverage_rows["public-operator"]["profile_status"], "reviewed")
            self.assertGreaterEqual(coverage_rows["public-operator"]["statement_count"], 1)
            self.assertEqual(role_rows["public-operator"]["display_name"], "Public Operator")
            self.assertEqual(answer["coverage"]["evidence_count"], 1)
            self.assertEqual(answer["evidence"][0]["role_id"], "public-operator")
            self.assertIn("answer", next_action_types)
            self.assertIn("compare", next_action_types)
            self.assertEqual(answer_action["payload"]["role_id"], "public-operator")
            self.assertEqual(answer_action["endpoint"], "/api/answer")
            self.assertEqual(compare_action["payload"]["roles"], "auto")
            self.assertEqual(compare_action["endpoint"], "/api/compare")

    def test_account_endpoints_create_and_collect_rss_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            feed_path = Path(temp_dir) / "feed.xml"
            feed_path.write_text(
                "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
                "<rss version=\"2.0\"><channel><title>API Account</title>"
                "<item><guid>api-account-post-1</guid><title>API Account Post</title>"
                "<link>https://example.com/account/1</link>"
                "<pubDate>Tue, 09 Jun 2026 08:00:00 GMT</pubDate>"
                "<description>API account public text.</description></item>"
                "</channel></rss>",
                encoding="utf-8",
            )

            with _running_server(kb, repo) as base_url:
                created = _post_json(
                    f"{base_url}/api/accounts/create",
                    {
                        "account_id": "rss-api-account",
                        "platform": "rss",
                        "platform_account_id": "api-account",
                        "role_id": "api-account-role",
                        "display_name": "API Account",
                        "feed_url": str(feed_path),
                    },
                )
                collected = _post_json(
                    f"{base_url}/api/accounts/collect",
                    {"account_id": "rss-api-account", "sync": True},
                )

            self.assertTrue(created["ok"])
            self.assertEqual(created["account"]["collection_mode"], "rss")
            self.assertTrue(collected["ok"])
            self.assertEqual(collected["account_collection"]["written"], 1)
            self.assertEqual(collected["account_status"]["summary"]["total"], 1)
            self.assertEqual(collected["ui"]["data"]["summary"]["account_archives"], 1)
            self.assertTrue(Path(collected["account_collection"]["capture_path"]).is_file())

    def test_answer_endpoint_rejects_missing_query(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            repo = Path(temp_dir) / "repo"
            repo.mkdir()

            with _running_server(kb, repo) as base_url:
                with self.assertRaises(HTTPError) as raised:
                    _post_json(f"{base_url}/api/answer", {"query": "   "})

                error = raised.exception
                body = json.loads(error.read().decode("utf-8"))

            self.assertEqual(error.code, 400)
            self.assertFalse(body["ok"])
            self.assertIn("query", body["error"])

    def test_evaluations_question_endpoints_manage_fixed_answer_regression_suite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            repo = Path(temp_dir) / "repo"
            repo.mkdir()

            with _running_server(kb, repo) as base_url:
                created = _post_json(
                    f"{base_url}/api/evaluations/answer-question",
                    {
                        "query": "NVDA margin",
                        "role_id": "sample-investor",
                        "symbol": "NVDA",
                        "expected_role_id": "sample-investor",
                        "min_evidence": 1,
                        "requires_role_answer": True,
                    },
                )
                deleted = _post_json(
                    f"{base_url}/api/evaluations/answer-question/delete",
                    {"id": created["question"]["id"]},
                )

            suite_path = kb.content_dir / "evaluations" / "questions.json"
            suite = json.loads(suite_path.read_text(encoding="utf-8"))
            self.assertTrue(created["ok"])
            self.assertEqual(created["question"]["id"], "nvda-margin")
            self.assertEqual(created["audit"]["summary"]["total"], 1)
            self.assertEqual(created["ui"]["data"]["answer_regression"]["summary"]["total"], 1)
            self.assertTrue(deleted["ok"])
            self.assertEqual(deleted["deleted_id"], "nvda-margin")
            self.assertEqual(deleted["audit"]["summary"]["total"], 0)
            self.assertEqual(suite["questions"], [])

    def test_evaluations_question_endpoints_return_governance_changelog(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            repo = Path(temp_dir) / "repo"
            repo.mkdir()

            with _running_server(kb, repo) as base_url:
                created = _post_json(
                    f"{base_url}/api/evaluations/answer-question",
                    {
                        "query": "NVDA margin",
                        "role_id": "sample-investor",
                        "symbol": "NVDA",
                        "expected_role_id": "sample-investor",
                        "source_url": "https://example.com/regression/nvda-margin",
                        "rationale": "Protects a fixed answer route.",
                        "updated_by": "local-ui",
                        "min_evidence": 1,
                    },
                )
                updated = _post_json(
                    f"{base_url}/api/evaluations/answer-question",
                    {
                        "id": created["question"]["id"],
                        "query": "NVDA margin",
                        "role_id": "sample-investor",
                        "symbol": "NVDA",
                        "topic": "margins",
                        "expected_role_id": "sample-investor",
                        "source_url": "https://example.com/regression/nvda-margin-v2",
                        "rationale": "Protects a fixed answer route and evidence threshold.",
                        "updated_by": "local-ui",
                        "min_evidence": 2,
                    },
                )
                deleted = _post_json(
                    f"{base_url}/api/evaluations/answer-question/delete",
                    {"id": created["question"]["id"], "updated_by": "local-ui"},
                )

            self.assertEqual(created["change"]["action"], "create")
            self.assertEqual(created["question"]["source_url"], "https://example.com/regression/nvda-margin")
            self.assertEqual(updated["change"]["action"], "update")
            self.assertEqual(updated["question"]["rationale"], "Protects a fixed answer route and evidence threshold.")
            self.assertEqual(deleted["change"]["action"], "delete")
            self.assertEqual([change["action"] for change in deleted["changes"]["changes"]], ["create", "update", "delete"])
            self.assertEqual(deleted["ui"]["data"]["answer_regression"]["recent_changes"][-1]["action"], "delete")

    def test_evaluations_suite_endpoints_export_and_import_batches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            repo = Path(temp_dir) / "repo"
            repo.mkdir()

            with _running_server(kb, repo) as base_url:
                _post_json(
                    f"{base_url}/api/evaluations/answer-question",
                    {
                        "query": "NVDA margin",
                        "role_id": "sample-investor",
                        "symbol": "NVDA",
                        "expected_role_id": "sample-investor",
                        "source_url": "https://example.com/regression/nvda-margin",
                        "rationale": "Protects an exported fixed question.",
                        "updated_by": "local-ui",
                        "min_evidence": 1,
                    },
                )
                exported = _get_json(f"{base_url}/api/evaluations/answer-suite/export")
                import_payload = {
                    "suite": {
                        **exported,
                        "questions": [
                            {
                                **exported["questions"][0],
                                "topic": "margins",
                                "rationale": "Protects an imported fixed question.",
                            }
                        ],
                    },
                    "dry_run": True,
                    "updated_by": "local-ui",
                }
                dry_run = _post_json(f"{base_url}/api/evaluations/answer-suite/import", import_payload)
                import_payload["dry_run"] = False
                applied = _post_json(f"{base_url}/api/evaluations/answer-suite/import", import_payload)

            self.assertTrue(exported["ok"])
            self.assertEqual(exported["question_count"], 1)
            self.assertFalse(dry_run["applied"])
            self.assertEqual(dry_run["summary"]["update"], 1)
            self.assertTrue(applied["applied"])
            self.assertEqual(applied["summary"]["update"], 1)
            self.assertEqual(applied["suite"]["questions"][0]["topic"], "margins")
            self.assertEqual(applied["ui"]["data"]["answer_regression"]["recent_changes"][-1]["action"], "import_update")


class _running_server:
    def __init__(self, kb, repo_root: Path, *, app_database=None) -> None:
        self._server = create_server(
            kb, host="127.0.0.1", port=0, repo_root=repo_root, app_database=app_database
        )
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> str:
        self._thread.start()
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def __exit__(self, exc_type, exc, tb) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _get_json(url: str) -> dict:
    with urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_json(url: str, payload: dict) -> dict:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_local_answer(kb, query: str, *, role_id: str) -> dict:
    from voicevault.answer import answer_query

    return answer_query(kb, query, role_id=role_id, limit=2)


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
