from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from voicevault.cli import main
from voicevault.guide import build_quickstart_guide, write_quickstart_guide
from voicevault.kb import init_kb


class QuickstartGuideTests(unittest.TestCase):
    def test_build_quickstart_guide_returns_operator_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")

            guide = build_quickstart_guide(kb, repo_root=repo)

            self.assertEqual(guide["schema_version"], 1)
            self.assertEqual(guide["product"]["english_name"], "VoiceVault")
            self.assertEqual(guide["product"]["chinese_name"], "声迹")
            self.assertEqual(guide["knowledge_base"], str(kb.root))
            self.assertEqual(guide["repo_root"], str(repo))
            self.assertIsInstance(guide["release_ready"], bool)
            phase_ids = [phase["id"] for phase in guide["phases"]]
            self.assertEqual(
                phase_ids,
                [
                    "setup",
                    "capture",
                    "source_jobs",
                    "research_outputs",
                    "release_handoff",
                    "post_handoff_verify",
                ],
            )
            commands = "\n".join(command for phase in guide["phases"] for command in phase["commands"])
            self.assertIn("voicevault doctor --kb", commands)
            self.assertIn("voicevault capture append", commands)
            self.assertIn("voicevault sources drain", commands)
            self.assertIn("voicevault profile generate", commands)
            self.assertIn("voicevault analyze --kb", commands)
            self.assertIn("voicevault analyses list --kb", commands)
            self.assertIn("voicevault answer --kb", commands)
            self.assertIn("voicevault collect --kb", commands)
            self.assertIn("voicevault release ship", commands)
            self.assertIn("voicevault release verify", commands)
            self.assertTrue(guide["data_boundary"])
            self.assertTrue(guide["next_actions"])

    def test_write_quickstart_guide_outputs_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")

            result = write_quickstart_guide(kb, repo_root=repo)
            payload = json.loads(Path(result["guide_json"]).read_text(encoding="utf-8"))
            markdown = Path(result["guide_markdown"]).read_text(encoding="utf-8")

            self.assertTrue(result["ok"])
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["repo_root"], str(repo))
            self.assertIn("# VoiceVault Quickstart Guide", markdown)
            self.assertIn("release ship", markdown)
            self.assertIn("release verify", markdown)

    def test_guide_quickstart_json_outputs_written_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(["guide", "quickstart", "--kb", str(kb.root), "--root", str(repo), "--json"])

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertTrue(Path(payload["guide_json"]).is_file())
            self.assertTrue(Path(payload["guide_markdown"]).is_file())
            self.assertEqual(payload["guide"]["repo_root"], str(repo))


def _write_minimal_repo(repo: Path) -> None:
    (repo / "src" / "voicevault").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname = \"voicevault\"\n", encoding="utf-8")
    (repo / "README.md").write_text("# VoiceVault\n", encoding="utf-8")
    (repo / "src" / "voicevault" / "__init__.py").write_text("__version__ = \"fixture\"\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
