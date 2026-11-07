"""Local web UI for the pool: browse, change status, take notes. Standard library only."""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import Config
from .db import ACTIVE_STATUSES, DB, STATUS_LABELS, STATUSES, loads
from .pipeline import llm_key_hint
from .report import ORDER, product_dict

_PRODUCT = re.compile(r"^/api/products/(\d+)$")


def _index_html() -> bytes:
    return resources.files("product_scout").joinpath("web/index.html").read_bytes()


def make_handler(cfg: Config):
    class Handler(BaseHTTPRequestHandler):
        server_version = "product-scout"

        def log_message(self, fmt: str, *args: Any) -> None:  # keep the console quiet
            pass

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any, status: int = 200) -> None:
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _same_origin(self) -> bool:
            # Block cross-site writes: browsers always send Origin on PATCH/POST.
            origin = self.headers.get("Origin")
            return origin is None or urlparse(origin).netloc == self.headers.get("Host")

        def do_GET(self) -> None:
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                return self._send(200, _index_html(), "text/html; charset=utf-8")
            db = DB(cfg.db_path)
            try:
                if u.path == "/api/overview":
                    return self._json(self._overview(db))
                if u.path == "/api/products":
                    qs = parse_qs(u.query)
                    status = (qs.get("status") or [""])[0]
                    return self._json(self._list(db, status, (qs.get("q") or [""])[0]))
                m = _PRODUCT.match(u.path)
                if m:
                    row = db.product(int(m.group(1)))
                    if row is None:
                        return self._json({"error": "not found"}, 404)
                    return self._json(product_dict(db, row, with_history=True))
                self._json({"error": "not found"}, 404)
            finally:
                db.close()

        def do_PATCH(self) -> None:
            if not self._same_origin():
                return self._json({"error": "cross-origin request refused"}, 403)
            m = _PRODUCT.match(urlparse(self.path).path)
            if not m:
                return self._json({"error": "not found"}, 404)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json({"error": "invalid JSON"}, 400)
            db = DB(cfg.db_path)
            try:
                pid = int(m.group(1))
                if db.product(pid) is None:
                    return self._json({"error": "not found"}, 404)
                if "status" in body:
                    if body["status"] not in STATUSES:
                        return self._json({"error": "unknown status"}, 400)
                    db.set_status(pid, body["status"])
                fields = {}
                if "notes" in body:
                    fields["notes"] = str(body["notes"])[:5000]
                if "pinned" in body:
                    fields["pinned"] = 1 if body["pinned"] else 0
                db.update_product(pid, **fields)
                return self._json(product_dict(db, db.product(pid), with_history=True))
            finally:
                db.close()

        def _overview(self, db: DB) -> dict[str, Any]:
            counts = {s: 0 for s in STATUSES}
            for r in db.conn.execute("SELECT status, COUNT(*) n FROM products GROUP BY status"):
                counts[r["status"]] = r["n"]
            last = db.last_run("daily")
            return {
                "statuses": [{"id": s, "label": STATUS_LABELS[s], "count": counts[s]} for s in STATUSES],
                "window": sum(1 for _ in db.products("window = 1 AND status IN ('new','watching','tried')")),
                "last_run": {"at": last["finished_at"], "summary": loads(last["summary"], {})} if last else None,
                "llm_missing_env": llm_key_hint(cfg),
            }

        def _list(self, db: DB, status: str, q: str) -> list[dict[str, Any]]:
            where, params = [], []
            if status == "window":
                where.append("window = 1 AND status IN ('new','watching','tried')")
            elif status == "active" or not status:
                where.append("status IN (%s)" % ",".join("?" * len(ACTIVE_STATUSES)))
                params += ACTIVE_STATUSES
            elif status in STATUSES:
                where.append("status = ?")
                params.append(status)
            if q:
                where.append("(name LIKE ? OR description LIKE ? OR key LIKE ? OR value_detail LIKE ? OR notes LIKE ?)")
                params += [f"%{q}%"] * 5
            order = "status_changed_at DESC" if status in ("published", "dropped") else ORDER
            rows = db.products(" AND ".join(where) or "1=1", params, order=order)[:300]
            return [product_dict(db, r) for r in rows]

    return Handler


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8765) -> None:
    DB(cfg.db_path).close()  # make sure the schema exists
    httpd = ThreadingHTTPServer((host, port), make_handler(cfg))
    print(f"选题池已打开：http://{host}:{port}/  （Ctrl+C 退出）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
