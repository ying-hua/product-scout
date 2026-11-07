"""GitHub: recently created repositories that are gaining stars fast."""

from __future__ import annotations

import os
import urllib.parse
from datetime import timedelta
from typing import Any

from ..db import DB, Item, iso, now
from ..keys import product_key
from ..net import Http, HttpError

NAME = "github"
API = "https://api.github.com"


def _headers(cfg: dict[str, Any]) -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get(cfg.get("token_env") or "GITHUB_TOKEN", "")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def readme_url(full_name: str) -> str:
    return f"https://raw.githubusercontent.com/{full_name}/HEAD/README.md"


def _to_item(r: dict[str, Any]) -> Item | None:
    key = product_key(r["html_url"])
    if not key:
        return None
    return Item(
        source=NAME,
        external_id=f"repo:{r['id']}",
        key=key,
        name=r["name"],
        url=r["html_url"],
        mention_url=r["html_url"],
        title=r["full_name"],
        description=r.get("description") or "",
        metric_name="stars",
        metric_value=float(r.get("stargazers_count") or 0),
        posted_at=r.get("created_at"),
        created_at=r.get("created_at"),
        readme_url=readme_url(r["full_name"]),
        extra={"homepage": r.get("homepage") or "", "language": r.get("language") or ""},
    )


def fetch(http: Http, cfg: dict[str, Any]) -> list[Item]:
    since = (now() - timedelta(days=int(cfg.get("days", 21)))).strftime("%Y-%m-%d")
    q = f"created:>{since} stars:>{int(cfg.get('min_stars', 300))} {cfg.get('query_extra', '')}".strip()
    items: list[Item] = []
    for page in range(1, int(cfg.get("pages", 2)) + 1):
        url = f"{API}/search/repositories?" + urllib.parse.urlencode(
            {"q": q, "sort": "stars", "order": "desc", "per_page": 50, "page": page}
        )
        data = http.json(url, headers=_headers(cfg))
        batch = data.get("items", [])
        items += [it for it in (_to_item(r) for r in batch) if it]
        if len(batch) < 50:
            break
    return items


def refresh(db: DB, http: Http, cfg: dict[str, Any], skip_keys: set[str]) -> int:
    """Re-read star counts for pool repos that didn't show up in today's search, so velocity stays current."""
    limit = int(cfg.get("refresh_limit", 30))
    if limit <= 0:
        return 0
    rows = db.products(
        "key LIKE 'github:%' AND status IN ('new','watching','tried','scheduled')",
        order="COALESCE(priority, 0) DESC",
    )
    done = 0
    for row in rows:
        if done >= limit:
            break
        if row["key"] in skip_keys:
            continue
        full = row["key"].removeprefix("github:")
        try:
            r = http.json(f"{API}/repos/{full}", headers=_headers(cfg), retries=0)
        except HttpError as e:
            if e.status in (403, 429):  # rate limited: stop quietly
                break
            continue
        db.add_snapshot(row["id"], "stars", float(r.get("stargazers_count") or 0))
        db.update_product(row["id"], last_seen=iso())
        done += 1
    db.conn.commit()
    return done
