from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from voicevault.cli import main
from voicevault.kb import init_kb
from voicevault.source_imports import (
    import_source_input,
    normalize_source_input,
    read_source_import_status,
    write_source_input_template,
)
from voicevault.sources import (
    create_source,
    list_sources,
    read_source_status,
    record_source_run_error,
    run_source,
    validate_source_adapters,
)
from voicevault.sync import sync_once, validate_capture_path


class SourceConfigTests(unittest.TestCase):
    def test_create_source_writes_config_and_list_sources_reads_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")

            created = create_source(
                kb,
                source_id="x-public-analyst",
                role_id="public-analyst",
                platform="x",
                source_url="https://x.com/public_analyst",
                display_name="Public Analyst on X",
                adapter="manual",
                symbols=["NVDA"],
                topics=["ai-infrastructure"],
                tags=["semiconductors", "macro"],
                cadence="manual",
                notes="公开来源，手工投递 capture。",
            )

            self.assertEqual(created["source_id"], "x-public-analyst")
            self.assertEqual(created["status"], "active")
            self.assertTrue(Path(created["config_path"]).is_file())

            sources = list_sources(kb)

            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0]["source_id"], "x-public-analyst")
            self.assertEqual(sources[0]["role_id"], "public-analyst")
            self.assertEqual(sources[0]["platform"], "x")
            self.assertEqual(sources[0]["symbols"], ["NVDA"])
            self.assertEqual(sources[0]["topics"], ["ai-infrastructure"])

    def test_create_source_stores_adapter_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")

            created = create_source(
                kb,
                source_id="local-jsonl-source",
                role_id="public-analyst",
                platform="x",
                adapter="local-jsonl",
                adapter_config={"input_path": "public-feed.jsonl"},
            )
            sources = list_sources(kb)

            self.assertEqual(created["adapter"], "local-jsonl")
            self.assertEqual(created["adapter_config"], {"input_path": "public-feed.jsonl"})
            self.assertEqual(sources[0]["adapter_config"]["input_path"], "public-feed.jsonl")

    def test_create_source_refuses_duplicate_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            create_source(kb, source_id="x-public-analyst", role_id="public-analyst", platform="x")

            with self.assertRaises(FileExistsError):
                create_source(kb, source_id="x-public-analyst", role_id="public-analyst", platform="x")

    def test_run_source_rejects_unsafe_source_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")

            with self.assertRaises(ValueError):
                run_source(kb, "..\\outside", text="Should not resolve outside source configs.")

    def test_run_source_dry_run_builds_capture_without_writing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            create_source(
                kb,
                source_id="x-public-analyst",
                role_id="public-analyst",
                platform="x",
                source_url="https://x.com/public_analyst",
                display_name="Public Analyst",
                symbols=["NVDA"],
                topics=["ai-infrastructure"],
            )

            result = run_source(
                kb,
                "x-public-analyst",
                text="NVDA AI infrastructure commentary from configured source.",
                title="NVDA AI demand",
                dry_run=True,
            )

            self.assertTrue(result["dry_run"])
            self.assertEqual(result["written"], 0)
            self.assertFalse(Path(result["capture_path"]).exists())
            self.assertEqual(result["record"]["role_id"], "public-analyst")
            self.assertEqual(result["record"]["platform"], "x")
            self.assertEqual(result["record"]["symbols"], ["NVDA"])
            self.assertEqual(result["record"]["topics"], ["ai-infrastructure"])

    def test_run_source_dry_run_records_source_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            create_source(
                kb,
                source_id="x-public-analyst",
                role_id="public-analyst",
                platform="x",
                source_url="https://x.com/public_analyst",
            )

            run_source(
                kb,
                "x-public-analyst",
                text="Dry-run capture should be recorded without writing a capture file.",
                title="Dry run",
                dry_run=True,
            )
            status = read_source_status(kb)

            self.assertTrue(status["ok"])
            self.assertEqual(status["summary"]["total"], 1)
            self.assertEqual(status["summary"]["dry_run"], 1)
            self.assertEqual(status["summary"]["failed"], 0)
            self.assertEqual(status["summary"]["active_without_runs"], 0)
            self.assertEqual(status["summary"]["active_failed_latest"], 0)
            self.assertTrue(Path(status["status_path"]).is_file())
            self.assertEqual(status["runs"][0]["source_id"], "x-public-analyst")
            self.assertEqual(status["runs"][0]["status"], "dry_run")
            self.assertEqual(status["sources"][0]["latest_run"]["status"], "dry_run")

    def test_run_local_jsonl_adapter_dry_run_reads_configured_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            input_path = Path(temp_dir) / "public-feed.jsonl"
            input_path.write_text(
                '{"text":"Adapter record from public JSONL.","title":"Adapter item","source_url":"https://x.com/public/status/1","symbols":["NVDA"],"topics":["adapter"]}\n',
                encoding="utf-8",
            )
            create_source(
                kb,
                source_id="local-jsonl-source",
                role_id="public-analyst",
                platform="x",
                adapter="local-jsonl",
                adapter_config={"input_path": str(input_path)},
                symbols=["MSFT"],
                topics=["fallback-topic"],
            )

            result = run_source(kb, "local-jsonl-source", dry_run=True)

            self.assertEqual(result["status"], "dry_run")
            self.assertEqual(result["record_count"], 1)
            self.assertEqual(result["written"], 0)
            self.assertFalse(Path(result["capture_path"]).exists())
            self.assertEqual(result["records"][0]["role_id"], "public-analyst")
            self.assertEqual(result["records"][0]["platform"], "x")
            self.assertEqual(result["records"][0]["title"], "Adapter item")
            self.assertEqual(result["records"][0]["symbols"], ["NVDA"])
            self.assertEqual(result["records"][0]["topics"], ["adapter"])

    def test_validate_source_adapters_reports_local_jsonl_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            input_path = Path(temp_dir) / "feed.jsonl"
            input_path.write_text('{"text":"Ready public record."}\n', encoding="utf-8")
            create_source(
                kb,
                source_id="ready-feed",
                role_id="public-analyst",
                platform="x",
                adapter="local-jsonl",
                adapter_config={"input_path": str(input_path)},
            )

            report = validate_source_adapters(kb)

            self.assertTrue(report["ok"])
            self.assertEqual(report["summary"]["total"], 1)
            self.assertEqual(report["summary"]["checked"], 1)
            self.assertEqual(report["summary"]["ready"], 1)
            self.assertEqual(report["summary"]["failed"], 0)
            self.assertEqual(report["sources"][0]["adapter"], "local-jsonl")
            self.assertEqual(report["sources"][0]["status"], "ready")
            self.assertEqual(report["sources"][0]["record_count"], 1)

    def test_validate_source_adapters_reports_unsupported_and_missing_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            create_source(kb, source_id="bad-adapter", role_id="public-analyst", platform="x", adapter="platform-api")
            create_source(
                kb,
                source_id="missing-feed",
                role_id="public-analyst",
                platform="x",
                adapter="local-jsonl",
                adapter_config={"input_path": "missing.jsonl"},
            )

            report = validate_source_adapters(kb)

            self.assertFalse(report["ok"])
            self.assertEqual(report["summary"]["total"], 2)
            self.assertEqual(report["summary"]["checked"], 2)
            self.assertEqual(report["summary"]["ready"], 0)
            self.assertEqual(report["summary"]["failed"], 2)
            messages = " ".join(item["message"] for item in report["sources"])
            self.assertIn("Unsupported source adapter", messages)
            self.assertIn("Adapter input not found", messages)

    def test_normalize_source_input_csv_writes_local_jsonl_and_updates_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            create_source(
                kb,
                source_id="x-public-analyst",
                role_id="public-analyst",
                platform="x",
                source_url="https://x.com/public_analyst",
                display_name="Public Analyst",
                symbols=["NVDA"],
                topics=["ai-infrastructure"],
            )
            input_path = Path(temp_dir) / "export.csv"
            input_path.write_text(
                "content,permalink,created_at,title,author,symbols,topics\n"
                "CSV public statement.,https://x.com/public/status/1,2026-05-31,CSV item,Public Analyst,\"NVDA,MSFT\",adapter\n",
                encoding="utf-8",
            )

            result = normalize_source_input(kb, "x-public-analyst", input_path, update_source=True)
            dry_run = run_source(kb, "x-public-analyst", dry_run=True)
            sources = list_sources(kb)

            self.assertEqual(result["record_count"], 1)
            self.assertTrue(Path(result["output_path"]).is_file())
            self.assertEqual(sources[0]["adapter"], "local-jsonl")
            self.assertEqual(sources[0]["adapter_config"]["input_path"], "inbox/adapter-fixtures/x-public-analyst.jsonl")
            self.assertEqual(dry_run["record_count"], 1)
            self.assertEqual(dry_run["records"][0]["text"], "CSV public statement.")
            self.assertEqual(dry_run["records"][0]["title"], "CSV item")
            self.assertEqual(dry_run["records"][0]["symbols"], ["NVDA", "MSFT"])
            self.assertEqual(dry_run["records"][0]["topics"], ["adapter"])

    def test_write_source_input_template_creates_csv_ready_for_normalize(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            create_source(
                kb,
                source_id="x-public-analyst",
                role_id="public-analyst",
                platform="x",
                source_url="https://x.com/public_analyst",
                display_name="Public Analyst",
                symbols=["NVDA"],
                topics=["ai-infrastructure"],
            )

            result = write_source_input_template(kb, "x-public-analyst", output_format="csv")
            normalized = normalize_source_input(kb, "x-public-analyst", Path(result["template_path"]), dry_run=True)
            template_text = Path(result["template_path"]).read_text(encoding="utf-8")

            self.assertTrue(result["ok"])
            self.assertEqual(result["format"], "csv")
            self.assertTrue(Path(result["template_path"]).is_file())
            self.assertIn("source_url,title,text", template_text)
            self.assertEqual(normalized["record_count"], 1)
            self.assertEqual(normalized["records"][0]["role_id"], "public-analyst")
            self.assertEqual(normalized["records"][0]["symbols"], ["NVDA"])
            self.assertIn("voicevault sources normalize", result["next_command"])

    def test_sources_template_json_outputs_template_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            create_source(kb, source_id="x-public-analyst", role_id="public-analyst", platform="x")
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "sources",
                        "template",
                        "--kb",
                        str(kb.root),
                        "--source",
                        "x-public-analyst",
                        "--format",
                        "jsonl",
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            template_path = Path(payload["template_path"])
            record = json.loads(template_path.read_text(encoding="utf-8").splitlines()[0])

            self.assertEqual(exit_code, 0)
            self.assertTrue(template_path.is_file())
            self.assertEqual(payload["format"], "jsonl")
            self.assertIn("text", record)
            self.assertIn("voicevault sources normalize", payload["next_command"])

    def test_import_source_input_updates_source_and_runs_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            create_source(
                kb,
                source_id="x-public-analyst",
                role_id="public-analyst",
                platform="x",
                source_url="https://x.com/public_analyst",
                symbols=["NVDA"],
                topics=["ai-infrastructure"],
            )
            input_path = Path(temp_dir) / "public-feed.csv"
            input_path.write_text(
                "source_url,title,text,author,username,published_at,symbols,topics\n"
                "https://x.com/public/status/10,Import preflight,Imported public statement.,Public Analyst,public_analyst,2026-05-31T10:00:00Z,NVDA,adapter\n",
                encoding="utf-8",
            )

            result = import_source_input(kb, "x-public-analyst", input_path)
            sources = list_sources(kb)
            capture_path = kb.inbox_captures_dir / "source-x-public-analyst.jsonl"

            self.assertTrue(result["ok"])
            self.assertFalse(result["dry_run"])
            self.assertTrue(Path(result["normalized"]["output_path"]).is_file())
            self.assertEqual(sources[0]["adapter"], "local-jsonl")
            self.assertEqual(result["source_validation"]["summary"]["ready"], 1)
            self.assertEqual(result["preflight_run"]["status"], "dry_run")
            self.assertEqual(result["preflight_run"]["record_count"], 1)
            self.assertFalse(capture_path.exists())
            self.assertIn("voicevault sources enqueue", "\n".join(result["next_commands"]))

    def test_import_source_input_records_import_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            create_source(
                kb,
                source_id="x-public-analyst",
                role_id="public-analyst",
                platform="x",
                source_url="https://x.com/public_analyst",
            )
            input_path = Path(temp_dir) / "public-feed.jsonl"
            input_path.write_text(
                '{"text":"Statused public statement.","source_url":"https://x.com/public/status/12"}\n',
                encoding="utf-8",
            )

            result = import_source_input(kb, "x-public-analyst", input_path)
            status = read_source_import_status(kb)
            latest = status["imports"][0]

            self.assertTrue(result["ok"])
            self.assertTrue(Path(status["status_path"]).is_file())
            self.assertEqual(status["summary"]["total"], 1)
            self.assertEqual(status["summary"]["ready"], 1)
            self.assertEqual(status["summary"]["failed"], 0)
            self.assertEqual(latest["source_id"], "x-public-analyst")
            self.assertEqual(latest["status"], "ready")
            self.assertEqual(latest["record_count"], 1)
            self.assertEqual(latest["input_path"], str(input_path))
            self.assertEqual(latest["output_path"], result["normalized"]["output_path"])
            self.assertEqual(latest["preflight_status"], "dry_run")
            self.assertIn("voicevault sources imports", "\n".join(result["next_commands"]))
            self.assertEqual(result["import_status"]["summary"]["ready"], 1)

    def test_import_source_input_dry_run_does_not_write_update_or_record_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            create_source(kb, source_id="x-public-analyst", role_id="public-analyst", platform="x")
            input_path = Path(temp_dir) / "public-feed.jsonl"
            input_path.write_text('{"text":"Dry-run public statement."}\n', encoding="utf-8")

            result = import_source_input(kb, "x-public-analyst", input_path, dry_run=True)
            sources = list_sources(kb)
            status = read_source_status(kb)

            self.assertTrue(result["ok"])
            self.assertTrue(result["dry_run"])
            self.assertFalse(Path(result["normalized"]["output_path"]).exists())
            self.assertEqual(sources[0]["adapter"], "manual")
            self.assertEqual(status["summary"]["total"], 0)
            self.assertIsNone(result["source_validation"])
            self.assertIsNone(result["preflight_run"])
            self.assertEqual(read_source_import_status(kb)["summary"]["total"], 0)

    def test_sources_import_json_outputs_preflight_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            create_source(kb, source_id="x-public-analyst", role_id="public-analyst", platform="x")
            input_path = Path(temp_dir) / "public-feed.jsonl"
            input_path.write_text('{"text":"CLI import public statement.","source_url":"https://x.com/public/status/11"}\n', encoding="utf-8")
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "sources",
                        "import",
                        "--kb",
                        str(kb.root),
                        "--source",
                        "x-public-analyst",
                        "--input",
                        str(input_path),
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())

            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["normalized"]["record_count"], 1)
            self.assertEqual(payload["preflight_run"]["status"], "dry_run")
            self.assertIn("voicevault sources enqueue", "\n".join(payload["next_commands"]))

    def test_sources_imports_json_outputs_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            create_source(kb, source_id="x-public-analyst", role_id="public-analyst", platform="x")
            input_path = Path(temp_dir) / "public-feed.jsonl"
            input_path.write_text('{"text":"CLI import status statement."}\n', encoding="utf-8")
            import_source_input(kb, "x-public-analyst", input_path)
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(["sources", "imports", "--kb", str(kb.root), "--json"])

            payload = json.loads(stdout.getvalue())

            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["summary"]["total"], 1)
            self.assertEqual(payload["summary"]["ready"], 1)
            self.assertEqual(payload["imports"][0]["source_id"], "x-public-analyst")
            self.assertEqual(payload["imports"][0]["status"], "ready")

    def test_normalize_source_input_dry_run_does_not_write_or_update_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            create_source(kb, source_id="x-public-analyst", role_id="public-analyst", platform="x")
            input_path = Path(temp_dir) / "export.jsonl"
            input_path.write_text(
                json.dumps(
                    {
                        "statement_id": "dry-run-001",
                        "full_text": "Dry-run normalized statement.",
                        "link": "https://x.com/public/status/2",
                        "posted_at": "2026-05-31T10:00:00Z",
                        "ingested_at": "2026-05-31T10:05:00Z",
                        "handle": "public_analyst",
                        "name": "Public Analyst",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            result = normalize_source_input(kb, "x-public-analyst", input_path, dry_run=True, update_source=True)
            sources = list_sources(kb)

            self.assertTrue(result["dry_run"])
            self.assertEqual(result["record_count"], 1)
            self.assertEqual(result["records"][0]["statement_id"], "dry-run-001")
            self.assertEqual(result["records"][0]["text"], "Dry-run normalized statement.")
            self.assertEqual(result["records"][0]["url"], "https://x.com/public/status/2")
            self.assertEqual(result["records"][0]["published_at"], "2026-05-31T10:00:00Z")
            self.assertEqual(result["records"][0]["captured_at"], "2026-05-31T10:05:00Z")
            self.assertEqual(result["records"][0]["platform_user_id"], "public_analyst")
            self.assertEqual(result["records"][0]["author"], "Public Analyst")
            self.assertFalse(Path(result["output_path"]).exists())
            self.assertEqual(sources[0]["adapter"], "manual")
            self.assertEqual(sources[0]["adapter_config"], {})

    def test_record_source_run_error_is_visible_in_source_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            create_source(
                kb,
                source_id="x-disabled",
                role_id="public-analyst",
                platform="x",
                source_url="https://x.com/public_analyst",
                enabled=False,
            )

            run = record_source_run_error(kb, "x-disabled", "Source is not active: x-disabled")
            status = read_source_status(kb)

            self.assertEqual(run["status"], "failed")
            self.assertFalse(status["ok"])
            self.assertEqual(status["summary"]["failed"], 1)
            self.assertEqual(status["summary"]["active_sources"], 0)
            self.assertEqual(status["summary"]["active_failed_latest"], 0)
            self.assertIn("not active", status["runs"][0]["error"])

    def test_run_source_writes_capture_that_sync_can_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            create_source(
                kb,
                source_id="x-public-analyst",
                role_id="public-analyst",
                platform="x",
                source_url="https://x.com/public_analyst",
                display_name="Public Analyst",
                symbols=["NVDA"],
                topics=["ai-infrastructure"],
            )

            result = run_source(
                kb,
                "x-public-analyst",
                text="NVDA configured source capture should sync into the archive.",
                title="Configured source capture",
                source_url="https://x.com/public_analyst/status/1",
            )
            validation = validate_capture_path(Path(result["capture_path"]))
            sync_result = sync_once(kb, archive_processed=True)

            self.assertFalse(result["dry_run"])
            self.assertEqual(result["written"], 1)
            self.assertTrue(validation["ok"])
            self.assertEqual(validation["records"], 1)
            self.assertEqual(sync_result.notes_written, 1)
            self.assertTrue((kb.roles_dir / "public-analyst" / "statements" / "x").is_dir())


if __name__ == "__main__":
    unittest.main()
