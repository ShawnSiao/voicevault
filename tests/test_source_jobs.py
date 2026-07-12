from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from voicevault.kb import init_kb
from voicevault.source_jobs import (
    complete_source_job,
    drain_source_jobs,
    enqueue_source_jobs,
    fail_source_job,
    get_source_job,
    read_source_job_status,
    retry_source_job,
)
from voicevault.sources import create_source, run_source


class SourceJobTests(unittest.TestCase):
    def test_enqueue_source_jobs_creates_one_pending_job_per_active_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            create_source(kb, source_id="x-public-analyst", role_id="public-analyst", platform="x")

            result = enqueue_source_jobs(kb)
            duplicate = enqueue_source_jobs(kb)
            status = read_source_job_status(kb)

            self.assertEqual(result["created"], 1)
            self.assertEqual(duplicate["created"], 0)
            self.assertEqual(status["summary"]["total"], 1)
            self.assertEqual(status["summary"]["pending"], 1)
            self.assertEqual(status["summary"]["completed"], 0)
            self.assertEqual(status["jobs"][0]["source_id"], "x-public-analyst")
            self.assertEqual(status["jobs"][0]["status"], "pending")
            self.assertTrue(Path(status["status_path"]).is_file())

    def test_complete_source_job_records_run_id_and_clears_pending_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            create_source(kb, source_id="x-public-analyst", role_id="public-analyst", platform="x")
            job = enqueue_source_jobs(kb)["jobs"][0]
            source_run = run_source(kb, "x-public-analyst", text="Dry-run job capture.", dry_run=True)

            completed = complete_source_job(kb, job["job_id"], source_run["run"])
            loaded = get_source_job(kb, job["job_id"])
            status = read_source_job_status(kb)

            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["run_id"], source_run["run"]["run_id"])
            self.assertEqual(completed["attempts"], 1)
            self.assertEqual(loaded["status"], "completed")
            self.assertEqual(status["summary"]["pending"], 0)
            self.assertEqual(status["summary"]["completed"], 1)

    def test_fail_source_job_records_error_and_attempt_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            create_source(kb, source_id="x-public-analyst", role_id="public-analyst", platform="x")
            job = enqueue_source_jobs(kb)["jobs"][0]

            failed = fail_source_job(kb, job["job_id"], "adapter timeout")
            status = read_source_job_status(kb, status_filter="failed")

            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["attempts"], 1)
            self.assertEqual(failed["last_error"], "adapter timeout")
            self.assertEqual(status["summary"]["failed"], 1)
            self.assertEqual(status["jobs"][0]["job_id"], job["job_id"])

    def test_retry_source_job_moves_failed_job_back_to_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            create_source(kb, source_id="x-public-analyst", role_id="public-analyst", platform="x")
            job = enqueue_source_jobs(kb)["jobs"][0]
            failed = fail_source_job(kb, job["job_id"], "adapter timeout")

            retried = retry_source_job(kb, failed["job_id"], due_at="soon")
            status = read_source_job_status(kb)

            self.assertEqual(retried["status"], "pending")
            self.assertEqual(retried["attempts"], 1)
            self.assertEqual(retried["due_at"], "soon")
            self.assertEqual(retried["last_error"], "")
            self.assertEqual(retried["run_id"], "")
            self.assertEqual(retried["capture_path"], "")
            self.assertEqual(retried["completed_at"], "")
            self.assertEqual(status["summary"]["pending"], 1)
            self.assertEqual(status["summary"]["failed"], 0)

    def test_drain_source_jobs_runs_pending_local_jsonl_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            input_path = Path(temp_dir) / "public-feed.jsonl"
            input_path.write_text(
                '{"text":"Queued adapter record.","source_url":"https://x.com/public/status/4"}\n',
                encoding="utf-8",
            )
            create_source(
                kb,
                source_id="local-jsonl-source",
                role_id="public-analyst",
                platform="x",
                adapter="local-jsonl",
                adapter_config={"input_path": str(input_path)},
            )
            job = enqueue_source_jobs(kb)["jobs"][0]

            result = drain_source_jobs(kb)
            status = read_source_job_status(kb)
            capture_path = kb.inbox_captures_dir / "source-local-jsonl-source.jsonl"
            records = [json.loads(line) for line in capture_path.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(result["processed"], 1)
            self.assertEqual(result["completed"], 1)
            self.assertEqual(result["failed"], 0)
            self.assertEqual(result["jobs"][0]["job_id"], job["job_id"])
            self.assertEqual(result["jobs"][0]["status"], "completed")
            self.assertEqual(result["jobs"][0]["written"], 1)
            self.assertEqual(status["summary"]["pending"], 0)
            self.assertEqual(status["summary"]["completed"], 1)
            self.assertEqual(records[0]["text"], "Queued adapter record.")

    def test_drain_source_jobs_marks_manual_job_failed_without_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            create_source(kb, source_id="manual-source", role_id="public-analyst", platform="x")
            job = enqueue_source_jobs(kb)["jobs"][0]

            result = drain_source_jobs(kb)
            status = read_source_job_status(kb, status_filter="failed")

            self.assertEqual(result["processed"], 1)
            self.assertEqual(result["completed"], 0)
            self.assertEqual(result["failed"], 1)
            self.assertEqual(result["jobs"][0]["job_id"], job["job_id"])
            self.assertEqual(result["jobs"][0]["status"], "failed")
            self.assertIn("Capture text is required", result["jobs"][0]["error"])
            self.assertEqual(status["summary"]["pending"], 0)
            self.assertEqual(status["summary"]["failed"], 1)
            self.assertEqual(status["jobs"][0]["attempts"], 1)


if __name__ == "__main__":
    unittest.main()
