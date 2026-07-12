from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from voicevault.api_router import ApiRouter
from voicevault.app_db import AppDatabase
from voicevault.collection_jobs import CollectionService


NOW = datetime(2026, 7, 12, tzinfo=timezone.utc)


class PageReadModelApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database = AppDatabase(data_dir=Path(self.temp_dir.name) / "runtime")
        self.database.initialize()
        self.collection = CollectionService(
            self.database,
            instance_id="test-instance",
            clock=lambda: NOW,
            handoff_ttl=timedelta(minutes=10),
            lease_ttl=timedelta(minutes=2),
        )
        self.router = ApiRouter(self.database, collection_service=self.collection)

    def _dispatch(self, method: str, target: str, payload: dict | None = None) -> dict:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        response = self.router.dispatch(method, target, body=body, content_length=len(body or b""))
        self.assertIsNotNone(response)
        return response.payload

    def test_workspace_and_person_detail_are_user_facing_read_models(self) -> None:
        created = self._dispatch("POST", "/api/persons", {"display_name": "Alice"})["person"]
        account = self._dispatch(
            "POST",
            f"/api/persons/{created['person_id']}/accounts",
            {
                "platform": "xueqiu",
                "external_user_id": "12345",
                "archive_basis_confirmed": True,
            },
        )["account"]

        workspace = self._dispatch("GET", "/api/workspace")["workspace"]
        detail = self._dispatch("GET", f"/api/persons/{created['person_id']}")["person"]
        collection = self._dispatch(
            "GET", f"/api/persons/{created['person_id']}/collection-summary"
        )["collection"]
        knowledge = self._dispatch(
            "GET", f"/api/persons/{created['person_id']}/knowledge-base"
        )["knowledge_base"]

        self.assertEqual(workspace["summary"]["person_count"], 1)
        self.assertEqual(workspace["pending_tasks"][0]["type"], "collect_missing")
        self.assertEqual(detail["next_action"]["type"], "collect_missing")
        self.assertEqual(detail["accounts"][0]["account_id"], account["account_id"])
        self.assertEqual(collection["jobs"], [])
        self.assertFalse(knowledge["can_ask"])
        serialized = json.dumps({"workspace": workspace, "detail": detail, "collection": collection, "knowledge": knowledge})
        for forbidden in ("lease_expires_at", "checkpoint", "manifest", "local_path", "embedding_fingerprint"):
            self.assertNotIn(forbidden, serialized)

    def test_empty_workspace_has_an_explicit_next_action(self) -> None:
        workspace = self._dispatch("GET", "/api/workspace")["workspace"]
        system = self._dispatch("GET", "/api/system")["system"]
        self.assertEqual(workspace["summary"], {"person_count": 0, "account_count": 0, "post_count": 0, "askable_person_count": 0})
        self.assertEqual(workspace["pending_tasks"], [{"type": "create_person", "priority": "high", "label": "创建人物", "target": "/people"}])
        self.assertEqual(system["activity"], [])
        self.assertEqual(system["health"]["resource_api"], "ready")

    def test_people_collection_and_knowledge_queries_preserve_account_boundaries(self) -> None:
        person = self._dispatch("POST", "/api/persons", {"display_name": "Multi account"})["person"]
        first = self._dispatch(
            "POST", f"/api/persons/{person['person_id']}/accounts",
            {"platform": "xueqiu", "external_user_id": "10001", "archive_basis_confirmed": True},
        )["account"]
        second = self._dispatch(
            "POST", f"/api/persons/{person['person_id']}/accounts",
            {"platform": "weibo", "external_user_id": "researcher_2", "archive_basis_confirmed": True},
        )["account"]

        people = self._dispatch("GET", "/api/people")["people"]
        collection = self._dispatch(
            "GET", f"/api/persons/{person['person_id']}/collection-summary"
        )["collection"]
        knowledge = self._dispatch(
            "GET", f"/api/persons/{person['person_id']}/knowledge-base"
        )["knowledge_base"]
        posts = self._dispatch("GET", f"/api/persons/{person['person_id']}/posts")["posts"]

        self.assertEqual([item["account_id"] for item in people[0]["accounts"]], [first["account_id"], second["account_id"]])
        self.assertEqual([item["account_id"] for item in collection["accounts"]], [first["account_id"], second["account_id"]])
        self.assertEqual(knowledge["posts"], [])
        self.assertEqual(posts, [])
        serialized = json.dumps({"collection": collection, "knowledge": knowledge, "posts": posts})
        for forbidden in ("lease_expires_at", "checkpoint", "manifest", "local_path", "embedding_fingerprint", "fused_rank"):
            self.assertNotIn(forbidden, serialized)

    def test_interrupted_collection_does_not_block_an_existing_ready_knowledge_base(self) -> None:
        person = self._dispatch("POST", "/api/persons", {"display_name": "Imported archive"})["person"]
        account = self._dispatch(
            "POST", f"/api/persons/{person['person_id']}/accounts",
            {"platform": "xueqiu", "external_user_id": "10001", "archive_basis_confirmed": True},
        )["account"]
        account_domain = self.router.accounts.get(account["account_id"])

        account_model = self.router._account_read_model(
            account_domain, [type("Job", (), {"status": "interrupted"})()]
        )
        readiness = self.router._person_readiness(
            {
                "accounts": [account_model],
                "archive": {"post_count": 1},
                "index_head": {"status": "degraded"},
            },
            [],
        )

        self.assertEqual(account_model["last_collection_status"], "interrupted")
        self.assertIsNone(account_model["active_job_status"])
        self.assertEqual(readiness, "ready")

    def test_partial_collection_does_not_block_an_existing_ready_knowledge_base(self) -> None:
        person = self._dispatch("POST", "/api/persons", {"display_name": "Partial archive"})["person"]
        account = self._dispatch(
            "POST", f"/api/persons/{person['person_id']}/accounts",
            {"platform": "xueqiu", "external_user_id": "10003", "archive_basis_confirmed": True},
        )["account"]
        account_domain = self.router.accounts.get(account["account_id"])

        account_model = self.router._account_read_model(
            account_domain, [type("Job", (), {"status": "partial"})()]
        )
        readiness = self.router._person_readiness(
            {
                "accounts": [account_model],
                "archive": {"post_count": 1},
                "index_head": {"status": "degraded"},
            },
            [],
        )

        self.assertEqual(account_model["last_collection_status"], "partial")
        self.assertIsNone(account_model["active_job_status"])
        self.assertEqual(readiness, "ready")

    def test_knowledge_base_materials_are_paginated_searchable_summaries(self) -> None:
        person = self._dispatch("POST", "/api/persons", {"display_name": "Materials"})["person"]
        account = self._dispatch(
            "POST", f"/api/persons/{person['person_id']}/accounts",
            {"platform": "xueqiu", "external_user_id": "10002", "archive_basis_confirmed": True},
        )["account"]
        records = (
            ("post-ai-older", "ai-older", "2026-07-10T00:00:00+00:00", "人工智能框架观察\n这是一条较早的 AI 研究摘要。"),
            ("post-consumer", "consumer", "2026-07-11T00:00:00+00:00", "消费复苏跟踪\n这一条不应出现在技术主题搜索结果。"),
            ("post-ai-newer", "ai-newer", "2026-07-12T00:00:00+00:00", "人工智能产业链更新\n这是一条最新的 AI 研究摘要。"),
        )
        with self.database.transaction() as connection:
            for post_id, external_post_id, published_at, content in records:
                connection.execute(
                    "INSERT INTO posts VALUES (?, ?, ?, ?, ?, ?)",
                    (post_id, account["account_id"], external_post_id, published_at, f"https://example.test/{external_post_id}", NOW.isoformat()),
                )
                connection.execute(
                    "INSERT INTO post_revisions VALUES (?, ?, ?, ?, ?, NULL)",
                    (f"revision-{post_id}", post_id, hashlib.sha256(content.encode("utf-8")).hexdigest(), content, published_at),
                )
            revised = "人工智能框架观察（修订）\n" + ("补充后的完整版本内容。" * 40)
            connection.execute(
                "INSERT INTO post_revisions VALUES (?, ?, ?, ?, ?, NULL)",
                ("revision-post-ai-older-v2", "post-ai-older", hashlib.sha256(revised.encode("utf-8")).hexdigest(), revised, "2026-07-10T01:00:00+00:00"),
            )

        materials = self._dispatch(
            "GET", f"/api/persons/{person['person_id']}/knowledge-base?q=AI&page=2&page_size=1"
        )["knowledge_base"]
        self.assertEqual(materials["post_page"], {
            "page": 2, "page_size": 1, "total": 2, "total_pages": 2,
            "query": "AI", "has_previous": True, "has_next": False,
        })
        self.assertEqual(len(materials["posts"]), 1)
        post = materials["posts"][0]
        self.assertEqual(post["post_key"], "post-ai-older")
        self.assertEqual(post["title"], "人工智能框架观察（修订）")
        self.assertIn("补充后的完整版本内容", post["summary"])
        self.assertEqual(post["version_count"], 2)
        self.assertNotIn("versions", post)

        detail = self._dispatch(
            "GET", f"/api/persons/{person['person_id']}/posts/post-ai-older"
        )["post"]
        self.assertEqual(detail["post_key"], "post-ai-older")
        self.assertEqual(len(detail["versions"]), 2)
        self.assertGreater(len(revised), 280)
        self.assertEqual(detail["versions"][0]["content"], revised)
        self.assertNotIn("excerpt", detail["versions"][0])


if __name__ == "__main__":
    unittest.main()
