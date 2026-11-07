"""Video value: how good would this product be as a video? Scored by an LLM, twice, independently."""

from __future__ import annotations

import re
import statistics
from typing import Any, Callable

from .config import Config
from .db import DB, iso
from .llm import LLMError, RefusalError, make_llm
from .net import Http

DIMENSIONS = ["visual", "hands_on", "paid_alternative", "audience_fit", "novelty"]
KNOWN_RISKS = {"nsfw", "grey", "copyright", "security", "hardware"}
DISAGREEMENT = 15.0  # if two scores differ by more than this (0-100), ask a third time
IRRELEVANT_CAP = 20.0

# JSON schema for structured outputs (used by providers that support it, e.g. Claude).
SCORE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "dimensions": {
            "type": "object",
            "properties": {d: {"type": "number"} for d in DIMENSIONS},
            "required": DIMENSIONS,
            "additionalProperties": False,
        },
        "risks": {"type": "array", "items": {"type": "string", "enum": sorted(KNOWN_RISKS)}},
        "pitch": {"type": "string"},
        "angle": {"type": "string"},
        "category": {"type": "string"},
    },
    "required": ["relevant", "dimensions", "risks", "pitch", "angle", "category"],
    "additionalProperties": False,
}

Log = Callable[[str], None]


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    dims_in = raw.get("dimensions") or {}
    dims = {}
    for d in DIMENSIONS:
        try:
            dims[d] = max(0.0, min(10.0, float(dims_in.get(d, 0))))
        except (TypeError, ValueError):
            dims[d] = 0.0
    risks = [str(r).strip().lower() for r in (raw.get("risks") or []) if str(r).strip()]
    relevant = raw.get("relevant", True)
    if isinstance(relevant, str):
        relevant = relevant.strip().lower() not in ("false", "no", "0")
    return {
        "relevant": bool(relevant),
        "dimensions": dims,
        "risks": sorted({r for r in risks if r in KNOWN_RISKS}),
        "pitch": str(raw.get("pitch") or "").strip(),
        "angle": str(raw.get("angle") or "").strip(),
        "category": str(raw.get("category") or "").strip(),
    }


def weighted(dims: dict[str, float], weights: dict[str, float]) -> float:
    total = sum(max(0.0, float(weights.get(d, 0))) for d in DIMENSIONS) or 1.0
    return sum(dims[d] * max(0.0, float(weights.get(d, 0))) for d in DIMENSIONS) / total * 10.0


def combine(results: list[dict[str, Any]], weights: dict[str, float], penalties: dict[str, float]) -> dict[str, Any]:
    """Merge independent scorings: per-dimension median, union of risks, majority vote on relevance."""
    dims = {d: statistics.median(r["dimensions"][d] for r in results) for d in DIMENSIONS}
    risks = sorted({x for r in results for x in r["risks"]})
    relevant = sum(r["relevant"] for r in results) * 2 >= len(results)
    base = weighted(dims, weights)
    value = base
    for risk in risks:
        value *= float(penalties.get(risk, 1.0))
    if not relevant:
        value = min(value, IRRELEVANT_CAP)
    first = results[0]
    return {
        "value": round(value, 1),
        "detail": {
            "base": round(base, 1),
            "dimensions": dims,
            "risks": risks,
            "relevant": relevant,
            "pitch": first["pitch"],
            "angle": first["angle"],
            "category": first["category"],
            "runs": [round(weighted(r["dimensions"], weights), 1) for r in results],
        },
    }


def score_with(ask: Callable[[], dict[str, Any]], weights: dict[str, float], penalties: dict[str, float]) -> dict[str, Any]:
    results = [normalize(ask()), normalize(ask())]
    a, b = (weighted(r["dimensions"], weights) for r in results)
    if abs(a - b) > DISAGREEMENT:
        results.append(normalize(ask()))
    return combine(results, weights, penalties)


def _strip_markup(text: str) -> str:
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)  # images
    text = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.S)  # front matter
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def product_brief(db: DB, row: Any, readme: str) -> str:
    lines = [
        f"名称：{row['name']}",
        f"链接：{row['url']}",
        f"简介：{row['description'] or '（无）'}",
        f"创建时间：{row['created_at'] or '未知'}",
        "出现在：",
    ]
    for m in db.mentions(row["id"]):
        metric = f"，{m['metric_name']}={int(m['metric_value'])}" if m["metric_name"] and m["metric_value"] is not None else ""
        lines.append(f"- {m['source']}：{m['title']}{metric}")
    if readme:
        lines += ["", "README 摘录：", readme]
    return "\n".join(lines)


def fetch_readme(http: Http, url: str | None, limit: int) -> str:
    if not url:
        return ""
    try:
        return _strip_markup(http.text(url, retries=1, timeout=20))[:limit]
    except Exception:
        return ""


def score_pending(db: DB, http: Http, cfg: Config, log: Log = print, limit: int | None = None) -> int:
    llm_cfg = cfg.section("llm")
    llm = make_llm(http, llm_cfg, proxy=cfg.section("network").get("proxy") or None)
    if getattr(llm, "import_error", ""):
        log(f"  跳过打分：{llm.import_error}")
        return 0
    if not llm.available:
        log(f"  跳过打分：没有设置大模型 Key（环境变量 {llm_cfg.get('api_key_env')}）")
        return 0
    weights = cfg.section("scoring", "weights")
    penalties = cfg.section("scoring", "risk_penalty")
    system = cfg.prompt("value-score.md")
    limit = limit if limit is not None else int(llm_cfg.get("max_new_per_run", 40))
    rows = db.products(
        "value IS NULL AND status IN ('new','watching','tried','scheduled')",
        order="COALESCE(priority, 0) DESC, id DESC",  # most promising first
    )[:limit]
    done = 0
    for row in rows:
        readme = fetch_readme(http, row["readme_url"], int(llm_cfg.get("readme_chars", 4000)))
        brief = product_brief(db, row, readme)
        try:
            result = score_with(lambda: llm.chat_json(system, brief, SCORE_SCHEMA), weights, penalties)
        except RefusalError as e:
            # Something the model won't touch is not a video topic either; park it at 0.
            detail = {"refused": str(e), "risks": ["security"], "relevant": False, "pitch": "", "angle": "", "category": ""}
            db.update_product(row["id"], value=0.0, value_detail=detail, scored_at=iso())
            log(f"    0.0  {row['name']}  {e}")
            continue
        except Exception as e:  # one bad product shouldn't stop the run
            log(f"  打分失败 {row['name']}：{e}")
            continue
        db.update_product(row["id"], value=result["value"], value_detail=result["detail"], scored_at=iso())
        done += 1
        log(f"  {result['value']:5.1f}  {row['name']}  {result['detail']['pitch']}")
    return done
