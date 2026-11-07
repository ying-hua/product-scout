"""Hacker News (via the Algolia API): Show HN launches, plus popular stories linking to GitHub."""

from __future__ import annotations

import html
import re
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any

from ..db import Item, iso
from ..keys import product_key
from ..net import Http
from . import github

NAME = "hackernews"
API = "https://hn.algolia.com/api/v1/search_by_date"

_SPLIT = re.compile(r"\s+[–—-]\s+|:\s+|,\s+")


def product_name(title: str) -> str:
    """'Show HN: Subvocal – Talk to your computer' -> 'Subvocal'."""
    t = re.sub(r"^(show|launch)\s+hn\s*:\s*", "", title, flags=re.I).strip()
    head = _SPLIT.split(t, maxsplit=1)[0].strip()
    head = re.sub(r"^(i built|i made|we built|introducing)\s+", "", head, flags=re.I)
    return head[:60] or t[:60]


def _query(http: Http, params: dict[str, Any]) -> list[dict[str, Any]]:
    url = API + "?" + urllib.parse.urlencode(params)
    return http.json(url).get("hits", [])


def _to_item(h: dict[str, Any]) -> Item:
    oid = h["objectID"]
    hn_url = f"https://news.ycombinator.com/item?id={oid}"
    link = h.get("url") or ""
    key = product_key(link) if link else None
    if not key or key.startswith("url:"):
        key = f"hn:{oid}"
        link = link or hn_url
    title = html.unescape(h.get("title") or "")
    created = datetime.fromtimestamp(int(h.get("created_at_i") or 0), tz=timezone.utc)
    readme = None
    name = product_name(title)
    if key.startswith("github:"):
        readme = github.readme_url(key.removeprefix("github:"))
        name = [s for s in urllib.parse.urlparse(link).path.split("/") if s][1]
    elif key.startswith("site:") and len(name.split()) > 4:
        name = key.removeprefix("site:")  # a sentence, not a name: the domain reads better
    return Item(
        source=NAME,
        external_id=f"item:{oid}",
        key=key,
        name=name,
        url=link,
        mention_url=hn_url,
        title=title,
        description=html.unescape(re.sub(r"<[^>]+>", " ", h.get("story_text") or ""))[:600].strip(),
        metric_name="hn_points",
        metric_value=float(h.get("points") or 0),
        posted_at=iso(created),
        readme_url=readme,
        extra={"comments": h.get("num_comments")},
    )


def fetch(http: Http, cfg: dict[str, Any]) -> list[Item]:
    since = int(time.time()) - int(cfg.get("days", 7)) * 86400
    hits = _query(http, {
        "tags": "show_hn",
        "numericFilters": f"points>{int(cfg.get('min_points', 50))},created_at_i>{since}",
        "hitsPerPage": 100,
    })
    if cfg.get("github_stories", True):
        hits += _query(http, {
            "tags": "story",
            "query": "github.com",
            "restrictSearchableAttributes": "url",
            "numericFilters": f"points>{int(cfg.get('github_min_points', 100))},created_at_i>{since}",
            "hitsPerPage": 100,
        })
    seen: set[str] = set()
    items = []
    for h in hits:
        if h["objectID"] in seen:
            continue
        seen.add(h["objectID"])
        if "show_hn" not in (h.get("_tags") or []) and not is_repo_root(h.get("url") or ""):
            continue  # a story about an issue, commit or blog post, not a launch
        items.append(_to_item(h))
    return items


def is_repo_root(url: str) -> bool:
    """True for https://github.com/owner/repo (not /issues/1, /blob/..., /pull/...)."""
    p = urllib.parse.urlparse(url)
    if (p.hostname or "").lower().removeprefix("www.") != "github.com":
        return False
    parts = [s for s in p.path.split("/") if s]
    return len(parts) == 2
