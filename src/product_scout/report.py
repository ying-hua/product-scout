"""Views over the pool, and exports (Markdown daily brief, RSS, JSON)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any
from xml.sax.saxutils import escape

from .coverage import COVERAGE_LABELS
from .db import ACTIVE_STATUSES, DB, STATUS_LABELS, iso, loads, now, parse_iso

SOURCE_LABELS = {"github": "GitHub", "huggingface": "Hugging Face", "hackernews": "Hacker News"}
METRIC_LABELS = {"stars": "star", "likes": "like", "hn_points": "分"}


def product_dict(db: DB, row: Any, with_history: bool = False) -> dict[str, Any]:
    vd = loads(row["value_detail"], {}) or {}
    d = {
        "id": row["id"],
        "key": row["key"],
        "name": row["name"],
        "url": row["url"],
        "description": row["description"],
        "created_at": row["created_at"],
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
        "status": row["status"],
        "status_label": STATUS_LABELS.get(row["status"], row["status"]),
        "notes": row["notes"],
        "pinned": bool(row["pinned"]),
        "value": row["value"],
        "value_detail": vd,
        "pitch": vd.get("pitch", ""),
        "angle": vd.get("angle", ""),
        "category": vd.get("category", ""),
        "risks": vd.get("risks", []),
        "timeliness": row["timeliness"],
        "timeliness_detail": loads(row["timeliness_detail"], {}),
        "priority": row["priority"],
        "window": bool(row["window"]),
        "coverage": row["coverage"],
        "coverage_label": COVERAGE_LABELS.get(row["coverage"], "未检查"),
        "coverage_detail": loads(row["coverage_detail"], {}),
        "mentions": [
            {
                "source": m["source"],
                "source_label": SOURCE_LABELS.get(m["source"], m["source"]),
                "url": m["url"],
                "title": m["title"],
                "metric": m["metric_name"],
                "metric_label": METRIC_LABELS.get(m["metric_name"] or "", m["metric_name"] or ""),
                "value": m["metric_value"],
                "first_seen": m["first_seen"],
            }
            for m in db.mentions(row["id"])
        ],
    }
    if with_history:
        hist: dict[str, list] = {}
        for s in db.snapshots(row["id"]):
            hist.setdefault(s["metric"], []).append([s["taken_at"], s["value"]])
        d["history"] = hist
    return d


def _active_where() -> str:
    return "status IN (%s)" % ",".join(f"'{s}'" for s in ACTIVE_STATUSES)


ORDER = "pinned DESC, COALESCE(priority, 0) DESC, id DESC"


def views(db: DB, top_n: int = 10, new_hours: int = 24) -> dict[str, list[dict[str, Any]]]:
    since = iso(now() - timedelta(hours=new_hours))
    window = db.products(f"{_active_where()} AND window = 1", order=ORDER)
    pool = db.products(f"{_active_where()} AND window = 0", order=ORDER)[:top_n]
    new = db.products("first_seen >= ? AND status = 'new'", (since,), order=ORDER)
    return {
        "window": [product_dict(db, r) for r in window],
        "pool": [product_dict(db, r) for r in pool],
        "new": [product_dict(db, r) for r in new],
    }


def _fmt(v: Any, nd: int = 0) -> str:
    if v is None:
        return "–"
    return f"{v:.{nd}f}"


def _line(p: dict[str, Any]) -> str:
    srcs = "、".join(sorted({m["source_label"] for m in p["mentions"]}))
    risk = f"  ⚠ {'/'.join(p['risks'])}" if p["risks"] else ""
    pitch = p["pitch"] or p["description"][:80]
    return (
        f"- **[{p['name']}]({p['url']})** · 优先级 {_fmt(p['priority'])} · 价值 {_fmt(p['value'])} · "
        f"时效 {_fmt(p['timeliness'], 2)} · 中文圈：{p['coverage_label']} · {srcs}{risk}\n"
        f"  {pitch}" + (f"\n  角度：{p['angle']}" if p["angle"] else "")
    )


def markdown(db: DB, top_n: int = 10) -> str:
    v = views(db, top_n)
    today = datetime.now().strftime("%Y-%m-%d")
    out = [f"# 选题日报 {today}", ""]
    out += ["## 🔥 窗口期提醒", ""]
    out += [_line(p) for p in v["window"]] or ["今天没有。热点不是每天都有，这很正常。"]
    out += ["", f"## 选题池 Top {top_n}", ""]
    out += [_line(p) for p in v["pool"]] or ["池子还是空的，先运行 `product-scout run`。"]
    out += ["", f"## 今日新进（{len(v['new'])}）", ""]
    out += [f"- [{p['name']}]({p['url']}) · 价值 {_fmt(p['value'])} — {p['pitch'] or p['description'][:60]}"
            for p in v["new"][:NEW_LIMIT]] or ["无"]
    if len(v["new"]) > NEW_LIMIT:
        out.append(f"- ……还有 {len(v['new']) - NEW_LIMIT} 个，在 `product-scout serve` 的「新发现」里看")
    return "\n".join(out) + "\n"


NEW_LIMIT = 30


def rss(db: DB, limit: int = 50, site_url: str = "http://localhost:8765/") -> str:
    rows = db.products(_active_where(), order="first_seen DESC")[:limit]
    items = []
    for r in rows:
        p = product_dict(db, r)
        pub = parse_iso(p["first_seen"]) or datetime.now(timezone.utc)
        desc = f"{p['pitch'] or p['description']}\n价值 {_fmt(p['value'])}，时效 {_fmt(p['timeliness'], 2)}，中文圈：{p['coverage_label']}"
        items.append(
            "<item>"
            f"<title>{escape(p['name'])}</title>"
            f"<link>{escape(p['url'])}</link>"
            f"<guid isPermaLink=\"false\">{escape(p['key'])}</guid>"
            f"<pubDate>{format_datetime(pub)}</pubDate>"
            f"<description>{escape(desc)}</description>"
            "</item>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel>'
        f"<title>product-scout</title><link>{escape(site_url)}</link>"
        "<description>选题池新进产品</description>" + "".join(items) + "</channel></rss>\n"
    )


def export_json(db: DB) -> str:
    rows = db.products(order=ORDER)
    return json.dumps([product_dict(db, r) for r in rows], ensure_ascii=False, indent=2)
