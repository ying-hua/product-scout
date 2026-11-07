"""Coverage check: has anyone in your target community already made videos about this product?

A provider module exposes `NAME` and `search(http, query) -> list[Video]`. To support another
platform or language community, add a module and register it in PROVIDERS.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from ..config import Config
from ..db import DB, iso, now, parse_iso
from ..net import Http
from . import bilibili

PROVIDERS = {bilibili.NAME: bilibili}

NONE, LIGHT, HEAVY = "none", "light", "heavy"
COVERAGE_LABELS = {NONE: "无人讲", LIGHT: "少量", HEAVY: "已扎堆", None: "未检查"}
HEAVY_VIDEOS = 3
HEAVY_PLAYS = 20000


@dataclass
class Video:
    title: str
    url: str
    published: datetime | None
    plays: int


_SEP = re.compile(r"[\s\-_.·]+")


def _norm(s: str) -> str:
    return _SEP.sub("", s.lower())


def name_pattern(name: str) -> re.Pattern[str] | None:
    """'Craft Anything' matches 'craft-anything' and 'CraftAnything', but not 'Minecraft anything'."""
    tokens = [re.escape(t) for t in _SEP.split(name.lower()) if t]
    if not tokens or len("".join(tokens)) < 3:
        return None
    return re.compile(r"(?<![a-z0-9])" + r"[\s\-_.·]*".join(tokens) + r"(?![a-z0-9])")


def query_terms(row: Any) -> list[str]:
    terms = [row["name"]]
    if row["key"].startswith(("github:", "hf-space:")):
        repo = row["key"].split("/")[-1]
        if _norm(repo) != _norm(row["name"]):
            terms.append(repo)
    return terms


def assess(videos: list[Video], terms: list[str], since: datetime | None) -> dict[str, Any]:
    """Keep videos whose title names the product and that came out after it appeared."""
    patterns = [p for p in (name_pattern(t) for t in terms) if p]
    hits, seen = [], set()
    for v in videos:
        if v.url in seen:
            continue
        if not any(p.search(v.title.lower()) for p in patterns):
            continue
        if since and v.published and v.published < since:
            continue
        seen.add(v.url)
        hits.append(v)
    plays = sum(v.plays for v in hits)
    if not hits:
        level = NONE
    elif len(hits) >= HEAVY_VIDEOS or plays >= HEAVY_PLAYS:
        level = HEAVY
    else:
        level = LIGHT
    hits.sort(key=lambda v: v.plays, reverse=True)
    return {
        "level": level,
        "videos": len(hits),
        "plays": plays,
        "top": [{"title": v.title, "url": v.url, "plays": v.plays,
                 "published": iso(v.published) if v.published else None} for v in hits[:5]],
    }


def check_pending(db: DB, cfg: Config, log: Callable[[str], None] = print, force: bool = False) -> int:
    c = cfg.section("coverage")
    if not c.get("enabled", True):
        return 0
    provider = PROVIDERS.get(c.get("provider", "bilibili"))
    if provider is None:
        log(f"  未知的覆盖度检查平台：{c.get('provider')}")
        return 0
    http = Http(
        proxy=(cfg.section("network").get("proxy") or None) if c.get("use_proxy") else None,
        use_env_proxy=bool(c.get("use_proxy", False)),
        timeout=float(cfg.section("network").get("timeout", 30)),
    )
    stale = iso(now() - timedelta(hours=float(c.get("recheck_hours", 24))))
    where = "status IN ('new','watching','tried','scheduled') AND COALESCE(value, 100) >= ?"
    params: list[Any] = [float(c.get("min_value", 40))]
    if not force:
        where += " AND (coverage_checked_at IS NULL OR coverage_checked_at < ?)"
        params.append(stale)
    rows = db.products(where, params, order="COALESCE(priority, value, 0) DESC")[: int(c.get("max_per_run", 40))]
    done = 0
    for row in rows:
        terms = query_terms(row)
        videos: list[Video] = []
        try:
            for t in terms:
                videos += provider.search(http, t)
        except Exception as e:
            log(f"  覆盖度检查失败 {row['name']}：{e}")
            continue
        since = parse_iso(row["created_at"]) or parse_iso(row["first_seen"])
        if since:
            since -= timedelta(days=2)
        result = assess(videos, terms, since)
        db.update_product(row["id"], coverage=result["level"], coverage_detail=result, coverage_checked_at=iso())
        done += 1
        log(f"  {COVERAGE_LABELS[result['level']]:>4}  {row['name']}（{result['videos']} 个视频，{result['plays']} 播放）")
    return done
