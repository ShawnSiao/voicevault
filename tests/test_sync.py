from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from voicevault.events import create_event
from voicevault.index import VoiceVaultIndex
from voicevault.importers import load_event, load_statements_from_kb
from voicevault.kb import init_kb
from voicevault.sync import read_capture_status, read_sync_status, sync_once, validate_capture_path


class SyncTests(unittest.TestCase):
    def test_sync_once_writes_capture_jsonl_to_obsidian_note_and_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            capture_path = kb.inbox_captures_dir / "x.jsonl"
            capture_path.write_text(
                json.dumps(
                    {
                        "role_id": "macro-commentator",
                        "platform": "x",
                        "platform_user_id": "macro_desk",
                        "author": "Macro Desk",
                        "url": "https://x.com/macro_desk/status/100",
                        "published_at": "2026-05-29T12:00:00Z",
                        "captured_at": "2026-05-30T01:00:00Z",
                        "title": "NVDA AI capex",
                        "text": "AI infrastructure demand remains durable for NVDA.",
                        "symbols": ["NVDA"],
                        "topics": ["ai-infrastructure"],
                        "stance": "bullish",
                        "time_horizon": "long_term",
                        "confidence": "medium",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            event_path = create_event(
                kb,
                event_id="2026-05-30-ai-capex",
                title="AI Capex",
                date="2026-05-30",
                symbols=["NVDA"],
                topics=["ai-infrastructure"],
                summary="Investors debate AI infrastructure durability.",
            )

            result = sync_once(kb)
            note_paths = list((kb.roles_dir / "macro-commentator" / "statements" / "x").glob("*.md"))
            statements = load_statements_from_kb(kb)
            relevant = VoiceVaultIndex(kb).query_relevant(load_event(event_path))

            self.assertEqual(result.captures_seen, 1)
            self.assertEqual(result.notes_written, 1)
            self.assertEqual(result.statements_indexed, len(statements))
            self.assertEqual(len(note_paths), 1)
            note_text = note_paths[0].read_text(encoding="utf-8")
            self.assertIn("source_platform: x", note_text)
            self.assertIn("source_user_id: macro_desk", note_text)
            self.assertIn("voicevault/platform/x", note_text)
            self.assertIn("macro-commentator", relevant)
            self.assertEqual(relevant["macro-commentator"][0].source_platform, "x")

    def test_sync_once_does_not_overwrite_existing_obsidian_note(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            capture_path = kb.inbox_captures_dir / "snowball.jsonl"
            capture_path.write_text(
                json.dumps(
                    {
                        "role_id": "value-writer",
                        "platform": "snowball",
                        "user_id": "value_01",
                        "display_name": "Value Writer",
                        "source_url": "https://xueqiu.com/value_01/100",
                        "published_at": "2026-05-28",
                        "captured_at": "2026-05-30T02:00:00Z",
                        "title": "Margin pressure",
                        "body": "Margin pressure matters when expectations are stretched.",
                        "symbols": "NVDA",
                        "topics": "earnings;valuation",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            first = sync_once(kb)
            note_path = next((kb.roles_dir / "value-writer" / "statements" / "snowball").glob("*.md"))
            note_path.write_text(
                note_path.read_text(encoding="utf-8") + "\n\n## Personal Annotation\n\nKeep this Obsidian edit.\n",
                encoding="utf-8",
                newline="\n",
            )
            second = sync_once(kb)

            self.assertEqual(first.notes_written, 1)
            self.assertEqual(second.captures_seen, 1)
            self.assertEqual(second.notes_written, 0)
            self.assertIn("Keep this Obsidian edit.", note_path.read_text(encoding="utf-8"))

    def test_sync_once_reports_duplicate_records_in_same_capture_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            capture_path = kb.inbox_captures_dir / "duplicates.jsonl"
            record = {
                "role_id": "duplicate-source",
                "platform": "x",
                "statement_id": "same-post",
                "url": "https://x.com/duplicate/status/1",
                "published_at": "2026-05-29",
                "text": "Duplicate capture should be observable without rewriting the note.",
                "symbols": ["NVDA"],
                "topics": ["duplicates"],
            }
            capture_path.write_text(
                json.dumps(record, ensure_ascii=False) + "\n" + json.dumps(record, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            result = sync_once(kb)
            status = read_capture_status(kb)

            self.assertEqual(result.captures_seen, 2)
            self.assertEqual(result.notes_written, 1)
            self.assertEqual(result.duplicates_skipped, 1)
            self.assertEqual(result.capture_files[0]["records_seen"], 2)
            self.assertEqual(result.capture_files[0]["notes_written"], 1)
            self.assertEqual(result.capture_files[0]["duplicates_skipped"], 1)
            self.assertEqual(status["summary"]["duplicates_skipped"], 1)

    def test_sync_once_reports_existing_statement_as_duplicate_on_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            capture_path = kb.inbox_captures_dir / "retry.jsonl"
            capture_path.write_text(
                json.dumps(
                    {
                        "role_id": "retry-source",
                        "platform": "x",
                        "statement_id": "retry-post",
                        "url": "https://x.com/retry/status/1",
                        "published_at": "2026-05-29",
                        "text": "Retrying the same capture should show a duplicate skip.",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            first = sync_once(kb)
            second = sync_once(kb)

            self.assertEqual(first.notes_written, 1)
            self.assertEqual(first.duplicates_skipped, 0)
            self.assertEqual(second.captures_seen, 1)
            self.assertEqual(second.notes_written, 0)
            self.assertEqual(second.duplicates_skipped, 1)
            self.assertEqual(second.capture_files[0]["duplicates_skipped"], 1)

    def test_sync_once_records_bad_capture_file_without_blocking_good_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            (kb.inbox_captures_dir / "bad.jsonl").write_text("{not-json}\n", encoding="utf-8")
            (kb.inbox_captures_dir / "good.jsonl").write_text(
                json.dumps(
                    {
                        "role_id": "quality-source",
                        "platform": "x",
                        "url": "https://x.com/quality/status/1",
                        "published_at": "2026-05-29",
                        "text": "NVDA margin debate has enough context for indexing.",
                        "symbols": ["NVDA"],
                        "topics": ["margins"],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            result = sync_once(kb)
            status = read_sync_status(kb)

            self.assertEqual(result.captures_seen, 1)
            self.assertEqual(result.notes_written, 1)
            self.assertEqual(len(result.errors), 1)
            self.assertIn("bad.jsonl", result.errors[0]["source_file"])
            self.assertTrue((kb.roles_dir / "quality-source" / "statements" / "x").is_dir())
            self.assertEqual(status["last_result"]["errors"], result.errors)
            self.assertEqual(status["last_result"]["captures_seen"], 1)

    def test_sync_once_archives_successful_capture_file_and_records_capture_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            capture_path = kb.inbox_captures_dir / "batch.jsonl"
            capture_path.write_text(
                json.dumps(
                    {
                        "role_id": "archive-source",
                        "platform": "x",
                        "url": "https://x.com/archive/status/1",
                        "published_at": "2026-05-30",
                        "text": "NVDA demand archive test.",
                        "symbols": ["NVDA"],
                        "topics": ["demand"],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            result = sync_once(kb, archive_processed=True)
            status = read_capture_status(kb)

            self.assertEqual(result.captures_seen, 1)
            self.assertEqual(len(result.archived_files), 1)
            self.assertFalse(capture_path.exists())
            archived_path = Path(result.archived_files[0])
            self.assertTrue(archived_path.is_file())
            self.assertIn("archive", archived_path.parts)
            self.assertEqual(status["summary"]["processed"], 1)
            self.assertEqual(status["summary"]["failed"], 0)
            self.assertEqual(status["files"][0]["status"], "processed")
            self.assertEqual(status["files"][0]["records_seen"], 1)
            self.assertEqual(status["files"][0]["archived_to"], str(archived_path))

    def test_sync_once_keeps_failed_capture_in_inbox_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            bad_path = kb.inbox_captures_dir / "bad.jsonl"
            bad_path.write_text("{bad-json}\n", encoding="utf-8")

            result = sync_once(kb, archive_processed=True)
            status = read_capture_status(kb)

            self.assertEqual(result.archived_files, [])
            self.assertTrue(bad_path.exists())
            self.assertEqual(status["summary"]["processed"], 0)
            self.assertEqual(status["summary"]["failed"], 1)
            self.assertEqual(status["files"][0]["status"], "failed")
            self.assertIn("invalid JSONL record", status["files"][0]["error"])

    def test_validate_capture_path_reports_records_without_writing_to_kb(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            capture_path = Path(temp_dir) / "valid.jsonl"
            capture_path.write_text(
                json.dumps(
                    {
                        "role_id": "validator-source",
                        "platform": "x",
                        "url": "https://x.com/validator/status/1",
                        "text": "Capture validation should not write notes.",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            report = validate_capture_path(capture_path)

            self.assertTrue(report["ok"])
            self.assertEqual(report["records"], 1)
            self.assertEqual(report["errors"], [])
            self.assertEqual(report["path"], str(capture_path))

    def test_validate_capture_path_reports_invalid_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            capture_path = Path(temp_dir) / "bad.jsonl"
            capture_path.write_text("{bad-json}\n", encoding="utf-8")

            report = validate_capture_path(capture_path)

            self.assertFalse(report["ok"])
            self.assertEqual(report["records"], 0)
            self.assertIn("invalid JSONL record", report["errors"][0])


if __name__ == "__main__":
    unittest.main()
