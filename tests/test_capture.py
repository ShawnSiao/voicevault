from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from voicevault.capture import append_capture_record, build_capture_record
from voicevault.kb import init_kb
from voicevault.sync import sync_once, validate_capture_path


class CaptureAdapterTests(unittest.TestCase):
    def test_append_capture_record_writes_valid_jsonl_for_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            capture_path = kb.inbox_captures_dir / "manual.jsonl"

            record = build_capture_record(
                role_id="adapter-source",
                platform="x",
                text="NVDA adapter capture should sync into Obsidian.",
                url="https://x.com/adapter/status/1",
                title="Adapter capture",
                author="Adapter Source",
                platform_user_id="adapter",
                published_at="2026-05-31T10:00:00Z",
                symbols=["NVDA"],
                topics=["adapter"],
                confidence="medium",
            )
            append_capture_record(capture_path, record)

            validation = validate_capture_path(capture_path)
            result = sync_once(kb, archive_processed=True)
            note_dir = kb.roles_dir / "adapter-source" / "statements" / "x"

            self.assertTrue(validation["ok"])
            self.assertEqual(validation["records"], 1)
            self.assertEqual(result.captures_seen, 1)
            self.assertEqual(result.notes_written, 1)
            self.assertEqual(len(result.archived_files), 1)
            self.assertTrue(note_dir.is_dir())

    def test_build_capture_record_requires_text(self) -> None:
        with self.assertRaises(ValueError):
            build_capture_record(role_id="adapter-source", platform="x", text="")

    def test_append_capture_record_uses_one_json_object_per_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            capture_path = Path(temp_dir) / "manual.jsonl"
            append_capture_record(
                capture_path,
                build_capture_record(role_id="source-a", platform="x", text="First capture."),
            )
            append_capture_record(
                capture_path,
                build_capture_record(role_id="source-b", platform="snowball", text="Second capture."),
            )

            records = [json.loads(line) for line in capture_path.read_text(encoding="utf-8").splitlines()]

            self.assertEqual([record["role_id"] for record in records], ["source-a", "source-b"])
            self.assertEqual(records[0]["platform"], "x")
            self.assertEqual(records[1]["platform"], "snowball")


if __name__ == "__main__":
    unittest.main()
