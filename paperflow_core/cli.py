"""命令行入口：整个工具只有三条命令。

    init      第一次用，配置 Obsidian 库、第 1 周起始日和长期目标位置
    week      每周跑一次：回顾上周、检索筛选、推 Zotero、写清单和周报
    summary   周 / 月总结，给人看的

设计原则只有一条：**推荐必须来自 AI。**
清单是 AI 读过你的笔记和长期目标之后挑的；没有可用的模型就直接报错退出（退出码 1），
**不写清单、不写周报、不推 Zotero、不记账本**，所以下次会重跑同一周——
而不是拿一份「按分数排个序」的假推荐去污染你的状态。

（`summary` 是例外：它主要在算数字，没配模型就只出数字。）

硬边界：脚本**不碰 git**，也不替你写代码。工程部分只做推荐。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from typing import List, Optional

from . import ledger as ledger_mod
from . import summary as summary_mod
from . import vault as vault_mod
from . import weekly as weekly_mod
from .config import default_dotenv_paths, default_state_dir, default_vault, load_dotenv, resolve_lang
from .llm import LLMError, get_client
from .texts import SUPPORTED_LANGS, pick
from .vault import VaultConfig

WEEK_NOTE_TEMPLATE = """# 本周

（选填，一两句话就行。写你此刻卡在哪、想搞清楚什么。
脚本每次都会读它，写在这里的比写在笔记里更容易影响推荐。）

"""

GOALS_TEMPLATE = """# 长期目标

（这个文件是你唯一需要认真写的东西。脚本只读它，从不改写。
写清楚：六个月后你希望自己能讲清什么、能做什么。
每条单独一行，越具体越好——比如
「能说清 metastability 和 criticality 的区别，并知道怎么从 EEG 里估计它们」。）

1. 
2. 
3. 
"""


# --------------------------------------------------------------------- 工具


def _reconfigure_console() -> None:
    """Windows 控制台默认 GBK，输出中文/作者名会直接抛异常，这里强制 UTF-8。

    stdin 也一并改：如果把中文管道进来，不改的话字节会按 GBK 解码成乱码。
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):  # pragma: no cover - 非标准流
            pass


def _parse_date(raw: Optional[str], lang: str) -> Optional[dt.date]:
    """日期写错了算参数错误：说清哪里错，然后退 2（和 argparse 一致）。

    以前这里 ``raise SystemExit("文案")``，Python 会把它当成普通错误、退出码是 1——
    和「模型不可用」撞在一起，看你自己的文档都分不清是哪一类问题。
    """
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(raw.strip()[:10])
    except ValueError:
        print(
            pick(lang, f"日期格式应为 YYYY-MM-DD，收到 {raw!r}", f"Date must be YYYY-MM-DD, got {raw!r}"),
            file=sys.stderr,
        )
        raise SystemExit(2)


def _next_monday(today: dt.date) -> dt.date:
    """默认的第 1 周起始日：本周一（今天就是周一）或下一个周一。"""
    return today + dt.timedelta(days=(7 - today.weekday()) % 7)


def _load_or_die(state_dir: Path, lang: str) -> VaultConfig:
    cfg = vault_mod.load_config(state_dir)
    if cfg is None:
        print(
            pick(
                lang,
                f"{state_dir} 里没有可用的 config.json。先跑一次：\n"
                f"  python paperflow.py init --vault <你的 Obsidian 库路径>",
                f"No usable config.json in {state_dir}. Run `init` first.",
            ),
            file=sys.stderr,
        )
        raise SystemExit(1)
    return cfg


def _blank_config(vault: Path, goals: Path, start: dt.date, per_week: int) -> VaultConfig:
    return VaultConfig(vault=vault, goals=goals, start_date=start, per_week=per_week)


# --------------------------------------------------------------------- init


def cmd_init(args: argparse.Namespace, lang: str) -> int:
    state_dir = Path(args.state_dir).resolve()
    raw_vault = args.vault or default_vault()
    if not raw_vault:
        print(
            pick(
                lang,
                "需要告诉我要往哪个 Obsidian 库写。例如：\n"
                '  python paperflow.py init --vault "D:\\Obsidian\\MyVault"\n'
                "或者把 PAPERFLOW_VAULT=D:\\Obsidian\\MyVault 写进 .env。",
                "Pass --vault (or set PAPERFLOW_VAULT) so I know where your Obsidian vault is.",
            ),
            file=sys.stderr,
        )
        return 2
    vault = Path(raw_vault).expanduser().resolve()

    existing = vault_mod.load_config(state_dir)
    if existing is not None and not args.force:
        cfg = existing
        cfg.vault = vault
        notes: List[str] = [
            pick(lang, "已经初始化过了，只补齐缺失的文件（没有覆盖任何东西）。", "Already initialised; filling in what is missing.")
        ]
    else:
        start = _parse_date(args.start, lang) or _next_monday(dt.date.today())
        goals_raw = args.goals or (vault / "长期目标" if args.goals is None else None)
        goals = Path(goals_raw).expanduser()
        if not goals.is_absolute():
            goals = vault / goals
        cfg = _blank_config(vault, goals.resolve(), start, args.per_week)
        notes = []

    vault_mod.bind(cfg, state_dir)
    cfg.vault.mkdir(parents=True, exist_ok=True)

    # 1) 周报 / 月报 / 账本
    cfg.week_report_dir.mkdir(parents=True, exist_ok=True)
    cfg.month_report_dir.mkdir(parents=True, exist_ok=True)
    if not cfg.ledger_path.exists():
        ledger_mod.write_ledger(cfg.ledger_path, [])

    # 2) 第 1 周（或当前那周）的文件夹
    today = dt.date.today()
    week = cfg.current_week(today)
    folder = cfg.ensure_week_dir(week)

    # 3) 本周.md 模板
    created_note = False
    if not cfg.week_note_path.exists():
        cfg.week_note_path.write_text(WEEK_NOTE_TEMPLATE, encoding="utf-8")
        created_note = True

    # 4) 长期目标：只在这个位置还不存在时才建模板，绝不改动你已有的文件
    goals_created = False
    if args.goals is None and not cfg.goals.exists():
        cfg.goals.mkdir(parents=True, exist_ok=True)
        (cfg.goals / "长期目标.md").write_text(GOALS_TEMPLATE, encoding="utf-8")
        goals_created = True

    vault_mod.save_config(state_dir, cfg)

    # ------------------------------------------------------------------ 汇报
    print(pick(lang, "初始化完成。", "Initialised."))
    print()
    print(pick(lang, f"- Obsidian 库：{cfg.vault}", f"- Vault: {cfg.vault}"))
    print(pick(lang, f"- 长期目标：{cfg.goals}", f"- Long-term goals: {cfg.goals}"))
    print(
        pick(
            lang,
            f"- 第 1 周从 {cfg.start_date.isoformat()} 开始（周一），每周 {cfg.per_week} 篇",
            f"- Week 1 starts {cfg.start_date.isoformat()} (Monday), {cfg.per_week} papers per week",
        )
    )
    print(pick(lang, f"- 状态目录：{state_dir}", f"- State dir: {state_dir}"))
    print(pick(lang, f"- 本周文件夹：{folder}", f"- This week's folder: {folder}"))
    print()
    for note in notes:
        print(f"  {note}")
    print(
        pick(
            lang,
            f"  本周.md {'已创建' if created_note else '已存在（没动）'}；"
            f"账本 进度.csv {'已创建' if not existing else '保留原样'}",
            f"  本周.md {'created' if created_note else 'kept'}; ledger {'created' if not existing else 'kept'}",
        )
    )
    print()

    goal_text = cfg.read_goals().strip()
    placeholder = goal_text and all(
        not line.strip("0123456789.。 、") for line in goal_text.splitlines() if not line.startswith("#")
    )
    if goals_created or not goal_text or placeholder:
        print(pick(lang, "下一步只剩一件事（别的都可以不做）：", "The only thing left to do:"))
        print()
        print(f"  1. {pick(lang, f'打开 {cfg.goals}，把长期目标写清楚', f'Fill in {cfg.goals}')}")
        print(
            "     "
            + pick(
                lang,
                "这是整套东西的输入。你写得越具体，推荐就越不像随机抽样。",
                "This is the input to everything else.",
            )
        )
        print(f"  2. {pick(lang, f'想起来的话，再去 {cfg.week_note_path} 写两句本周的卡点', f'Optionally write {cfg.week_note_path}')}")
    else:
        print(pick(lang, "长期目标已读取：", "Long-term goals loaded:"))
        print()
        print(goal_text[:1500])
    print()
    print(pick(lang, "写完之后跑：", "Then run:"))
    print()
    print("  python paperflow.py week --dry-run    " + pick(lang, "# 试跑，什么都不写", "# dry run, writes nothing"))
    print("  python paperflow.py week              " + pick(lang, "# 正式跑", "# for real"))
    return 0


# --------------------------------------------------------------------- week


def cmd_week(args: argparse.Namespace, lang: str) -> int:
    state_dir = Path(args.state_dir).resolve()
    # 先看日期：参数写错了就该报参数错误，而不是被「你还没 init」盖过去。
    today = _parse_date(args.date, lang)
    cfg = _load_or_die(state_dir, lang)
    client = get_client()

    outcome = weekly_mod.run_week(
        cfg,
        client=client,
        lang=lang,
        dry_run=args.dry_run,
        full=args.full,
        per_week=args.per_week,
        deep_slots=args.deep,
        query=args.topic or "",
        min_year=args.min_year,
        sources=args.sources,
        seeds=args.seed,
        zotero_on=not args.no_zotero,
        today=today,
        min_citations=args.min_citations,
        years=args.years,
        max_years=args.max_years,
    )

    print()
    print(pick(lang, f"=== 第{outcome.week}周（{outcome.window}）===", f"=== Week {outcome.week} ({outcome.window}) ==="))
    if outcome.dry_run:
        print(pick(lang, "—— 试跑：没有写任何文件，没有动 Zotero ——", "—— dry run: nothing written, Zotero untouched ——"))
    print(pick(lang, f"检索式：{outcome.query}", f"Queries: {outcome.query}"))
    if outcome.year_window:
        print(pick(lang, f"年份窗口：{outcome.year_window}", f"Year window: {outcome.year_window}"))
    print(
        pick(
            lang,
            f"候选 {outcome.candidates} 篇 → 选出 {len(outcome.papers)} 篇"
            f"（{weekly_mod.mode_label(outcome.mode, lang)}）",
            f"{outcome.candidates} candidates → {len(outcome.papers)} selected "
            f"({weekly_mod.mode_label(outcome.mode, lang)})",
        )
    )
    print(
        pick(
            lang,
            f"引用门槛：{weekly_mod.citation_policy(args.min_citations if args.min_citations is not None else cfg.citation_base, lang)}",
            f"Citation floor: {weekly_mod.citation_policy(args.min_citations if args.min_citations is not None else cfg.citation_base, lang)}",
        )
    )
    print(pick(lang, f"读到的笔记：{outcome.notes_used} 份", f"Notes read: {outcome.notes_used}"))
    print()
    if outcome.papers:
        for index, paper in enumerate(outcome.papers, start=1):
            label = weekly_mod.role_label(paper.get("role") or "", lang)
            print(f"  {index}. [{label}] {paper.get('title')} ({paper.get('year') or '?'})")
            if paper.get("why"):
                print(f"     {paper['why']}")
    else:
        print(pick(lang, "  这一周没有选出新论文。", "  Nothing selected this week."))
    print()
    if outcome.report_path:
        print(pick(lang, f"周报：   {outcome.report_path}", f"Report: {outcome.report_path}"))
    if outcome.queue_path:
        print(pick(lang, f"阅读清单：{outcome.queue_path}", f"Queue: {outcome.queue_path}"))
    if outcome.zotero:
        print(str(outcome.zotero.get("message") or ""))
    if outcome.warnings:
        print()
        for item in outcome.warnings:
            print(f"  ⚠ {item}")
    return 0


# ------------------------------------------------------------------ summary


def cmd_summary(args: argparse.Namespace, lang: str) -> int:
    state_dir = Path(args.state_dir).resolve()
    anchor = _parse_date(args.date, lang)
    cfg = _load_or_die(state_dir, lang)
    client = None if args.no_llm else get_client()
    outcome = summary_mod.run_summary(
        cfg,
        client=client,
        period=args.period,
        anchor=anchor,
        lang=lang,
        dry_run=args.dry_run,
    )
    print(outcome.text)
    if outcome.path:
        print(pick(lang, f"\n已写入：{outcome.path}", f"\nWritten to {outcome.path}"))
    else:
        print(pick(lang, "\n（试跑，没有写入文件）", "\n(dry run, nothing written)"))
    return 0


# ------------------------------------------------------------------ parser


def _add_common(parser: argparse.ArgumentParser) -> None:
    """给（子）命令挂上 ``--state-dir`` / ``--lang``。

    两个选项都用 ``SUPPRESS`` 作为默认值：只有用户真的写了，才会往
    ``args`` 里塞值。否则内层子命令会把外层已解析的值覆盖回默认值
    （``paperflow.py --state-dir X week`` 会被 ``week`` 改回默认目录）。
    """
    parser.add_argument("--state-dir", default=argparse.SUPPRESS, help="状态目录（默认取 PAPERFLOW_STATE_DIR）")
    parser.add_argument(
        "--lang",
        choices=list(SUPPORTED_LANGS),
        default=argparse.SUPPRESS,
        help="输出语言：zh（默认）或 en",
    )


def make_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    _add_common(common)

    parser = argparse.ArgumentParser(
        prog="paperflow",
        description="PaperFlow：发散地读论文，收敛地积累理解",
        epilog="只有三条命令：init / week / summary。脚本不碰 git，也不替你写代码。",
    )
    parser.add_argument("--state-dir", default=default_state_dir(), help="状态目录")
    parser.add_argument("--lang", choices=list(SUPPORTED_LANGS), default=None, help="输出语言：zh（默认）或 en")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", parents=[common], help="初始化：配置 Obsidian 库、第 1 周起始日、长期目标位置")
    p.add_argument("--vault", help="Obsidian 库根目录（也可用 PAPERFLOW_VAULT 环境变量）")
    p.add_argument("--goals", help="长期目标文件或文件夹（默认 <vault>/长期目标）")
    p.add_argument("--start", help="第 1 周的周一，YYYY-MM-DD（默认下一个周一）")
    p.add_argument("--per-week", type=int, default=12, help="每周推荐几篇（默认 12 = 5 篇精读 + 7 篇泛读）")
    p.add_argument(
        "--force",
        action="store_true",
        help="重建 config.json（不会删你的笔记，也不会清空账本）",
    )

    p = sub.add_parser("week", parents=[common], help="每周跑一次：回顾上周 + 检索筛选 + 推 Zotero + 写清单和周报")
    p.add_argument("--dry-run", action="store_true", help="试跑：不写文件、不动 Zotero")
    p.add_argument("--full", action="store_true", help="忽略上次运行时间，读全部历史笔记")
    p.add_argument("--per-week", type=int, help="这一周推荐几篇（默认用 init 里设的值，init 默认 12）")
    p.add_argument("--deep", type=int, help="最多几篇标为精读（默认每周篇数的 2/5：12 篇 → 5 篇）")
    p.add_argument("--topic", help="覆盖自动推断的检索式（用 | 分隔多条）")
    p.add_argument("--min-year", type=int, help="只收这个年份之后的论文")
    p.add_argument(
        "--years",
        type=int,
        help="年份窗口：优先只收近 N 年的论文（默认 5）；0 = 不限年份",
    )
    p.add_argument(
        "--max-years",
        type=int,
        help="近 N 年凑不够候选时的放宽上限（默认 10，只放宽到这个年份之后）",
    )
    p.add_argument("--sources", help="逗号分隔：s2, openalex, arxiv, pubmed。默认 auto")
    p.add_argument("--seed", action="append", help="种子论文 ID / DOI / 链接，可重复")
    p.add_argument("--no-zotero", action="store_true", help="这一周不推 Zotero")
    p.add_argument(
        "--min-citations",
        type=int,
        default=None,
        help="引用次数门槛的基数（默认 50）：老论文要求高、新论文按比例放宽；0 = 不设门槛",
    )
    p.add_argument("--date", help="YYYY-MM-DD，默认今天")

    p = sub.add_parser("summary", parents=[common], help="周 / 月总结（给人看）")
    p.add_argument("--period", choices=list(summary_mod.PERIODS), default="month")
    p.add_argument("--date", help="以哪一天为准，YYYY-MM-DD，默认今天")
    p.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    p.add_argument("--no-llm", action="store_true", help="总结里不调 AI，只出数字")

    return parser


HANDLERS = {"init": cmd_init, "week": cmd_week, "summary": cmd_summary}


def main(argv: Optional[List[str]] = None) -> int:
    _reconfigure_console()
    load_dotenv(default_dotenv_paths())
    parser = make_parser()
    args = parser.parse_args(argv)
    lang = resolve_lang(getattr(args, "lang", None))
    handler = HANDLERS.get(args.command)
    if handler is None:  # pragma: no cover - argparse 已保证
        parser.error(pick(lang, f"未知命令 {args.command}", f"Unknown command {args.command}"))
        return 2
    try:
        return handler(args, lang)
    except LLMError as exc:
        print(
            pick(
                lang,
                f"LLM 调用失败（{exc}）。你的本地数据没有被修改。",
                f"LLM call failed ({exc}). Local data was not modified.",
            ),
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:  # pragma: no cover - 交互中断
        print(pick(lang, "已中断。", "Interrupted."), file=sys.stderr)
        return 130
