"""Tiny HTTP helpers on top of urllib (no third-party dependencies)."""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from typing import Any

USER_AGENT = "product-scout/0.1"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.status = status
        self.body = body


def ssl_context() -> ssl.SSLContext:
    """System CA store, plus certifi's bundle when it's installed.

    Some Python installs ship a stale CA store that can't verify newer chains
    (api.github.com moved to a new Sectigo CA in 2026); certifi fixes that.
    """
    ctx = ssl.create_default_context()
    try:
        import certifi

        ctx.load_verify_locations(cafile=certifi.where())
    except (ImportError, OSError):
        pass
    return ctx


class Http:
    """A urllib opener with an optional proxy, cookies and simple retries."""

    def __init__(self, proxy: str | None = None, use_env_proxy: bool = True, timeout: float = 30):
        handlers: list[Any] = [
            urllib.request.HTTPCookieProcessor(CookieJar()),
            urllib.request.HTTPSHandler(context=ssl_context()),
        ]
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        elif not use_env_proxy:
            handlers.append(urllib.request.ProxyHandler({}))
        self.opener = urllib.request.build_opener(*handlers)
        self.timeout = timeout

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        retries: int = 2,
        timeout: float | None = None,
    ) -> bytes:
        hdrs = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity", **(headers or {})}
        last: Exception | None = None
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
            try:
                with self.opener.open(req, timeout=timeout or self.timeout) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")
                last = HttpError(e.code, url, text)
                if e.code not in (429, 500, 502, 503, 504):
                    raise last from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
        assert last is not None
        raise last

    def text(self, url: str, **kw: Any) -> str:
        return self.request(url, **kw).decode("utf-8", "replace")

    def json(self, url: str, **kw: Any) -> Any:
        return json.loads(self.request(url, **kw).decode("utf-8"))

    def post_json(self, url: str, payload: Any, headers: dict[str, str] | None = None, **kw: Any) -> Any:
        data = json.dumps(payload).encode("utf-8")
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        return json.loads(self.request(url, method="POST", headers=hdrs, body=data, **kw).decode("utf-8"))
