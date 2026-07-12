from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from voicevault.events import create_event, list_events
from voicevault.importers import load_event
from voicevault.kb import init_kb


class EventTemplateTests(unittest.TestCase):
    def test_create_event_writes_parseable_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")

            path = create_event(
                kb,
                event_id="2026-05-30-nvda-earnings",
                title="NVIDIA Earnings",
                date="2026-05-30",
                symbols=["NVDA"],
                topics=["earnings", "ai-infrastructure"],
                summary="NVIDIA beat revenue expectations, but margin guidance softened.",
            )
            event = load_event(path)

            self.assertTrue(path.is_file())
            self.assertEqual(event.event_id, "2026-05-30-nvda-earnings")
            self.assertEqual(event.title, "NVIDIA Earnings")
            self.assertEqual(event.symbols, ["NVDA"])
            self.assertEqual(event.topics, ["earnings", "ai-infrastructure"])
            self.assertIn("margin guidance", event.summary)

    def test_create_event_refuses_to_overwrite_existing_file_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            create_event(kb, "duplicate", "First", "2026-05-30", [], [], "First")

            with self.assertRaises(FileExistsError):
                create_event(kb, "duplicate", "Second", "2026-05-30", [], [], "Second")

    def test_list_events_returns_parseable_event_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            create_event(
                kb,
                event_id="2026-05-30-nvda-earnings",
                title="NVIDIA Earnings",
                date="2026-05-30",
                symbols=["NVDA"],
                topics=["earnings"],
                summary="Margin guidance softened.",
            )

            events = list_events(kb)

            self.assertEqual([event["event_id"] for event in events], ["2026-05-30-nvda-earnings", "example-nvda-margin"])
            self.assertEqual(events[0]["title"], "NVIDIA Earnings")
            self.assertEqual(events[0]["symbols"], ["NVDA"])
            self.assertTrue(events[0]["path"].endswith("2026-05-30-nvda-earnings.md"))


if __name__ == "__main__":
    unittest.main()
