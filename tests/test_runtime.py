from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from voicevault.app_db import AppDatabase
from voicevault.kb import init_kb
from voicevault.runtime import RuntimeDiscoveryError, RuntimeRecord, RuntimeRegistry
from voicevault.server import create_server


class RuntimeRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_dir = Path(self.temp_dir.name) / "runtime"
        self.registry = RuntimeRegistry(data_dir=self.data_dir)

    def test_publish_is_atomic_and_discover_returns_non_secret_record(self) -> None:
        record = RuntimeRecord(
            schema_version=1,
            instance_id="instance-a",
            base_url="http://127.0.0.1:43210",
            pid=123,
            started_at="2026-07-11T08:00:00Z",
        )
        real_replace = os.replace
        calls: list[tuple[Path, Path]] = []

        def observed_replace(source, destination):
            calls.append((Path(source), Path(destination)))
            self.assertEqual(Path(source).parent, self.registry.path.parent)
            self.assertTrue(Path(source).is_file())
            real_replace(source, destination)

        with patch("voicevault.runtime.os.replace", side_effect=observed_replace):
            self.registry.publish(record)

        self.assertEqual(self.registry.discover(), record)
        self.assertEqual(calls[0][1], self.registry.path)
        raw = self.registry.path.read_text(encoding="utf-8").lower()
        self.assertNotIn("cookie", raw)
        self.assertNotIn("api_key", raw)
        self.assertNotIn("command", raw)

    def test_clear_only_removes_record_owned_by_instance(self) -> None:
        first = RuntimeRecord(1, "instance-a", "http://127.0.0.1:1", 1, "2026-07-11T08:00:00Z")
        second = RuntimeRecord(1, "instance-b", "http://127.0.0.1:2", 2, "2026-07-11T08:01:00Z")
        self.registry.publish(first)
        self.registry.publish(second)

        self.assertFalse(self.registry.clear("instance-a"))
        self.assertEqual(self.registry.discover(), second)
        self.assertTrue(self.registry.clear("instance-b"))
        self.assertFalse(self.registry.path.exists())
        self.assertFalse(self.registry.clear("instance-b"))

    def test_clear_and_publish_share_one_critical_section_so_new_record_survives(self) -> None:
        first = RuntimeRecord(1, "instance-a", "http://127.0.0.1:1", 1, "2026-07-11T08:00:00Z")
        second = RuntimeRecord(1, "instance-b", "http://127.0.0.1:2", 2, "2026-07-11T08:01:00Z")
        old_registry = RuntimeRegistry(path=self.registry.path)
        new_registry = RuntimeRegistry(path=self.registry.path)
        old_registry.publish(first)
        about_to_unlink = threading.Event()
        allow_unlink = threading.Event()
        publish_finished = threading.Event()
        original_unlink = Path.unlink

        def paused_unlink(path: Path, *args, **kwargs):
            if Path(path) == self.registry.path and threading.current_thread().name == "old-clear":
                about_to_unlink.set()
                self.assertTrue(allow_unlink.wait(timeout=2))
            return original_unlink(path, *args, **kwargs)

        clear_thread = threading.Thread(target=lambda: old_registry.clear("instance-a"), name="old-clear")
        publish_thread = threading.Thread(
            target=lambda: (new_registry.publish(second), publish_finished.set()), name="new-publish"
        )
        with patch("pathlib.Path.unlink", new=paused_unlink):
            clear_thread.start()
            self.assertTrue(about_to_unlink.wait(timeout=2))
            publish_thread.start()
            time.sleep(0.1)
            was_blocked = not publish_finished.is_set()
            allow_unlink.set()
            clear_thread.join(timeout=2)
            publish_thread.join(timeout=2)

        self.assertTrue(was_blocked)
        self.assertEqual(self.registry.discover(), second)

    def test_runtime_lock_blocks_publish_from_another_process(self) -> None:
        script = (
            "import sys,time\n"
            "import voicevault.runtime as runtime_module\n"
            "from voicevault.runtime import RuntimeRegistry\n"
            "registry=RuntimeRegistry(path=sys.argv[1])\n"
            "with registry._exclusive_lock():\n"
            " print('locked|' + runtime_module.__file__, flush=True)\n"
            " time.sleep(0.6)\n"
        )
        repo_root = Path(__file__).resolve().parents[1]
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(self.registry.path)],
            cwd=repo_root,
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    filter(
                        None,
                        [
                            str(repo_root / "src"),
                            os.environ.get("PYTHONPATH", ""),
                        ],
                    )
                ),
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        def cleanup_process() -> None:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            process.stdout.close()
            process.stderr.close()

        self.addCleanup(cleanup_process)
        line = process.stdout.readline().strip()
        self.assertTrue(line.startswith("locked|"), process.stderr.read() if not line else line)
        imported_path = Path(line.split("|", 1)[1]).resolve()
        self.assertTrue(imported_path.is_relative_to((repo_root / "src").resolve()), imported_path)
        started = time.monotonic()

        self.registry.publish(
            RuntimeRecord(1, "instance-a", "http://127.0.0.1:1", 1, "2026-07-11T08:00:00Z")
        )

        elapsed = time.monotonic() - started
        self.assertEqual(process.wait(timeout=2), 0, process.stderr.read())
        self.assertGreaterEqual(elapsed, 0.4)

    def test_discover_rejects_missing_malformed_stale_schema_and_non_loopback_url(self) -> None:
        with self.assertRaisesRegex(RuntimeDiscoveryError, "not found"):
            self.registry.discover()
        self.registry.path.parent.mkdir(parents=True)
        for raw in (
            "{",
            json.dumps({"schema_version": 99, "instance_id": "x", "base_url": "http://127.0.0.1:1", "pid": 1, "started_at": "x"}),
            json.dumps({"schema_version": 1, "instance_id": "x", "base_url": "https://127.0.0.1:1", "pid": 1, "started_at": "x"}),
            json.dumps({"schema_version": 1, "instance_id": "x", "base_url": "http://example.com:1", "pid": 1, "started_at": "x"}),
        ):
            with self.subTest(raw=raw):
                self.registry.path.write_text(raw, encoding="utf-8")
                with self.assertRaises(RuntimeDiscoveryError):
                    self.registry.discover()

    def test_default_registry_path_matches_app_database_data_directory(self) -> None:
        database = AppDatabase(data_dir=self.data_dir)
        registry = RuntimeRegistry(data_dir=self.data_dir)
        self.assertEqual(registry.path.parent, database.path.parent)
        self.assertEqual(registry.path.name, "runtime.json")

    def test_server_publishes_real_ephemeral_port_and_clears_own_record_idempotently(self) -> None:
        kb = init_kb(Path(self.temp_dir.name) / "kb")
        database = AppDatabase(data_dir=self.data_dir)
        server = create_server(
            kb,
            port=0,
            app_database=database,
            instance_id="instance-a",
            runtime_registry=self.registry,
        )
        host, port = server.server_address
        record = self.registry.discover()

        self.assertEqual(record.instance_id, "instance-a")
        self.assertEqual(record.base_url, f"http://{host}:{port}")
        self.assertNotEqual(port, 0)
        self.assertEqual(server.collection_service.instance_id, "instance-a")

        server.server_close()
        server.server_close()
        self.assertFalse(self.registry.path.exists())


if __name__ == "__main__":
    unittest.main()
