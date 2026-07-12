from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from voicevault.app_db import AppDatabase
from voicevault.coverage import CoverageRepository, page_date_range_to_utc
from voicevault.kb import init_kb
from voicevault.server import create_server


class PersonArchiveApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.database = AppDatabase(data_dir=root / "runtime")
        self.kb = init_kb(root / "kb")
        self.server = create_server(self.kb, port=0, app_database=self.database)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"
        self.addCleanup(self._close_server)

    def _close_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_create_list_and_bind_account(self) -> None:
        created = self._post("/api/persons", {"display_name": " Alice ", "aliases": ["A", "阿丽"]})
        person_id = created["person"]["person_id"]
        bound = self._post(
            f"/api/persons/{person_id}/accounts",
            {
                "platform": "snowball",
                "external_user_id": "12345",
                "display_name": "Alice on Xueqiu",
                "archive_basis_confirmed": True,
            },
        )
        listed = self._get("/api/persons")

        self.assertEqual(created["person"]["display_name"], "Alice")
        self.assertEqual(bound["account"]["platform"], "xueqiu")
        self.assertIsNotNone(bound["account"]["archive_basis_confirmed_at"])
        self.assertEqual(listed["persons"][0]["accounts"], [bound["account"]])

    def test_account_validation_ownership_and_missing_person_map_stably(self) -> None:
        alice = self._post("/api/persons", {"display_name": "Alice"})["person"]
        bob = self._post("/api/persons", {"display_name": "Bob"})["person"]
        self._post(
            f"/api/persons/{alice['person_id']}/accounts",
            {"platform": "xueqiu", "external_user_id": "12345"},
        )

        status, conflict = self._error(
            "POST",
            f"/api/persons/{bob['person_id']}/accounts",
            {"platform": "xueqiu", "external_user_id": "12345"},
        )
        bad_status, bad = self._error(
            "POST",
            f"/api/persons/{alice['person_id']}/accounts",
            {"platform": "xueqiu", "external_user_id": "https://xueqiu.com/u/123?cookie=bad"},
        )
        missing_status, missing = self._error(
            "POST",
            "/api/persons/missing/accounts",
            {"platform": "xueqiu", "external_user_id": "999"},
        )

        self.assertEqual((status, conflict["error"]["code"]), (409, "account_ownership_conflict"))
        self.assertEqual((bad_status, bad["error"]["code"]), (400, "invalid_request"))
        self.assertNotIn("sqlite", json.dumps(conflict).lower())
        self.assertEqual((missing_status, missing["error"]["code"]), (404, "person_not_found"))

    def test_coverage_returns_clipped_covered_missing_and_proof(self) -> None:
        person = self._post("/api/persons", {"display_name": "Alice"})["person"]
        account = self._post(
            f"/api/persons/{person['person_id']}/accounts",
            {
                "platform": "xueqiu",
                "external_user_id": "12345",
                "archive_basis_confirmed_at": "2026-07-01T00:00:00Z",
            },
        )["account"]
        CoverageRepository(self.database).record_validated_complete(
            account["account_id"], page_date_range_to_utc("2026-07-02", "2026-07-02")
        )
        query = urlencode(
            {"account_id": account["account_id"], "start_date": "2026-07-01", "end_date": "2026-07-03"}
        )
        payload = self._get(f"/api/persons/{person['person_id']}/coverage?{query}")["coverage"]

        self.assertEqual(
            payload["request"],
            {
                "account_id": account["account_id"],
                "start_at": "2026-06-30T16:00:00Z",
                "end_at": "2026-07-03T16:00:00Z",
            },
        )
        self.assertEqual(payload["covered"], [{"start_at": "2026-07-01T16:00:00Z", "end_at": "2026-07-02T16:00:00Z"}])
        self.assertEqual(len(payload["missing"]), 2)
        self.assertFalse(payload["proof_complete"])

    def test_coverage_rejects_wrong_owner_bad_dates_and_unknown_resources(self) -> None:
        alice = self._post("/api/persons", {"display_name": "Alice"})["person"]
        bob = self._post("/api/persons", {"display_name": "Bob"})["person"]
        account = self._post(
            f"/api/persons/{alice['person_id']}/accounts",
            {"platform": "xueqiu", "external_user_id": "12345"},
        )["account"]
        base_query = {"account_id": account["account_id"], "start_date": "2026-07-01", "end_date": "2026-07-03"}

        status, payload = self._error("GET", f"/api/persons/{bob['person_id']}/coverage?{urlencode(base_query)}")
        bad_query = {**base_query, "end_date": "2026-06-30"}
        bad_status, bad = self._error("GET", f"/api/persons/{alice['person_id']}/coverage?{urlencode(bad_query)}")
        missing_status, missing = self._error("GET", f"/api/persons/missing/coverage?{urlencode(base_query)}")

        self.assertEqual((status, payload["error"]["code"]), (409, "account_person_conflict"))
        self.assertEqual((bad_status, bad["error"]["code"]), (400, "invalid_request"))
        self.assertEqual((missing_status, missing["error"]["code"]), (404, "person_not_found"))

    def test_malformed_json_oversize_body_and_unknown_dynamic_route_are_stable(self) -> None:
        malformed = Request(
            f"{self.base_url}/api/persons",
            data=b"{",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(malformed, timeout=5)
        malformed_payload = json.loads(raised.exception.read())

        oversized = Request(
            f"{self.base_url}/api/persons",
            data=b"x" * (1024 * 1024 + 1),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as too_large:
            urlopen(oversized, timeout=5)
        too_large_payload = json.loads(too_large.exception.read())
        unknown_status, unknown = self._error("GET", "/api/persons/not-a-route/extra")

        self.assertEqual((raised.exception.code, malformed_payload["error"]["code"]), (400, "invalid_json"))
        self.assertEqual((too_large.exception.code, too_large_payload["error"]["code"]), (413, "request_too_large"))
        self.assertEqual(unknown_status, 404)
        self.assertFalse(unknown["ok"])

    def test_declared_oversize_short_body_returns_413_without_waiting_for_declared_length(self) -> None:
        host, port = self.server.server_address
        with socket.create_connection((host, port), timeout=2) as client:
            client.settimeout(1)
            client.sendall(
                (
                    "POST /api/persons HTTP/1.1\r\n"
                    f"Host: {host}:{port}\r\n"
                    f"Content-Length: {1024 * 1024 + 100}\r\n"
                    "Content-Type: application/json\r\n"
                    "Connection: keep-alive\r\n"
                    "\r\n"
                ).encode("ascii")
                + b"{}"
            )

            response = b""
            while b"\r\n\r\n" not in response:
                response += client.recv(4096)
            header_bytes, body = response.split(b"\r\n\r\n", 1)
            headers = header_bytes.decode("iso-8859-1").lower()
            content_length = next(
                int(line.split(":", 1)[1].strip())
                for line in headers.split("\r\n")
                if line.startswith("content-length:")
            )
            while len(body) < content_length:
                body += client.recv(4096)

            self.assertIn(" 413 ", headers.split("\r\n", 1)[0])
            self.assertIn("connection: close", headers)
            self.assertEqual(json.loads(body[:content_length])["error"]["code"], "request_too_large")
            self.assertEqual(client.recv(1), b"")

    def _get(self, path: str) -> dict:
        with urlopen(f"{self.base_url}{path}", timeout=5) as response:
            return json.loads(response.read())

    def _post(self, path: str, payload: dict) -> dict:
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 201 if path in {"/api/persons"} or path.endswith("/accounts") else 200)
            return json.loads(response.read())

    def _error(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(
            f"{self.base_url}{path}", data=data, headers={"Content-Type": "application/json"}, method=method
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=5)
        return raised.exception.code, json.loads(raised.exception.read())


if __name__ == "__main__":
    unittest.main()
