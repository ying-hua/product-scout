"""Timeliness and priority.

value       0-100, how good a video it would make (stable, scored once)
timeliness  0-1, how urgent it is right now (recomputed every run, decays)
priority    value x (0.35 + 0.65 x timeliness), so evergreen picks stay in the pool
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Any

from .config import Config
from .db import ACTIVE_STATUSES, DB, iso, now, parse_iso

# Per-day growth that counts as "maxed out" momentum for each metric.
VELOCITY_CAP = {"stars": 1500.0, "likes": 60.0, "hn_points": 1000.0}
COVERAGE_FACTOR = {"none": 1.0, None: 0.9, "light": 0.7, "heavy": 0.35}
FRESH_HALF_LIFE_DAYS = 10.0
DEFAULT_VALUE = 50.0  # used before a product has been scored by the LLM


def velocity(points: list[tuple[datetime, float]], created: datetime | None, at: datetime) -> float:
    """Per-day growth from the readings over the last week, or average since creation if we have one reading."""
    if not points:
        return 0.0
    points = sorted(points)
    latest_t, latest_v = points[-1]
    window = [p for p in points if latest_t - p[0] <= timedelta(days=7) and latest_t - p[0] >= timedelta(hours=12)]
    if window:
        t0, v0 = window[0]
        days = (latest_t - t0).total_seconds() / 86400
        return max(0.0, (latest_v - v0) / days)
    if created:
        days = max((at - created).total_seconds() / 86400, 1.0)
        return max(0.0, latest_v / days)
    return 0.0


def momentum_score(v: float, cap: float) -> float:
    return min(1.0, math.log1p(v) / math.log1p(cap)) if cap > 0 else 0.0


def timeliness(db: DB, row: Any, at: datetime) -> tuple[float, dict[str, Any]]:
    created = parse_iso(row["created_at"]) or parse_iso(row["first_seen"])
    momentum, velocities = 0.0, {}
    for metric, cap in VELOCITY_CAP.items():
        pts = [(parse_iso(s["taken_at"]), float(s["value"])) for s in db.snapshots(row["id"], metric)]
        pts = [(t, v) for t, v in pts if t]
        if not pts:
            continue
        v = velocity(pts, created, at)
        velocities[metric] = round(v, 1)
        momentum = max(momentum, momentum_score(v, cap))

    week_ago = iso(at - timedelta(days=7))
    sources = {m["source"] for m in db.mentions(row["id"]) if m["last_seen"] >= week_ago}
    spread = min(1.0, (len(sources) - 1) / 2) if sources else 0.0

    age_days = max(0.0, (at - created).total_seconds() / 86400) if created else 0.0
    freshness = 0.5 ** (age_days / FRESH_HALF_LIFE_DAYS)

    raw = 0.55 * momentum + 0.20 * spread + 0.25 * freshness
    factor = COVERAGE_FACTOR.get(row["coverage"], 0.9)
    t = round(raw * factor, 3)
    return t, {
        "momentum": round(momentum, 3),
        "velocity": velocities,
        "sources": sorted(sources),
        "spread": round(spread, 3),
        "age_days": round(age_days, 1),
        "freshness": round(freshness, 3),
        "coverage_factor": factor,
    }


def priority(value: float | None, t: float) -> float:
    v = DEFAULT_VALUE if value is None else value
    return round(v * (0.35 + 0.65 * t), 1)


def is_window(row: Any, value: float | None, t: float, cfg: dict[str, Any]) -> bool:
    return (
        value is not None
        and value >= float(cfg.get("window_min_value", 70))
        and t >= float(cfg.get("window_min_timeliness", 0.5))
        and row["coverage"] in (None, "none", "light")
        and row["status"] in ("new", "watching", "tried")
    )


def rank_all(db: DB, cfg: Config, at: datetime | None = None) -> int:
    at = at or now()
    rcfg = cfg.section("rank")
    rows = db.products("status IN (%s)" % ",".join("?" * len(ACTIVE_STATUSES)), ACTIVE_STATUSES)
    for row in rows:
        t, detail = timeliness(db, row, at)
        db.conn.execute(
            "UPDATE products SET timeliness = ?, timeliness_detail = ?, priority = ?, window = ?, ranked_at = ?"
            " WHERE id = ?",
            (t, json.dumps(detail), priority(row["value"], t),
             int(is_window(row, row["value"], t, rcfg)), iso(at), row["id"]),
        )
    db.conn.commit()
    return len(rows)
