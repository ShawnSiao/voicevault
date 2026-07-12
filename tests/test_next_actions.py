from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from voicevault.answer import answer_query, default_answer_dir, write_answer_outputs
from voicevault.comparison import compare_roles, default_comparison_dir, review_comparison_export, write_comparison_outputs
from voicevault.importers import load_statements_from_kb
from voicevault.index import VoiceVaultIndex
from voicevault.kb import init_kb
from voicevault.next_actions import build_research_action_audit, build_research_next_actions


class ResearchNextActionsTests(unittest.TestCase):
    def test_completed_answer_and_comparison_do_not_repeat_as_next_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            _add_growth_role(kb)
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            initial_actions = build_research_next_actions(kb)
            answer_action = next(item for item in initial_actions if item["action_type"] == "answer")
            compare_action = next(item for item in initial_actions if item["action_type"] == "compare")
            query = answer_action["payload"]["query"]

            answer = answer_query(
                kb,
                query,
                role_id=answer_action["payload"]["role_id"],
                symbol=answer_action["payload"]["symbol"],
                topic=answer_action["payload"]["topic"],
                limit=answer_action["payload"]["limit"],
            )
            write_answer_outputs(default_answer_dir(kb, query), answer)
            comparison = compare_roles(
                kb,
                compare_action["payload"]["query"],
                roles=compare_action["payload"]["roles"],
                symbol=compare_action["payload"]["symbol"],
                topic=compare_action["payload"]["topic"],
                limit=compare_action["payload"]["limit"],
                evidence_limit=compare_action["payload"]["evidence_limit"],
            )
            output = write_comparison_outputs(default_comparison_dir(kb, query), comparison)
            review_comparison_export(output["comparison_json"], status="adopted", reviewer="test", notes="complete")

            next_actions = build_research_next_actions(kb)
            repeated = [
                item
                for item in next_actions
                if item["action_type"] in {"answer", "compare"} and item["payload"].get("query") == query
            ]

            self.assertEqual(repeated, [])

    def test_completed_answer_and_comparison_are_visible_in_action_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            _add_growth_role(kb)
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            initial_actions = build_research_next_actions(kb)
            answer_action = next(item for item in initial_actions if item["action_type"] == "answer")
            compare_action = next(item for item in initial_actions if item["action_type"] == "compare")
            query = answer_action["payload"]["query"]

            answer = answer_query(
                kb,
                query,
                role_id=answer_action["payload"]["role_id"],
                symbol=answer_action["payload"]["symbol"],
                topic=answer_action["payload"]["topic"],
                limit=answer_action["payload"]["limit"],
            )
            answer_output = write_answer_outputs(default_answer_dir(kb, query), answer)
            comparison = compare_roles(
                kb,
                compare_action["payload"]["query"],
                roles=compare_action["payload"]["roles"],
                symbol=compare_action["payload"]["symbol"],
                topic=compare_action["payload"]["topic"],
                limit=compare_action["payload"]["limit"],
                evidence_limit=compare_action["payload"]["evidence_limit"],
            )
            comparison_output = write_comparison_outputs(default_comparison_dir(kb, query), comparison)
            reviewed = review_comparison_export(
                comparison_output["comparison_json"],
                status="adopted",
                reviewer="test",
                notes="complete",
            )

            audit = build_research_action_audit(kb)
            completed = [
                item
                for item in audit["completed_actions"]
                if item["payload"].get("query") == query and item["action_type"] in {"answer", "compare"}
            ]

            self.assertEqual(audit["schema_version"], 1)
            self.assertEqual(audit["summary"]["completed"], 2)
            self.assertEqual(audit["summary"]["ready"], len(build_research_next_actions(kb)))
            self.assertEqual({item["action_type"] for item in completed}, {"answer", "compare"})
            answer_completed = next(item for item in completed if item["action_type"] == "answer")
            compare_completed = next(item for item in completed if item["action_type"] == "compare")
            self.assertEqual(answer_completed["status"], "completed")
            self.assertEqual(answer_completed["completed_by"]["kind"], "answer_export")
            self.assertEqual(answer_completed["completed_by"]["path"], str(answer_output["answer_json"]))
            self.assertEqual(compare_completed["completed_by"]["kind"], "comparison_export")
            self.assertEqual(compare_completed["completed_by"]["path"], reviewed["comparison_json"])
            self.assertIn("deliverable answer export", answer_completed["reason"])
            self.assertIn("deliverable comparison export", compare_completed["reason"])

    def test_ready_next_actions_include_audit_explanation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            _add_growth_role(kb)
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            action = next(item for item in build_research_next_actions(kb) if item["action_type"] == "answer")

            self.assertEqual(action["audit"]["state"], "recommended")
            self.assertEqual(action["audit"]["trigger"], "latest_statement")
            self.assertEqual(action["audit"]["completion"]["status"], "pending")
            self.assertEqual(action["audit"]["completion_key"], f"answer:{action['payload']['query'].strip().lower()}")


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
