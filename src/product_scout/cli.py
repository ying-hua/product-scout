"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from . import __version__, coverage, rank, report, scoring
from .config import CONFIG_NAME, Config, default_config_text, default_prompt_text, load_config
from .db import DB, STATUS_LABELS, STATUSES
from .pipeline import fetch_all, make_http, run_daily
from .sources import REGISTRY

ENV_EXAMPLE = """# 复制成 .env 后填写。.env 不要提交到 git。
SCOUT_LLM_API_KEY=
# 可选：GitHub token，限额更高
GITHUB_TOKEN=
"""


def _utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                # line_buffering: progress shows up in log files while a scheduled run is going
                stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            except (ValueError, OSError):
                pass


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    files = {
        root / CONFIG_NAME: default_config_text(),
        root / "prompts" / "value-score.md": default_prompt_text("value-score.md"),
        root / ".env.example": ENV_EXAMPLE,
        root / ".gitignore": ".env\ndata/\n",
    }
    for path, text in files.items():
        if path.exists():
            print(f"  已存在，跳过：{path}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"  已创建：{path}")
    print("\n下一步：把 .env.example 复制成 .env 并填上大模型 Key，然后运行 product-scout run")
    return 0


def _db(cfg: Config) -> DB:
    return DB(cfg.db_path)


def cmd_run(args: argparse.Namespace, cfg: Config) -> int:
    db = _db(cfg)
    s = run_daily(db, cfg)
    out = _write_report(db, cfg)
    print(f"\n完成：新产品 {s['new']} 个，打分 {s['scored']} 个，检查覆盖度 {s['coverage']} 个。")
    print(f"日报：{out}")
    print("打开选题池：product-scout serve")
    return 0


def cmd_fetch(args: argparse.Namespace, cfg: Config) -> int:
    fetch_all(_db(cfg), cfg, only=args.source or None)
    return 0


def cmd_score(args: argparse.Namespace, cfg: Config) -> int:
    db = _db(cfg)
    n = scoring.score_pending(db, make_http(cfg), cfg, limit=args.limit)
    rank.rank_all(db, cfg)
    print(f"打分 {n} 个")
    return 0


def cmd_coverage(args: argparse.Namespace, cfg: Config) -> int:
    db = _db(cfg)
    n = coverage.check_pending(db, cfg, force=args.force)
    rank.rank_all(db, cfg)
    print(f"检查 {n} 个")
    return 0


def cmd_rank(args: argparse.Namespace, cfg: Config) -> int:
    print(f"重新排序 {rank.rank_all(_db(cfg), cfg)} 个")
    return 0


def cmd_list(args: argparse.Namespace, cfg: Config) -> int:
    db = _db(cfg)
    if args.status == "window":
        rows = db.products("window = 1", order=report.ORDER)
    elif args.status:
        rows = db.products("status = ?", (args.status,), order=report.ORDER)
    else:
        rows = db.products("status IN ('new','watching','tried','scheduled')", order=report.ORDER)
    print(f"{'ID':>4}  {'优先':>4}  {'价值':>4}  {'时效':>4}  {'状态':<4}  名称")
    for r in rows[: args.limit]:
        flag = "🔥" if r["window"] else "  "
        print(f"{r['id']:>4}  {report._fmt(r['priority']):>4}  {report._fmt(r['value']):>4}  "
              f"{report._fmt(r['timeliness'], 2):>4}  {STATUS_LABELS[r['status']]:<4}{flag} {r['name']}")
    return 0


def cmd_status(args: argparse.Namespace, cfg: Config) -> int:
    db = _db(cfg)
    try:
        db.set_status(args.id, args.status)
    except KeyError:
        print(f"找不到 ID {args.id}", file=sys.stderr)
        return 1
    rank.rank_all(db, cfg)
    print(f"{args.id} → {STATUS_LABELS[args.status]}")
    return 0


def _write_report(db: DB, cfg: Config) -> Path:
    out = cfg.data_dir / "reports" / f"{datetime.now():%Y-%m-%d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.markdown(db, int(cfg.section("rank").get("top_n", 10))), encoding="utf-8")
    return out


def cmd_report(args: argparse.Namespace, cfg: Config) -> int:
    db = _db(cfg)
    text = report.markdown(db, int(cfg.section("rank").get("top_n", 10)))
    if args.print:
        print(text)
    else:
        print(_write_report(db, cfg))
    return 0


def cmd_export(args: argparse.Namespace, cfg: Config) -> int:
    db = _db(cfg)
    text = {"md": lambda: report.markdown(db, int(cfg.section("rank").get("top_n", 10))),
            "rss": lambda: report.rss(db),
            "json": lambda: report.export_json(db)}[args.format]()
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(args.out)
    else:
        sys.stdout.write(text)
    return 0


def cmd_serve(args: argparse.Namespace, cfg: Config) -> int:
    from .server import serve

    serve(cfg, args.host, args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="product-scout", description="给内容创作者的选题池：发现正在变火的产品，按视频价值和时效排序。")
    p.add_argument("--version", action="version", version=f"product-scout {__version__}")
    p.add_argument("-c", "--config", help=f"配置文件路径（默认向上查找 {CONFIG_NAME}）")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="命令")

    s = sub.add_parser("init", help="在目录里生成配置文件和提示词")
    s.add_argument("dir", nargs="?", default=".")
    s.set_defaults(func=cmd_init, needs_cfg=False)

    sub.add_parser("run", help="每日流程：抓取 → 打分 → 覆盖度 → 排序 → 日报").set_defaults(func=cmd_run)

    s = sub.add_parser("fetch", help="只抓取信源")
    s.add_argument("--source", action="append", choices=list(REGISTRY), help="只抓这个信源（可重复）")
    s.set_defaults(func=cmd_fetch)

    s = sub.add_parser("score", help="给还没打分的产品打视频价值分")
    s.add_argument("--limit", type=int)
    s.set_defaults(func=cmd_score)

    s = sub.add_parser("coverage", help="检查中文圈有没有人做过")
    s.add_argument("--force", action="store_true", help="忽略 recheck_hours，全部重查")
    s.set_defaults(func=cmd_coverage)

    sub.add_parser("rank", help="重新计算时效分和优先级").set_defaults(func=cmd_rank)

    s = sub.add_parser("list", help="在终端列出选题池")
    s.add_argument("--status", choices=["window", *STATUSES])
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("status", help="修改产品状态")
    s.add_argument("id", type=int)
    s.add_argument("status", choices=STATUSES)
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("report", help="生成今天的 Markdown 日报")
    s.add_argument("--print", action="store_true", help="打印到终端而不是写文件")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("export", help="导出为 Markdown / RSS / JSON")
    s.add_argument("--format", choices=["md", "rss", "json"], default="json")
    s.add_argument("--out")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("serve", help="打开本地网页管理选题池")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    args = build_parser().parse_args(argv)
    if getattr(args, "needs_cfg", True) is False:
        return args.func(args)
    cfg = load_config(Path(args.config) if args.config else None)
    return args.func(args, cfg)
