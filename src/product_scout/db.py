"""SQLite storage: products (the topic pool), mentions (where we saw them), snapshots (metric history)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

STATUSES = ["new", "watching", "tried", "scheduled", "published", "dropped"]
STATUS_LABELS = {
    "new": "新发现",
    "watching": "观察中",
    "tried": "已试用",
    "scheduled": "排期",
    "published": "已发布",
    "dropped": "放弃",
}
ACTIVE_STATUSES = ("new", "watching", "tried", "scheduled")

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  url TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  created_at TEXT,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new',
  status_changed_at TEXT,
  notes TEXT NOT NULL DEFAULT '',
  pinned INTEGER NOT NULL DEFAULT 0,
  readme_url TEXT,
  value REAL,
  value_detail TEXT,
  scored_at TEXT,
  coverage TEXT,
  coverage_detail TEXT,
  coverage_checked_at TEXT,
  timeliness REAL,
  timeliness_detail TEXT,
  priority REAL,
  window INTEGER NOT NULL DEFAULT 0,
  ranked_at TEXT
);
CREATE TABLE IF NOT EXISTS mentions (
  id INTEGER PRIMARY KEY,
  product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  source TEXT NOT NULL,
  external_id TEXT NOT NULL,
  url TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  metric_name TEXT,
  metric_value REAL,
  posted_at TEXT,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  UNIQUE (source, external_id)
);
CREATE TABLE IF NOT EXISTS snapshots (
  product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  metric TEXT NOT NULL,
  value REAL NOT NULL,
  taken_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots ON snapshots(product_id, metric, taken_at);
CREATE TABLE IF NOT EXISTS status_log (
  product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  from_status TEXT,
  to_status TEXT NOT NULL,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  summary TEXT
);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Item:
    """One sighting of a product on one source."""

    source: str
    external_id: str
    key: str
    name: str
    url: str
    mention_url: str
    description: str = ""
    title: str = ""
    metric_name: str | None = None
    metric_value: float | None = None
    posted_at: str | None = None
    created_at: str | None = None
    readme_url: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class DB:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ---- writes -------------------------------------------------------

    def upsert_item(self, item: Item, at: datetime | None = None) -> tuple[int, bool]:
        """Store a sighting. Returns (product_id, is_new_product)."""
        ts = iso(at)
        c = self.conn
        row = c.execute("SELECT * FROM products WHERE key = ?", (item.key,)).fetchone()
        is_new = row is None
        if is_new:
            cur = c.execute(
                "INSERT INTO products (key, name, url, description, created_at, first_seen, last_seen,"
                " status_changed_at, readme_url) VALUES (?,?,?,?,?,?,?,?,?)",
                (item.key, item.name, item.url, item.description, item.created_at, ts, ts, ts, item.readme_url),
            )
            pid = int(cur.lastrowid)
        else:
            pid = int(row["id"])
            c.execute(
                "UPDATE products SET last_seen = ?,"
                " description = CASE WHEN description = '' THEN ? ELSE description END,"
                " created_at = COALESCE(created_at, ?),"
                " readme_url = COALESCE(readme_url, ?)"
                " WHERE id = ?",
                (ts, item.description, item.created_at, item.readme_url, pid),
            )
        c.execute(
            "INSERT INTO mentions (product_id, source, external_id, url, title, metric_name, metric_value,"
            " posted_at, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(source, external_id) DO UPDATE SET metric_value = excluded.metric_value,"
            " title = excluded.title, last_seen = excluded.last_seen",
            (pid, item.source, item.external_id, item.mention_url, item.title or item.name,
             item.metric_name, item.metric_value, item.posted_at, ts, ts),
        )
        if item.metric_name and item.metric_value is not None:
            self.add_snapshot(pid, item.metric_name, item.metric_value, at)
        c.commit()
        return pid, is_new

    def add_snapshot(self, pid: int, metric: str, value: float, at: datetime | None = None) -> None:
        """Record a metric reading; at most one per metric per hour."""
        at = at or now()
        last = self.conn.execute(
            "SELECT taken_at FROM snapshots WHERE product_id = ? AND metric = ? ORDER BY taken_at DESC LIMIT 1",
            (pid, metric),
        ).fetchone()
        if last:
            prev = parse_iso(last["taken_at"])
            if prev and at - prev < timedelta(hours=1):
                self.conn.execute(
                    "UPDATE snapshots SET value = ? WHERE product_id = ? AND metric = ? AND taken_at = ?",
                    (value, pid, metric, last["taken_at"]),
                )
                return
        self.conn.execute(
            "INSERT INTO snapshots (product_id, metric, value, taken_at) VALUES (?,?,?,?)",
            (pid, metric, value, iso(at)),
        )

    def update_product(self, pid: int, **fields: Any) -> None:
        if not fields:
            return
        for k, v in list(fields.items()):
            if isinstance(v, (dict, list)):
                fields[k] = json.dumps(v, ensure_ascii=False)
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE products SET {cols} WHERE id = ?", (*fields.values(), pid))
        self.conn.commit()

    def set_status(self, pid: int, status: str) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}; choose from {', '.join(STATUSES)}")
        row = self.conn.execute("SELECT status FROM products WHERE id = ?", (pid,)).fetchone()
        if row is None:
            raise KeyError(pid)
        if row["status"] == status:
            return
        ts = iso()
        self.conn.execute("UPDATE products SET status = ?, status_changed_at = ? WHERE id = ?", (status, ts, pid))
        self.conn.execute(
            "INSERT INTO status_log (product_id, from_status, to_status, at) VALUES (?,?,?,?)",
            (pid, row["status"], status, ts),
        )
        self.conn.commit()

    def start_run(self, kind: str) -> int:
        cur = self.conn.execute("INSERT INTO runs (kind, started_at) VALUES (?, ?)", (kind, iso()))
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, summary: dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, summary = ? WHERE id = ?",
            (iso(), json.dumps(summary, ensure_ascii=False), run_id),
        )
        self.conn.commit()

    # ---- reads --------------------------------------------------------

    def product(self, pid: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()

    def products(self, where: str = "1=1", params: Iterable[Any] = (), order: str = "id") -> list[sqlite3.Row]:
        return self.conn.execute(f"SELECT * FROM products WHERE {where} ORDER BY {order}", tuple(params)).fetchall()

    def mentions(self, pid: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM mentions WHERE product_id = ? ORDER BY first_seen", (pid,)
        ).fetchall()

    def snapshots(self, pid: int, metric: str | None = None) -> list[sqlite3.Row]:
        if metric:
            return self.conn.execute(
                "SELECT * FROM snapshots WHERE product_id = ? AND metric = ? ORDER BY taken_at", (pid, metric)
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM snapshots WHERE product_id = ? ORDER BY metric, taken_at", (pid,)
        ).fetchall()

    def last_run(self, kind: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM runs WHERE kind = ? AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1", (kind,)
        ).fetchone()


def loads(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default
