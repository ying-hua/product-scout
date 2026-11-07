"""End-to-end without the internet: a fake OpenAI-compatible endpoint, and the local web UI."""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from product_scout import rank, report, scoring
from product_scout.config import load_config
from product_scout.db import DB, Item
from product_scout.net import Http
from product_scout.server import make_handler

REPLY = {
    "relevant": True,
    "dimensions": {"visual": 9, "hands_on": 8, "paid_alternative": 6, "audience_fit": 9, "novelty": 8},
    "risks": [],
    "pitch": "一句话让 AI 帮你操作手机",
    "angle": "AI 替你玩手机",
    "category": "AI Agent",
}


class FakeLLM(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert self.headers["Authorization"] == "Bearer test-key"
        assert body["messages"][0]["role"] == "system"
        FakeLLM.calls += 1
        content = "```json\n" + json.dumps(REPLY, ensure_ascii=False) + "\n```"  # fenced, like many models do
        out = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def start(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.llm, llm_url = start(FakeLLM)
        (self.root / "product-scout.toml").write_text(
            f'[llm]\nbase_url = "{llm_url}/v1"\nmodel = "fake"\napi_key_env = "SCOUT_TEST_KEY"\n'
            '[coverage]\nenabled = false\n',
            encoding="utf-8",
        )
        os.environ["SCOUT_TEST_KEY"] = "test-key"
        self.cfg = load_config(self.root / "product-scout.toml")
        self.db = DB(self.cfg.db_path)
        self.db.upsert_item(Item(
            source="github", external_id="repo:1", key="github:me/mika", name="Mika",
            url="https://github.com/me/mika", mention_url="https://github.com/me/mika",
            description="phone agent", metric_name="stars", metric_value=5000.0,
            created_at="2026-11-01T00:00:00Z",
        ))

    def tearDown(self):
        self.llm.shutdown()
        self.llm.server_close()
        self.db.close()
        os.environ.pop("SCOUT_TEST_KEY", None)
        self.tmp.cleanup()

    def test_score_rank_report_and_web(self):
        FakeLLM.calls = 0
        n = scoring.score_pending(self.db, Http(use_env_proxy=False), self.cfg, log=lambda _: None)
        self.assertEqual(n, 1)
        self.assertEqual(FakeLLM.calls, 2)  # scored twice, independently; they agree, so no third call
        rank.rank_all(self.db, self.cfg)
        row = self.db.products()[0]
        self.assertAlmostEqual(row["value"], 82.5)  # 9*.3 + 8*.2 + 6*.15 + 9*.25 + 8*.1
        self.assertIsNotNone(row["priority"])
        self.assertIn("Mika", report.markdown(self.db))
        self.assertIn("<rss", report.rss(self.db))

        web, url = start(make_handler(self.cfg))
        try:
            with urllib.request.urlopen(url + "/") as r:
                self.assertIn(b"product-scout", r.read())
            with urllib.request.urlopen(url + "/api/products?status=active") as r:
                items = json.loads(r.read())
            self.assertEqual(items[0]["pitch"], REPLY["pitch"])
            pid = items[0]["id"]

            req = urllib.request.Request(
                f"{url}/api/products/{pid}", method="PATCH",
                data=json.dumps({"status": "tried", "notes": "效果不错"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req) as r:
                self.assertEqual(json.loads(r.read())["status"], "tried")

            evil = urllib.request.Request(
                f"{url}/api/products/{pid}", method="PATCH", data=b'{"status":"dropped"}',
                headers={"Content-Type": "application/json", "Origin": "https://evil.example"},
            )
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(evil)
            self.assertEqual(e.exception.code, 403)
        finally:
            web.shutdown()
            web.server_close()
        check = DB(self.cfg.db_path)
        self.assertEqual(check.product(pid)["status"], "tried")
        self.assertEqual(check.product(pid)["notes"], "效果不错")
        check.close()


if __name__ == "__main__":
    unittest.main()
