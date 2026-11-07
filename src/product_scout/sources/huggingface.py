"""Hugging Face: trending Spaces (each one is a live demo you can screen-record)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..db import Item, now, parse_iso
from ..keys import product_key
from ..net import Http

NAME = "huggingface"


def _to_item(s: dict[str, Any]) -> Item | None:
    sid = s.get("id") or ""
    url = f"https://huggingface.co/spaces/{sid}"
    key = product_key(url)
    if not key:
        return None
    card = s.get("cardData") or {}
    name = card.get("title") or sid.split("/")[-1]
    return Item(
        source=NAME,
        external_id=f"space:{sid}",
        key=key,
        name=str(name),
        url=url,
        mention_url=url,
        title=sid,
        description=card.get("short_description") or "",
        metric_name="likes",
        metric_value=float(s.get("likes") or 0),
        posted_at=s.get("createdAt"),
        created_at=s.get("createdAt"),
        readme_url=f"https://huggingface.co/spaces/{sid}/raw/main/README.md",
        extra={"trending_score": s.get("trendingScore"), "sdk": s.get("sdk")},
    )


def fetch(http: Http, cfg: dict[str, Any]) -> list[Item]:
    limit = int(cfg.get("limit", 60))
    data = http.json(
        f"https://huggingface.co/api/spaces?sort=trendingScore&direction=-1&limit={limit}&full=true"
    )
    min_likes = float(cfg.get("min_likes", 20))
    oldest = now() - timedelta(days=int(cfg.get("max_age_days", 120)))
    items = []
    for s in data:
        created = parse_iso(s.get("createdAt"))
        if (s.get("likes") or 0) < min_likes or (created and created < oldest):
            continue
        it = _to_item(s)
        if it:
            items.append(it)
    return items
