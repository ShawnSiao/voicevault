from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from voicevault.app_db import AppDatabase
from voicevault.kb import init_kb
from voicevault.server import create_server


class ResourceUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.kb = init_kb(self.root / "kb")

    def test_resource_mode_serves_api_ui_from_fixed_same_origin_allowlist(self) -> None:
        server = create_server(
            self.kb,
            port=0,
            repo_root=Path(__file__).resolve().parents[1],
            app_database=AppDatabase(data_dir=self.root / "runtime"),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address
        base = f"http://{host}:{port}"

        html = urlopen(f"{base}/", timeout=5).read().decode()
        self.assertIn("声迹 VoiceVault｜本地人物知识库 MVP", html)
        self.assertIn("react@18.3.1/umd/react.development.js", html)
        self.assertIn("react-dom@18.3.1/umd/react-dom.development.js", html)
        self.assertIn("@babel/standalone@7.29.0/babel.min.js", html)
        self.assertIn('/resource-api.js', html)
        self.assertIn('/components.jsx', html)
        self.assertIn('/resource-ui.jsx', html)
        self.assertNotIn('/resource-app.js', html)
        self.assertNotIn('/app.jsx', html)
        self.assertNotIn('/data.js', html)
        for asset in ("styles.css", "resource-api.js", "components.jsx", "resource-ui.jsx"):
            with urlopen(f"{base}/{asset}", timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertGreater(len(response.read()), 100)
        for path in ("/../README.md", "/does-not-exist.js", "/data.js", "/resource-app.js"):
            with self.assertRaises(HTTPError) as raised:
                urlopen(f"{base}{path}", timeout=5)
            self.assertEqual(raised.exception.code, 404)

        app = urlopen(f"{base}/resource-ui.jsx", timeout=5).read().decode()
        for route in (
            "/api/persons",
            "/api/collection-jobs",
            "/api/index-jobs",
            "/api/workspace",
            "/api/questions",
            "/api/capabilities",
        ):
            self.assertIn(route, app)
        self.assertIn("window.VoiceVaultApi", app)
        self.assertIn("window.VVComponents", app)
        self.assertIn("materialQuery", app)
        self.assertIn("搜索主题、摘要或帖子 ID", app)
        self.assertIn("post_page", app)
        self.assertIn("/posts/${encodeURIComponent(post.post_key)}", app)
        self.assertIn("version.content", app)
        self.assertNotIn("version.excerpt", app)
        for component in (
            "QuestionComposer",
            "AnswerProgress",
            "AnswerSummary",
            "UncertaintyNotice",
            "EvidenceRail",
        ):
            self.assertIn(component, app)
        components = urlopen(f"{base}/components.jsx", timeout=5).read().decode()
        for component in (
            "QuestionComposer",
            "AnswerProgress",
            "AnswerSummary",
            "OpinionSection",
            "UncertaintyNotice",
            "EvidenceRail",
        ):
            self.assertIn(f"function {component}", components)
        self.assertIn("ReactDOM.createRoot", app)
        self.assertNotIn("VoiceVaultData", app)
        self.assertNotIn("linzhou_demo", app)

    def test_legacy_mode_keeps_generated_ui(self) -> None:
        server = create_server(self.kb, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address
        html = urlopen(f"http://{host}:{port}/", timeout=5).read().decode()
        self.assertNotIn("/resource-ui.jsx", html)
        self.assertIn("data.json", html)


if __name__ == "__main__":
    unittest.main()
