from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from voicevault.importers import load_statements_from_kb
from voicevault.index import VoiceVaultIndex
from voicevault.kb import init_kb
from voicevault.models import Statement
from voicevault.role_agent import (
    ask_role_agent,
    audit_role_agent_exports,
    audit_role_agent_readiness,
    build_role_agent_prompt,
    inspect_role_agent_runtime,
)
from voicevault.role_skill import audit_role_skill_coverage, distill_role_skill, list_role_skills, write_role_skill


class RoleSkillTests(unittest.TestCase):
    def test_distill_role_skill_writes_a_reusable_agent_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            skill = distill_role_skill(kb, "sample-investor", limit=5)
            output = write_role_skill(kb, skill)
            listed = list_role_skills(kb)
            coverage = audit_role_skill_coverage(kb)

            self.assertEqual(skill["schema_version"], 1)
            self.assertEqual(skill["artifact_type"], "voicevault_role_skill")
            self.assertEqual(skill["role_id"], "sample-investor")
            self.assertGreaterEqual(skill["source_statement_count"], 1)
            self.assertTrue(skill["knowledge_system"]["focus_areas"])
            self.assertTrue(skill["knowledge_system"]["decision_frameworks"])
            self.assertTrue(skill["knowledge_system"]["style_markers"])
            self.assertIn("framework_projection", skill["answer_policy"]["required_sections"])
            self.assertIn("evidence_backed_claims", skill["answer_policy"]["required_sections"])
            self.assertIn("Do not claim to be the public role", skill["prompt_contract"]["system"])
            self.assertTrue(skill["source_statements"])
            self.assertTrue(output["skill_json"].is_file())
            self.assertTrue(output["skill_markdown"].is_file())
            persisted = json.loads(output["skill_json"].read_text(encoding="utf-8"))
            markdown = output["skill_markdown"].read_text(encoding="utf-8")
            self.assertEqual(persisted["role_id"], "sample-investor")
            self.assertIn("## Role Skill", markdown)
            self.assertIn("## Decision Frameworks", markdown)
            self.assertEqual(listed["summary"]["ready"], 1)
            self.assertEqual(listed["skills"][0]["role_id"], "sample-investor")
            self.assertTrue(coverage["ok"])
            self.assertEqual(coverage["summary"]["missing"], 0)

    def test_role_skill_coverage_requires_ready_roles_to_have_skill_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            coverage = audit_role_skill_coverage(kb)

            self.assertFalse(coverage["ok"])
            self.assertEqual(coverage["summary"]["ready_roles"], 1)
            self.assertEqual(coverage["summary"]["missing"], 1)
            self.assertEqual(coverage["missing_roles"][0]["role_id"], "sample-investor")
            self.assertIn("voicevault role distill", coverage["missing_roles"][0]["remediation"])

    def test_distill_role_skill_filters_capture_noise_and_extracts_chinese_concepts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            statements = [
                Statement(
                    statement_id="stmt_role_noise_1",
                    role_id="macro-role",
                    source_type="post",
                    source_url="https://example.com/1",
                    published_at="2026-05-27T10:00:00+08:00",
                    captured_at="2026-05-27T10:05:00+08:00",
                    title="置顶帖早已说明一切",
                    body=(
                        "置顶帖早已说明一切。流动性辩证分析。货币与信用。"
                        "基于人工智能的第四次工业革命，创造性破坏推动发展。"
                        "产业思维，底线思维，风险管理。"
                        "图片: https://xqimg.imedao.com/ugc/images/face/emoji_76_rich.png?v=1 网页链接"
                    ),
                    symbols=["NVDA"],
                    topics=["macro", "risk-management", "industrial-policy"],
                    stance="mixed",
                    time_horizon="long_term",
                    confidence="medium",
                    notes="",
                ),
                Statement(
                    statement_id="stmt_role_noise_2",
                    role_id="macro-role",
                    source_type="post",
                    source_url="https://example.com/2",
                    published_at="2026-05-28T10:00:00+08:00",
                    captured_at="2026-05-28T10:05:00+08:00",
                    title="泡沫行情考验交易能力",
                    body=(
                        "宏观流动性的改变，会影响市场对泡沫和资产价格的容忍度。"
                        "买是底线思维，不是股价思维；交易体系要先卸除杠杆。"
                        "https://assets.example.com/noisy.jpeg!800.jpg"
                    ),
                    symbols=["NVDA"],
                    topics=["macro", "market-cycle", "risk-management"],
                    stance="unclear",
                    time_horizon="long_term",
                    confidence="medium",
                    notes="",
                ),
            ]
            VoiceVaultIndex(kb).rebuild(statements)

            skill = distill_role_skill(kb, "macro-role")

            knowledge = skill["knowledge_system"]
            distilled_terms = json.dumps(
                knowledge["common_terms"] + knowledge["role_concepts"],
                ensure_ascii=False,
            ).lower()
            self.assertIn("产业思维", distilled_terms)
            self.assertIn("底线思维", distilled_terms)
            self.assertIn("宏观流动性", distilled_terms)
            for noisy_fragment in ["http", "xqimg", "imedao", "emoji_76", "jpeg", "png", "网页链接"]:
                self.assertNotIn(noisy_fragment, distilled_terms)

    def test_build_role_agent_prompt_uses_skill_and_evidence_without_copy_only_answering(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            write_role_skill(kb, distill_role_skill(kb, "sample-investor"))

            prompt = build_role_agent_prompt(
                kb,
                "sample-investor",
                "How would this role reason about NVDA margin pressure?",
                symbol="NVDA",
                limit=3,
            )

            self.assertEqual(prompt["schema_version"], 1)
            self.assertEqual(prompt["answer_type"], "role_agent_prompt")
            self.assertEqual(prompt["role_id"], "sample-investor")
            self.assertTrue(prompt["coverage"]["skill_ready"])
            self.assertGreaterEqual(prompt["coverage"]["evidence_count"], 1)
            self.assertGreaterEqual(len(prompt["messages"]), 2)
            self.assertIn("Do not claim to be the public role", prompt["messages"][0]["content"])
            self.assertIn("framework_projection", prompt["messages"][1]["content"])
            self.assertIn("related_evidence", prompt["messages"][1]["content"])
            self.assertIn("不是复制粘贴证据", prompt["messages"][1]["content"])

    def test_ask_role_agent_can_dry_run_or_call_injected_external_llm_client(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            write_role_skill(kb, distill_role_skill(kb, "sample-investor"))

            dry_run = ask_role_agent(
                kb,
                "sample-investor",
                "How would this role think about NVDA margins?",
                symbol="NVDA",
                dry_run=True,
            )
            live = ask_role_agent(
                kb,
                "sample-investor",
                "How would this role think about NVDA margins?",
                symbol="NVDA",
                dry_run=False,
                llm_client=_FakeRoleAgentClient(),
                model="fake-model",
            )

            self.assertTrue(Path(dry_run["role_agent_json"]).is_file())
            self.assertEqual(dry_run["llm"]["status"], "not_called")
            self.assertIn("prompt_bundle", dry_run)
            self.assertEqual(live["llm"]["status"], "completed")
            self.assertEqual(live["answer"]["mode"], "external_llm_role_agent")
            self.assertIn("framework_inference", live["answer"])
            self.assertIn("evidence_backed_claims", live["answer"])

    def test_role_agent_audit_scores_completed_answers_and_failed_runtime_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            write_role_skill(kb, distill_role_skill(kb, "sample-investor"))

            live = ask_role_agent(
                kb,
                "sample-investor",
                "How would this role think about NVDA margins?",
                symbol="NVDA",
                dry_run=False,
                llm_client=_FakeRoleAgentClient(),
                model="fake-model",
            )
            with patch.dict(
                "os.environ",
                {
                    "VOICEVAULT_LLM_ENDPOINT": "",
                    "VOICEVAULT_LLM_BASE_URL": "",
                    "VOICEVAULT_LLM_API_KEY": "",
                    "VOICEVAULT_LLM_MODEL": "",
                },
                clear=False,
            ):
                failed = ask_role_agent(
                    kb,
                    "sample-investor",
                    "How would this role think about missing runtime config?",
                    symbol="NVDA",
                    dry_run=False,
                )
                runtime = inspect_role_agent_runtime()
            audit = audit_role_agent_exports(kb)

            self.assertTrue(Path(live["role_agent_json"]).is_file())
            self.assertTrue(Path(failed["role_agent_json"]).is_file())
            self.assertFalse(failed["ok"])
            self.assertEqual(failed["llm"]["status"], "failed")
            self.assertFalse(runtime["configured"])
            self.assertEqual(audit["summary"]["completed"], 1)
            self.assertEqual(audit["summary"]["deliverable"], 1)
            self.assertEqual(audit["summary"]["failed"], 1)
            self.assertEqual(audit["summary"]["invalid_completed"], 0)
            self.assertFalse(audit["ok"])
            self.assertTrue(any(item["quality_status"] == "deliverable" for item in audit["items"]))
            self.assertTrue(any(item["quality_status"] == "failed" for item in audit["items"]))

    def test_role_agent_readiness_tracks_prompt_ready_and_live_required_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            write_role_skill(kb, distill_role_skill(kb, "sample-investor"))

            ask_role_agent(
                kb,
                "sample-investor",
                "How would this role think about NVDA margins?",
                symbol="NVDA",
                dry_run=True,
            )
            prompt_ready = audit_role_agent_readiness(kb)
            live_required = audit_role_agent_readiness(kb, require_live=True)
            ask_role_agent(
                kb,
                "sample-investor",
                "How would this role think about NVDA margins?",
                symbol="NVDA",
                dry_run=False,
                llm_client=_FakeRoleAgentClient(),
                model="fake-model",
            )
            live_ready = audit_role_agent_readiness(kb, require_live=True)

            self.assertTrue(prompt_ready["ok"])
            self.assertFalse(prompt_ready["live_ok"])
            self.assertEqual(prompt_ready["summary"]["roles_prompt_ready"], 1)
            self.assertEqual(prompt_ready["summary"]["roles_live_ready"], 0)
            self.assertEqual(prompt_ready["summary"]["roles_missing_live"], 1)
            self.assertEqual(prompt_ready["roles"][0]["status"], "prompt_ready")
            self.assertIn("voicevault role ask", prompt_ready["roles"][0]["remediation"][0])
            self.assertFalse(live_required["ok"])
            self.assertFalse(live_required["live_ok"])
            self.assertEqual(live_required["summary"]["roles_blocked_runtime"], 1)
            self.assertEqual(live_required["roles"][0]["status"], "blocked_runtime")
            self.assertTrue(live_ready["ok"])
            self.assertTrue(live_ready["live_ok"])
            self.assertEqual(live_ready["summary"]["roles_live_ready"], 1)
            self.assertEqual(live_ready["roles"][0]["status"], "live_ready")


class _FakeRoleAgentClient:
    def complete(self, *, messages, model: str, temperature: float) -> dict:
        return {
            "mode": "external_llm_role_agent",
            "answer": "该角色会先拆分需求、利润率和估值边界，再给出条件化判断。",
            "evidence_backed_claims": [{"text": "本地材料讨论了 NVDA margin。", "refs": ["[1]"]}],
            "framework_inference": "在没有逐字观点时，用已蒸馏的框架推演，而不是复制材料。",
            "uncertainty": ["这不是本人实时观点。"],
            "model": model,
            "message_count": len(messages),
        }


if __name__ == "__main__":
    unittest.main()
