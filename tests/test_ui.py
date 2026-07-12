from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from voicevault.analysis import analyze_event
from voicevault.action_runs import record_action_run
from voicevault.accounts import create_account
from voicevault.comparison import compare_roles, default_comparison_dir, review_comparison_export, write_comparison_outputs
from voicevault.exporters import write_analysis_outputs
from voicevault.importers import load_event, load_statements_from_kb
from voicevault.index import VoiceVaultIndex
from voicevault.kb import init_kb
from voicevault.role_skill import distill_role_skill, write_role_skill
from voicevault.source_imports import import_source_input
from voicevault.source_jobs import complete_source_job, enqueue_source_jobs
from voicevault.sources import create_source, run_source
from voicevault.sync import sync_once
from voicevault.ui import build_ui_data, write_local_ui


class LocalUiTests(unittest.TestCase):
    def test_build_ui_data_contains_real_knowledge_base_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            (kb.inbox_captures_dir / "ui.jsonl").write_text(
                json.dumps(
                    {
                        "role_id": "ui-source",
                        "platform": "x",
                        "url": "https://x.com/ui/status/1",
                        "published_at": "2026-05-31T09:00:00Z",
                        "text": "UI timeline statement should come from the real knowledge base.",
                        "symbols": ["NVDA"],
                        "topics": ["ui"],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            answer_dir = kb.exports_dir / "answers" / "ui-answer"
            answer_dir.mkdir(parents=True)
            (answer_dir / "answer.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "answer_type": "local_evidence_answer",
                        "query": "UI answer",
                        "filters": {"role_id": "", "symbol": "", "topic": "", "limit": 1},
                        "generated_at": "2026-05-31T01:00:00Z",
                        "answer_language": "zh-CN",
                        "confidence": "medium",
                        "coverage": {"evidence_count": 1, "total_matches": 1, "role_count": 1},
                        "citations": [
                            {
                                "ref": "[1]",
                                "statement_id": "ui-source-1",
                                "role_id": "ui-source",
                                "title": "UI answer evidence",
                                "source_url": "https://x.com/ui/status/1",
                                "published_at": "2026-05-31T09:00:00Z",
                                "source_platform": "x",
                            }
                        ],
                        "key_points": [{"text": "关键证据会进入 UI 数据。", "refs": ["[1]"]}],
                        "answer": "声迹在本地索引中找到 1 条可引用证据。",
                        "role_answer": {
                            "schema_version": 1,
                            "mode": "single_role",
                            "role_id": "ui-source",
                            "display_name": "UI Source",
                            "profile_status": "reviewed",
                            "source_scope": "local_public_statements_only",
                            "answer": "UI Source 的公开材料支持这条 UI answer。",
                            "evidence_refs": ["[1]"],
                            "limitations": ["该回答只归纳本地公开 statement。"],
                        },
                        "answer_markdown": "# 证据答案\n\n## 结论\n\n声迹在本地索引中找到 1 条可引用证据。\n",
                        "evidence": [
                            {
                                "ref": "[1]",
                                "statement_id": "ui-source-1",
                                "role_id": "ui-source",
                                "title": "UI answer evidence",
                                "source_url": "https://x.com/ui/status/1",
                                "published_at": "2026-05-31T09:00:00Z",
                                "captured_at": "2026-05-31T09:05:00Z",
                                "excerpt": "UI answer evidence should pass the v1 contract.",
                            }
                        ],
                        "uncertainty": ["该答案由本地规则生成。"],
                        "search": {"query": "UI answer", "total_matches": 1, "results": []},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            suite_path = kb.content_dir / "evaluations" / "questions.json"
            suite_path.parent.mkdir(parents=True, exist_ok=True)
            suite_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "questions": [
                            {
                                "id": "ui-answer",
                                "query": "UI answer",
                                "role_id": "ui-source",
                                "expected_role_id": "ui-source",
                                "min_evidence": 1,
                                "requires_role_answer": True,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            create_source(
                kb,
                source_id="x-ui-source",
                role_id="ui-source",
                platform="x",
                source_url="https://x.com/ui",
                display_name="UI Source",
                topics=["ui"],
            )
            create_account(
                kb,
                account_id="rss-ui-source",
                platform="rss",
                platform_account_id="ui-source",
                role_id="ui-source",
                display_name="UI Source RSS",
                collection_mode="blocked",
            )
            source_input = Path(temp_dir) / "ui-source-feed.jsonl"
            source_input.write_text(
                '{"text":"UI source import status should appear in product data.","source_url":"https://x.com/ui/status/2"}\n',
                encoding="utf-8",
            )
            import_source_input(kb, "x-ui-source", source_input)
            job = enqueue_source_jobs(kb, source_id="x-ui-source")["jobs"][0]
            source_run = run_source(
                kb,
                "x-ui-source",
                text="UI source dry-run should appear in source status.",
                dry_run=True,
            )
            complete_source_job(kb, job["job_id"], source_run["run"])
            sync_once(kb)
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            write_role_skill(kb, distill_role_skill(kb, "sample-investor"))
            comparison = compare_roles(kb, "NVDA margin", symbol="NVDA", roles="all", limit=3, evidence_limit=1)
            comparison_output = write_comparison_outputs(default_comparison_dir(kb, "NVDA margin"), comparison)
            review_comparison_export(
                comparison_output["comparison_json"],
                status="adopted",
                reviewer="codex-product-review",
                notes="UI comparison review is publishable.",
            )
            record_action_run(
                kb,
                action_type="answer",
                status="completed",
                payload={"query": "UI answer", "role_id": "sample-investor"},
                result={
                    "artifact_kind": "answer",
                    "artifact_path": str(answer_dir / "answer.json"),
                    "artifact_markdown": str(answer_dir / "answer.md"),
                    "evidence_count": 1,
                },
                source="local_api",
            )
            record_action_run(
                kb,
                action_type="answer",
                status="failed",
                payload={"query": "failed UI answer", "role_id": "sample-investor"},
                error="temporary UI failure",
                source="local_api",
            )
            event = load_event(kb.events_dir / "example-event.md")
            write_analysis_outputs(kb.exports_dir / "ui-analysis", analyze_event(kb, event, roles="all"))

            data = build_ui_data(kb)

            self.assertEqual(data["product"]["english_name"], "VoiceVault")
            self.assertEqual(data["schema_version"], 1)
            self.assertEqual(data["product"]["version"], "0.89.0")
            self.assertGreaterEqual(data["summary"]["roles"], 2)
            self.assertIn("role_coverage", data)
            self.assertIn("role_skills", data)
            self.assertIn("role_skill_coverage", data)
            self.assertIn("role_agent_exports", data)
            self.assertIn("role_agent_audit", data)
            self.assertIn("role_agent_runtime", data)
            self.assertIn("role_agent_readiness", data)
            self.assertIn("configured", data["role_agent_runtime"])
            self.assertFalse(data["role_coverage"]["ok"])
            self.assertEqual(data["summary"]["reviewed_roles_with_statements"], 1)
            self.assertGreaterEqual(data["summary"]["statements"], 1)
            self.assertIn("duplicates_skipped", data["capture_status"]["summary"])
            self.assertTrue(any(item["role_id"] == "ui-source" for item in data["roles"]))
            self.assertIn("answer_exports", data)
            self.assertIn("source_configs", data)
            self.assertIn("account_archives", data)
            self.assertIn("account_status", data)
            self.assertIn("source_status", data)
            self.assertIn("source_adapter_validation", data)
            self.assertIn("source_job_status", data)
            self.assertIn("source_import_status", data)
            self.assertIn("action_runs", data)
            self.assertIn("remediation_queue", data)
            self.assertIn("answer_quality", data)
            self.assertIn("answer_regression", data)
            self.assertIn("analysis_exports", data)
            self.assertIn("release_actions", data)
            self.assertIn("next_actions", data)
            self.assertIn("next_action_audit", data)
            self.assertIn("next_actions", data["summary"])
            self.assertIn("completed_next_actions", data["summary"])
            self.assertIn("action_runs", data["summary"])
            self.assertIn("action_run_failed", data["summary"])
            self.assertIn("remediation_items", data["summary"])
            self.assertIn("remediation_ready", data["summary"])
            self.assertIn("remediation_blocked", data["summary"])
            self.assertIn("answer_quality_passed", data["summary"])
            self.assertIn("answer_quality_review", data["summary"])
            self.assertIn("answer_quality_failed", data["summary"])
            self.assertIn("answer_regression_passed", data["summary"])
            self.assertIn("answer_regression_review", data["summary"])
            self.assertIn("answer_regression_failed", data["summary"])
            self.assertIn("answer_regression_questions", data["summary"])
            self.assertIn("answer_regression_min_questions", data["summary"])
            self.assertIn("answer_regression_missing_provenance", data["summary"])
            self.assertIn("role_skills", data["summary"])
            self.assertIn("role_skills_ready", data["summary"])
            self.assertIn("role_skills_missing", data["summary"])
            self.assertIn("role_agent_exports", data["summary"])
            self.assertIn("role_agent_completed", data["summary"])
            self.assertIn("role_agent_deliverable", data["summary"])
            self.assertIn("role_agent_failed", data["summary"])
            self.assertIn("role_agent_invalid_completed", data["summary"])
            self.assertIn("role_agent_roles_prompt_ready", data["summary"])
            self.assertIn("role_agent_roles_live_ready", data["summary"])
            self.assertIn("role_agent_roles_missing_live", data["summary"])
            self.assertEqual(data["summary"]["source_configs"], 2)
            self.assertEqual(data["summary"]["account_archives"], 1)
            self.assertEqual(data["summary"]["account_archives_blocked"], 1)
            self.assertEqual(data["account_archives"][0]["account_id"], "rss-ui-source")
            self.assertEqual(data["account_status"]["summary"]["blocked"], 1)
            self.assertGreaterEqual(data["summary"]["next_actions"], 1)
            self.assertEqual(data["summary"]["completed_next_actions"], len(data["next_action_audit"]["completed_actions"]))
            self.assertEqual(data["summary"]["analysis_exports"], 1)
            self.assertEqual(data["summary"]["source_runs"], 2)
            self.assertEqual(data["summary"]["source_run_failed"], 0)
            self.assertEqual(data["summary"]["source_adapter_failed"], 0)
            self.assertEqual(data["summary"]["source_imports"], 1)
            self.assertEqual(data["summary"]["source_import_failed"], 0)
            self.assertEqual(data["summary"]["source_jobs"], 1)
            self.assertEqual(data["summary"]["source_jobs_pending"], 0)
            self.assertEqual(data["summary"]["source_jobs_failed"], 0)
            self.assertEqual(data["summary"]["action_runs"], 2)
            self.assertEqual(data["summary"]["action_run_failed"], 1)
            self.assertEqual(data["action_runs"]["summary"]["completed"], 1)
            self.assertEqual(data["action_runs"]["summary"]["retryable_failed"], 1)
            self.assertEqual(data["action_runs"]["runs"][0]["action_type"], "answer")
            self.assertTrue(data["action_runs"]["runs"][0]["retryable"])
            self.assertGreaterEqual(data["summary"]["remediation_items"], 1)
            self.assertGreaterEqual(data["summary"]["remediation_ready"], 1)
            self.assertTrue(any(item["action_type"] == "retry_action_run" for item in data["remediation_queue"]["items"]))
            self.assertEqual(data["summary"]["comparison_exports"], 1)
            self.assertEqual(data["summary"]["adopted_comparison_exports"], 1)
            self.assertIn("x-ui-source", {item["source_id"] for item in data["source_configs"]})
            self.assertEqual(data["source_status"]["runs"][0]["status"], "dry_run")
            self.assertEqual(data["source_import_status"]["imports"][0]["source_id"], "x-ui-source")
            self.assertEqual(data["source_import_status"]["imports"][0]["status"], "ready")
            self.assertEqual(data["analysis_exports"][0]["event_id"], "example-nvda-margin")
            self.assertEqual(data["analysis_exports"][0]["role_summaries"][0]["role_id"], "sample-investor")
            self.assertGreaterEqual(len(data["analysis_exports"][0]["evidence_summaries"]), 1)
            self.assertTrue(data["analysis_exports"][0]["evidence_summaries"][0]["source_url"])
            self.assertEqual(data["source_job_status"]["jobs"][0]["status"], "completed")
            self.assertGreater(data["summary"]["release_blockers"], 0)
            self.assertGreaterEqual(len(data["release_actions"]), 1)
            self.assertTrue(
                {"status", "phase", "check_id", "action", "command"}.issubset(data["release_actions"][0]),
                data["release_actions"][0],
            )
            self.assertTrue(
                {"id", "phase", "action_type", "label", "action", "payload", "audit"}.issubset(
                    data["next_actions"][0]
                ),
                data["next_actions"][0],
            )
            self.assertEqual(data["next_actions"][0]["audit"]["state"], "recommended")
            self.assertEqual(data["next_action_audit"]["schema_version"], 1)
            self.assertIn("completed_actions", data["next_action_audit"])
            self.assertEqual(data["summary"]["deliverable_answer_exports"], 1)
            self.assertTrue(any(item["display_name"] == "Sample Investor" for item in data["roles"]))
            self.assertEqual(data["answer_exports"][0]["status"], "deliverable")
            self.assertEqual(data["answer_exports"][0]["key_points"][0]["text"], "关键证据会进入 UI 数据。")
            self.assertEqual(data["summary"]["answer_regression_passed"], 1)
            self.assertEqual(data["summary"]["answer_regression_questions"], 1)
            self.assertEqual(data["summary"]["answer_regression_min_questions"], 4)
            self.assertEqual(data["answer_regression"]["items"][0]["status"], "pass")
            self.assertEqual(data["summary"]["role_skills_ready"], 1)
            self.assertEqual(data["role_skills"]["skills"][0]["role_id"], "sample-investor")
            self.assertEqual(data["comparison_exports"][0]["review_status"], "adopted")
            self.assertEqual(data["comparison_exports"][0]["reviewer"], "codex-product-review")
            self.assertTrue(
                any("UI timeline statement" in item["body"] for item in data["statements"]),
                data["statements"],
            )

    def test_write_local_ui_outputs_html_and_data_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            sync_once(kb)

            html_path = write_local_ui(kb, repo_root=repo)
            data_path = html_path.with_name("data.json")
            payload = json.loads(data_path.read_text(encoding="utf-8"))

            self.assertEqual(html_path, kb.exports_dir / "ui" / "index.html")
            self.assertTrue(html_path.is_file())
            self.assertTrue(data_path.is_file())
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["product"]["version"], "0.89.0")
            self.assertIn("comparison_exports", payload)
            self.assertIn("role_coverage", payload)
            self.assertIn("role_skills", payload)
            self.assertIn("role_skill_coverage", payload)
            self.assertIn("role_agent_exports", payload)
            self.assertIn("role_agent_audit", payload)
            self.assertIn("role_agent_runtime", payload)
            self.assertIn("role_agent_readiness", payload)
            self.assertIn("comparison_exports", payload["summary"])
            self.assertIn("adopted_comparison_exports", payload["summary"])
            self.assertIn("reviewed_roles_with_statements", payload["summary"])
            self.assertIn("role_skills_ready", payload["summary"])
            self.assertIn("role_agent_exports", payload["summary"])
            self.assertIn("role_agent_deliverable", payload["summary"])
            self.assertIn("role_agent_roles_prompt_ready", payload["summary"])
            self.assertIn("role_agent_roles_live_ready", payload["summary"])
            self.assertIn("role_agent_roles_missing_live", payload["summary"])
            self.assertEqual(payload["repo_root"], str(repo.resolve()))
            html = html_path.read_text(encoding="utf-8")
            self.assertIn("data.json", html)
            self.assertIn('data-view="actions"', html)
            self.assertIn('id="actions"', html)
            self.assertIn('id="analysis"', html)
            self.assertIn('id="statements"', html)
            self.assertIn('id="answers"', html)
            self.assertIn('id="answerForm"', html)
            self.assertIn('id="answerQuery"', html)
            self.assertIn('id="answerCompare"', html)
            self.assertIn('id="onboardingForm"', html)
            self.assertIn('id="onboardingStatementForm"', html)
            self.assertIn('id="onboardingStatus"', html)
            self.assertIn("来源接入", html)
            self.assertIn("账户归档", html)
            self.assertIn('id="accountArchiveForm"', html)
            self.assertIn('id="accountArchivePlatform"', html)
            self.assertIn('id="accountArchiveAccount"', html)
            self.assertIn('id="accountArchiveFeedUrl"', html)
            self.assertIn('id="accountArchiveCollectForm"', html)
            self.assertIn("accountArchiveRows", html)
            self.assertIn("submitAccountArchive", html)
            self.assertIn("submitAccountCollect", html)
            self.assertIn("fetch('/api/accounts/create'", html)
            self.assertIn("fetch('/api/accounts/collect'", html)
            self.assertIn("更新档案", html)
            self.assertIn("fetch('/api/onboarding/role-source'", html)
            self.assertIn("fetch('/api/onboarding/statement'", html)
            self.assertIn("renderOnboardingResult", html)
            self.assertIn("submitOnboardingRoleSource", html)
            self.assertIn("submitOnboardingStatement", html)
            self.assertIn("自动选择角色", html)
            self.assertIn("auto_route", html)
            self.assertIn("fetch('/api/compare'", html)
            self.assertIn("fetch('/api/comparison/review'", html)
            self.assertIn("data-comparison-review", html)
            self.assertIn("review_status", html)
            self.assertIn("renderLiveComparison", html)
            self.assertIn("comparisonCard", html)
            self.assertIn("comparison_exports", html)
            self.assertIn("routeHints(answer)", html)
            self.assertIn("selected_role_id", html)
            self.assertIn("fetch('/api/answer'", html)
            self.assertIn("renderLiveAnswer", html)
            self.assertGreaterEqual(html.count("refreshUiFromPayload(payload);"), 5)
            self.assertIn("answerError", html)
            self.assertIn("发布行动", html)
            self.assertIn("renderActions", html)
            self.assertIn("releaseActionRows", html)
            self.assertIn("release_actions", html)
            self.assertIn("研究行动", html)
            self.assertIn("action-table-scroll", html)
            self.assertIn("已完成研究行动", html)
            self.assertIn("修复队列", html)
            self.assertIn("remediationRows", html)
            self.assertIn("data-remediation-item", html)
            self.assertIn("runRemediationItem", html)
            self.assertIn("nextActionRows", html)
            self.assertIn("completedActionRows", html)
            self.assertIn("data-next-action", html)
            self.assertIn("completed_by", html)
            self.assertIn("next_action_audit", html)
            self.assertIn("行动历史", html)
            self.assertIn("actionRunRows", html)
            self.assertIn("actionRunFilter", html)
            self.assertIn("data-action-run-status", html)
            self.assertIn("data-action-run-retry", html)
            self.assertIn("retryActionRun", html)
            self.assertIn("fetch('/api/action-runs/retry'", html)
            self.assertIn("action_runs", html)
            self.assertIn("runNextAction", html)
            self.assertIn("next_actions", html)
            self.assertIn("事件分析", html)
            self.assertIn("renderAnalysis", html)
            self.assertIn("analysisRoleRows", html)
            self.assertIn("analysisEvidenceRows", html)
            self.assertIn("证据", html)
            self.assertIn("证据回答", html)
            self.assertIn("来源配置", html)
            self.assertIn("来源适配器", html)
            self.assertIn("sourceAdapterRows", html)
            self.assertIn("来源运行", html)
            self.assertIn("sourceRunRows", html)
            self.assertIn("来源任务", html)
            self.assertIn("sourceJobRows", html)
            self.assertIn("来源导入", html)
            self.assertIn("sourceImportRows", html)
            self.assertIn("answerPoints(answer)", html)
            self.assertIn("roleAnswer(answer)", html)
            self.assertIn("角色回答", html)
            self.assertIn("角色代理", html)
            self.assertIn("角色技能", html)
            self.assertIn('id="roleAgentQuery"', html)
            self.assertIn('id="roleAgentCallLlm"', html)
            self.assertIn('id="roleAgentModel"', html)
            self.assertIn('id="roleAgentTemperature"', html)
            self.assertIn("fetch('/api/role/distill'", html)
            self.assertIn("fetch('/api/role/ask'", html)
            self.assertIn("dry_run: !callLlm", html)
            self.assertIn("role_agent_audit", html)
            self.assertIn("role_agent_runtime", html)
            self.assertIn("role_agent_readiness", html)
            self.assertIn("角色代理就绪状态", html)
            self.assertIn("renderRoleAgentResult", html)
            self.assertIn("回答质量", html)
            self.assertIn("answerQualityRows", html)
            self.assertIn("回答回归测试", html)
            self.assertIn("answerRegressionRows", html)
            self.assertIn("data-answer-regression-item", html)
            self.assertIn("handleAnswerRegressionClick", html)
            self.assertIn("runAnswerRegressionItem", html)
            self.assertIn('id="answerRegressionForm"', html)
            self.assertIn('id="regressionQuery"', html)
            self.assertIn('id="regressionSourceUrl"', html)
            self.assertIn('id="regressionRationale"', html)
            self.assertIn("回答回归变更", html)
            self.assertIn("回归覆盖", html)
            self.assertIn("answerRegressionExport", html)
            self.assertIn("answerRegressionImportPayload", html)
            self.assertIn("submitAnswerRegressionImport", html)
            self.assertIn("answerRegressionChangeRows", html)
            self.assertIn("source_url", html)
            self.assertIn("rationale", html)
            self.assertIn("updated_by", html)
            self.assertIn("submitAnswerRegressionQuestion", html)
            self.assertIn("deleteAnswerRegressionQuestion", html)
            self.assertIn("fetch('/api/evaluations/answer-question'", html)
            self.assertIn("fetch('/api/evaluations/answer-question/delete'", html)
            self.assertIn("fetch('/api/evaluations/answer-suite/export'", html)
            self.assertIn("fetch('/api/evaluations/answer-suite/import'", html)
            self.assertIn("data-answer-regression-delete", html)
            self.assertIn("answer.status", html)
            self.assertIn("role.display_name", html)
            self.assertIn("key-points", html)
            self.assertIn("角色覆盖", html)
            self.assertIn("role_coverage", html)
            self.assertIn("VoiceVault", html)


if __name__ == "__main__":
    unittest.main()
