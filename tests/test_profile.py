from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from voicevault.importers import load_statements_from_kb
from voicevault.index import VoiceVaultIndex
from voicevault.kb import init_kb
from voicevault.profile import generate_profile, promote_generated_profile


class ProfileGenerationTests(unittest.TestCase):
    def test_generate_profile_writes_generated_file_without_overwriting_reviewed_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            reviewed_profile = kb.roles_dir / "sample-investor" / "profile.md"
            reviewed_profile.write_text("reviewed profile", encoding="utf-8")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            generated_path = generate_profile(kb, "sample-investor")

            self.assertEqual(reviewed_profile.read_text(encoding="utf-8"), "reviewed profile")
            self.assertTrue(generated_path.is_file())
            generated = generated_path.read_text(encoding="utf-8")
            self.assertIn("## Focus Areas", generated)
            self.assertIn("## Representative Views", generated)
            self.assertIn("profile_status: generated_unreviewed", generated)

    def test_promote_generated_profile_creates_reviewed_profile_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            reviewed_profile = kb.roles_dir / "sample-investor" / "profile.md"
            reviewed_profile.unlink()
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            generated_path = generate_profile(kb, "sample-investor")

            promoted_path = promote_generated_profile(
                kb,
                "sample-investor",
                reviewer="codex-product-review",
                review_note="Reviewed generated profile against sample evidence.",
            )

            self.assertEqual(promoted_path, reviewed_profile)
            self.assertTrue(generated_path.is_file())
            promoted = promoted_path.read_text(encoding="utf-8")
            self.assertIn("profile_status: reviewed", promoted)
            self.assertIn("reviewed_at:", promoted)
            self.assertIn("reviewed_by: codex-product-review", promoted)
            self.assertIn("review_note: Reviewed generated profile against sample evidence.", promoted)

            with self.assertRaises(FileExistsError):
                promote_generated_profile(kb, "sample-investor")

    def test_promote_generated_profile_requires_generated_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")

            with self.assertRaises(FileNotFoundError):
                promote_generated_profile(kb, "sample-investor")


if __name__ == "__main__":
    unittest.main()
