"""Turn any URL into a stable product key, so the same product from different sources merges into one."""

from __future__ import annotations

import re
from urllib.parse import urlparse

# Hosts where the domain says nothing about the product; key on the full path instead.
_PLATFORM_HOSTS = {
    "twitter.com", "x.com", "youtube.com", "youtu.be", "medium.com", "reddit.com",
    "news.ycombinator.com", "producthunt.com", "substack.com", "linkedin.com",
    "docs.google.com", "drive.google.com", "arxiv.org", "gist.github.com",
    "apps.apple.com", "play.google.com", "chromewebstore.google.com", "npmjs.com", "pypi.org",
}
_GITHUB_RESERVED = {
    "orgs", "topics", "trending", "marketplace", "sponsors", "settings", "features",
    "collections", "explore", "about", "login", "search",
}


def _host(url: str) -> tuple[str, list[str]]:
    p = urlparse(url.strip())
    host = (p.hostname or "").lower().removeprefix("www.")
    parts = [s for s in p.path.split("/") if s]
    return host, parts


def product_key(url: str) -> str | None:
    if not url:
        return None
    host, parts = _host(url)
    if not host:
        return None
    if host == "github.com":
        if len(parts) >= 2 and parts[0].lower() not in _GITHUB_RESERVED:
            repo = re.sub(r"\.git$", "", parts[1])
            return f"github:{parts[0].lower()}/{repo.lower()}"
        return None
    if host == "huggingface.co":
        if len(parts) >= 3 and parts[0] == "spaces":
            return f"hf-space:{parts[1].lower()}/{parts[2].lower()}"
        if len(parts) >= 2 and parts[0] not in ("spaces", "datasets", "papers", "blog", "docs"):
            return f"hf-model:{parts[0].lower()}/{parts[1].lower()}"
        return None
    if host.endswith(".hf.space"):
        # owner-space-name.hf.space can't be split reliably; keep it as its own key.
        return f"site:{host}"
    if host.endswith("github.io"):
        user = host.split(".")[0]
        return f"github-pages:{user}/{parts[0].lower()}" if parts else f"site:{host}"
    if host in _PLATFORM_HOSTS or any(host.endswith("." + h) for h in _PLATFORM_HOSTS):
        path = "/".join(parts[:3])
        return f"url:{host}/{path}".rstrip("/")
    return f"site:{host}"


def canonical_url(key: str) -> str:
    kind, _, rest = key.partition(":")
    if kind == "github":
        return f"https://github.com/{rest}"
    if kind == "hf-space":
        return f"https://huggingface.co/spaces/{rest}"
    if kind == "hf-model":
        return f"https://huggingface.co/{rest}"
    if kind == "github-pages":
        user, _, proj = rest.partition("/")
        return f"https://{user}.github.io/{proj}"
    return f"https://{rest}"
