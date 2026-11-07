"""The daily run: fetch -> score -> coverage -> rank."""

from __future__ import annotations

import os
from typing import Any, Callable

from . import coverage, rank, scoring
from .config import Config
from .db import DB
from .net import Http
from .sources import REGISTRY, github

Log = Callable[[str], None]


def make_http(cfg: Config) -> Http:
    net = cfg.section("network")
    return Http(proxy=net.get("proxy") or None, timeout=float(net.get("timeout", 30)))


def fetch_all(db: DB, cfg: Config, log: Log = print, only: list[str] | None = None) -> dict[str, Any]:
    http = make_http(cfg)
    summary: dict[str, Any] = {"sources": {}, "new": 0}
    seen_keys: set[str] = set()
    for name, mod in REGISTRY.items():
        scfg = cfg.section("sources", name)
        if only and name not in only:
            continue
        if not scfg.get("enabled", True):
            continue
        try:
            items = mod.fetch(http, scfg)
        except Exception as e:
            log(f"  {name}: 抓取失败：{e}")
            summary["sources"][name] = {"error": str(e)[:300]}
            continue
        new = 0
        for it in items:
            _, is_new = db.upsert_item(it)
            new += is_new
            seen_keys.add(it.key)
        summary["sources"][name] = {"items": len(items), "new": new}
        summary["new"] += new
        log(f"  {name}: {len(items)} 条，新产品 {new} 个")
    if (not only or "github" in only) and cfg.section("sources", "github").get("enabled", True):
        try:
            n = github.refresh(db, http, cfg.section("sources", "github"), seen_keys)
            if n:
                log(f"  github: 刷新了 {n} 个池中仓库的 star 数")
        except Exception as e:
            log(f"  github: 刷新失败：{e}")
    return summary


def run_daily(db: DB, cfg: Config, log: Log = print) -> dict[str, Any]:
    run_id = db.start_run("daily")
    log("抓取信源 …")
    summary = fetch_all(db, cfg, log)
    log("计算时效分 …")
    rank.rank_all(db, cfg)  # rank first so coverage checks go to the most promising products
    log("给新产品打分 …")
    summary["scored"] = scoring.score_pending(db, make_http(cfg), cfg, log)
    log("检查中文圈覆盖度 …")
    summary["coverage"] = coverage.check_pending(db, cfg, log)
    log("重新排序 …")
    summary["ranked"] = rank.rank_all(db, cfg)
    db.finish_run(run_id, summary)
    return summary


def llm_key_hint(cfg: Config) -> str | None:
    env = cfg.section("llm").get("api_key_env") or "SCOUT_LLM_API_KEY"
    return None if os.environ.get(env) else env
