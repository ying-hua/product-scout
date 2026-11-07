"""Bilibili video search (public web API, no login)."""

from __future__ import annotations

import html
import re
import time
import urllib.parse
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..net import BROWSER_UA, Http

if TYPE_CHECKING:
    from . import Video

NAME = "bilibili"
_HEADERS = {"User-Agent": BROWSER_UA, "Referer": "https://search.bilibili.com/"}
_warmed: set[int] = set()


def _warm(http: Http) -> None:
    # The search API wants the buvid cookies that the home page sets.
    if id(http) not in _warmed:
        http.request("https://www.bilibili.com/", headers={"User-Agent": BROWSER_UA})
        _warmed.add(id(http))


def search(http: Http, query: str) -> list["Video"]:
    from . import Video

    _warm(http)
    url = "https://api.bilibili.com/x/web-interface/search/type?" + urllib.parse.urlencode(
        {"search_type": "video", "keyword": query, "order": "totalrank"}
    )
    data = http.json(url, headers=_HEADERS)
    time.sleep(0.6)  # be polite
    if data.get("code") != 0:
        raise RuntimeError(f"bilibili: {data.get('code')} {data.get('message')}")
    out = []
    for r in (data.get("data") or {}).get("result") or []:
        title = html.unescape(re.sub(r"<[^>]+>", "", r.get("title") or ""))
        pub = r.get("pubdate")
        out.append(Video(
            title=title,
            url=f"https://www.bilibili.com/video/{r.get('bvid')}",
            published=datetime.fromtimestamp(pub, tz=timezone.utc) if pub else None,
            plays=int(r.get("play") or 0) if str(r.get("play") or "0").isdigit() else 0,
        ))
    return out
