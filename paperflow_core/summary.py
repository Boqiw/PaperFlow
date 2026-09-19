"""周 / 月总结：数字由脚本算，叙述由 AI 写，工程链接你自己填。

这份东西是给**人**看的（导师、或者三个月后的你自己）。所以它和
``week`` 的输出刻意不同：

* ``week`` 给你的是「下一步该读什么」——简短、可执行；
* ``summary`` 给你的是「这段时间实际发生了什么」——带数字、带没做到的事。

``summary`` 不碰 git、不碰代码仓库，GitHub 链接永远留成占位符。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import ledger, terms
from .llm import LLMClient, build_prompt
from .notes import Note, read_notes, read_pf_blocks
from .texts import pick
from .vault import VaultConfig, week_label

PERIODS = ("week", "month")
ARTIFACT_PLACEHOLDER = "<!-- TODO: 贴上你自己的 GitHub / 复现仓库链接 -->"


@dataclass
class SummaryFacts:
    period: str
    label: str
    start: dt.date
    end: dt.date
    weeks: List[int] = field(default_factory=list)
    pushed: List[Dict[str, str]] = field(default_factory=list)
    carried: List[Dict[str, str]] = field(default_factory=list)
    notes: List[Note] = field(default_factory=list)
    history: List[Dict[str, Any]] = field(default_factory=list)
    goals: str = ""
    themes: str = ""

    @property
    def stats(self) -> Dict[str, int]:
        return ledger.stats(self.pushed)

    @property
    def note_chars(self) -> int:
        return sum(len(note.text) for note in self.notes)


def _bounds(cfg: VaultConfig, period: str, anchor: dt.date) -> tuple:
    """返回 ``(标签, 起始日, 结束日, [周号...])``。"""
    if period == "month":
        start = anchor.replace(day=1)
        end = (start + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)
        first = cfg.week_of(start) or 1
        last = cfg.week_of(end)
        weeks = list(range(first, last + 1)) if last else []
        return anchor.strftime("%Y-%m"), start, end, weeks
    week = cfg.current_week(anchor)
    start, end = cfg.week_bounds(week)
    return week_label(week), start, end, [week]


def collect(cfg: VaultConfig, period: str = "month", anchor: Optional[dt.date] = None) -> SummaryFacts:
    """把总结要用到的全部事实算好（AI 只能引用这里的东西）。"""
    today = anchor or dt.date.today()
    label, start, end, weeks = _bounds(cfg, period, today)
    rows = ledger.read_ledger(cfg.ledger_path)
    # 用「到目前为止的全部笔记」对一次账，这样「已读几篇」和跨期遗留都是最新的。
    all_notes = read_notes(
        cfg, list(range(1, cfg.current_week(today) + 1)), per_note=8000, total=240000
    )
    refreshed, _ = ledger.refresh(rows, all_notes.mentions)

    def pushed_on(row: Dict[str, str]) -> Optional[dt.date]:
        raw = str(row.get("pushed_on") or "").strip()[:10]
        try:
            return dt.date.fromisoformat(raw)
        except ValueError:
            return None

    def week_no_of(row: Dict[str, str]) -> int:
        """这条论文算「第几周」的产物。

        以账本里的 ``week`` 列为准——那才是``week`` 命令当场写下的归属，
        而 ``pushed_on`` 只是「哪天按下的回车」。这两者经常不是同一周：你完全可能
        周日晚上就先跑一次下周的推荐（本项目的第 1 周就是这样起的）。要是按日期
        切窗口，那些论文会被算成「上期遗留」，总结里的「推荐了几篇」就永远是假的。
        没有周号的老账本才退回用日期推算。
        """
        try:
            week_no = int(str(row.get("week") or "").strip())
        except ValueError:
            week_no = 0
        if week_no > 0:
            return week_no
        when = pushed_on(row)
        return cfg.week_of(when) if when else 0

    window = set(weeks)
    first_week = min(window) if window else 0
    pushed: List[Dict[str, str]] = []
    carried: List[Dict[str, str]] = []
    for row in refreshed:
        week_no = week_no_of(row)
        if not week_no or not first_week:
            continue
        if week_no in window:
            pushed.append(row)
        elif week_no < first_week and (row.get("status") or "").strip() == ledger.STATUS_TODO:
            carried.append(row)

    notes = [note for note in all_notes.notes if note.week in window]
    history = [item for item in read_pf_blocks(cfg.week_report_dir) if int(item.get("week") or 0) in window]
    return SummaryFacts(
        period=period,
        label=label,
        start=start,
        end=end,
        weeks=weeks,
        pushed=pushed,
        carried=carried,
        notes=notes,
        history=history,
        goals=cfg.read_goals(),
        themes=terms.theme_line([row.get("title") or "" for row in pushed]),
    )


def facts_block(facts: SummaryFacts) -> str:
    """把事实渲染成 prompt 里的文本——**只**有这些数字可以被引用。"""
    counts = facts.stats
    lines = [
        f"Period: {facts.label} ({facts.start.isoformat()} to {facts.end.isoformat()})",
        f"Papers pushed: {counts['pushed']} (read {counts['read']}, skipped {counts['skipped']}, unmarked {counts['unmarked']})",
        f"Carry-over papers still unread from earlier periods: {len(facts.carried)}",
        f"Notes written: {len(facts.notes)} files, {facts.note_chars} characters total",
        f"Themes detected in this period's titles: {facts.themes}",
    ]
    lines.append("Papers pushed this period:")
    for row in facts.pushed:
        lines.append(
            f"  - [{row.get('status') or '未标记'}] {row.get('title')} "
            f"({row.get('role') or '-'}, week {row.get('week') or '-'})"
        )
    if not facts.pushed:
        lines.append("  (none)")
    lines.append("Carried over and still not read:")
    for row in facts.carried:
        lines.append(f"  - {row.get('title')} (week {row.get('week') or '-'})")
    if not facts.carried:
        lines.append("  (none)")
    return "\n".join(lines)


def summary_prompt(facts: SummaryFacts, lang: str = "zh") -> str:
    notes_view = "\n\n".join(
        f"### {note.path.parent.name}/{note.path.name}\n\n{note.text[:3000]}" for note in facts.notes
    ) or "(no notes in this period)"
    uncovered = [str(item.get("uncovered")) for item in facts.history if item.get("uncovered")]
    body = f"""Write a {"weekly" if facts.period == "week" else "monthly"} progress summary for a beginner in
computational neuroscience, addressed to a real human supervisor.

Hard rules:
- Use ONLY the numbers, titles and note contents given below. Never invent a paper,
  a number, a result or an experiment.
- Be explicit about what did NOT get done. A supervisor trusts honesty far more than optimism.
- No praise, no filler, no "great progress". Short sentences.
- If the notes show the student drifting away from their long-term goals, say so plainly
  and say which goal is being neglected.

## Long-term goals (the student wrote these)
{facts.goals[:5000] or '(none written yet)'}

## Numbers (computed by the script, authoritative)
{facts_block(facts)}

## Previous weeks' own assessment of what was uncovered
{chr(10).join('- ' + item for item in uncovered) or '(none)'}

## The student's actual notes in this period
{notes_view}

## Task
Return JSON:
{{
  "headline": "one sentence a supervisor can read in five seconds",
  "did": ["what actually happened, grounded in the numbers"],
  "learned": ["what they can now explain that they could not before, based on the notes"],
  "not_done": ["what was planned or implied but did not happen, including unread carry-over papers"],
  "drift": "where the reading is scattering away from the long-term goals; 'none' if it is on track",
  "next_focus": "the single most valuable thing to do next period",
  "artifacts": "one line describing what code/reproduction artifact exists, or 'none'"
}}"""
    return build_prompt(body, lang)


def render(facts: SummaryFacts, payload: Dict[str, Any], lang: str = "zh") -> str:
    """渲染成 Markdown。数字部分完全由脚本生成，AI 只填叙述。"""
    counts = facts.stats
    title = "月度总结" if facts.period == "month" else "周总结"
    lines = [f"# {title}：{facts.label}", ""]
    lines.append(f"- 区间：{facts.start.isoformat()} ~ {facts.end.isoformat()}")
    lines.append(f"- 周号：{'、'.join(week_label(w) for w in facts.weeks) or '（无）'}")
    lines.append(f"- 生成时间：{dt.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    lines.append("## 数字（脚本算的，不是 AI 说的）")
    lines.append("")
    lines.append(
        f"- 推荐 {counts['pushed']} 篇：已读 {counts['read']}、跳过 {counts['skipped']}、未标记 {counts['unmarked']}"
    )
    lines.append(f"- 更早期遗留下来、至今没读的：{len(facts.carried)} 篇")
    lines.append(f"- 笔记：{len(facts.notes)} 份，共 {facts.note_chars} 字")
    lines.append(f"- 主题分布：{facts.themes}")
    lines.append("")
    if facts.pushed:
        lines.append("| 状态 | 角色 | 周 | 标题 |")
        lines.append("| --- | --- | --- | --- |")
        for row in facts.pushed:
            lines.append(
                f"| {row.get('status') or '未标记'} | {row.get('role') or '-'} | "
                f"{row.get('week') or '-'} | {row.get('title') or ''} |"
            )
        lines.append("")
    if facts.carried:
        lines.append("### 一直没读的")
        lines.append("")
        lines.extend(f"- {row.get('title')}" for row in facts.carried)
        lines.append("")

    headline = str(payload.get("headline") or "").strip()
    if headline:
        lines.append("## 一句话")
        lines.append("")
        lines.append(headline)
        lines.append("")

    for key, title in (
        ("did", "实际做了什么"),
        ("learned", "现在能讲清什么"),
        ("not_done", "没做到的"),
    ):
        items = [str(x).strip() for x in (payload.get(key) or []) if str(x).strip()]
        lines.append(f"## {title}")
        lines.append("")
        lines.extend(f"- {item}" for item in items)
        if not items:
            lines.append(pick(lang, "- （没有可说的，或者 AI 不可用）", "- (nothing reported, or no AI available)"))
        lines.append("")

    drift = str(payload.get("drift") or "").strip()
    lines.append("## 有没有跑偏")
    lines.append("")
    lines.append(drift or pick(lang, "（本次没有判断）", "(not assessed)"))
    lines.append("")

    lines.append("## 下一个周期的唯一重点")
    lines.append("")
    lines.append(str(payload.get("next_focus") or "").strip() or pick(lang, "（未给出）", "(not given)"))
    lines.append("")

    lines.append("## 代码 / 复现产物")
    lines.append("")
    lines.append(str(payload.get("artifacts") or "").strip() or pick(lang, "（无）", "(none)"))
    lines.append("")
    lines.append(ARTIFACT_PLACEHOLDER)
    lines.append("")
    lines.append("> 这一节由你自己填。脚本不碰 git，也不会替你写代码。")
    return "\n".join(lines).rstrip() + "\n"


@dataclass
class SummaryOutcome:
    label: str
    period: str
    path: Optional[Path]
    text: str
    mode: str
    dry_run: bool = False


def run_summary(
    cfg: VaultConfig,
    client: Optional[LLMClient] = None,
    period: str = "month",
    anchor: Optional[dt.date] = None,
    lang: str = "zh",
    dry_run: bool = False,
) -> SummaryOutcome:
    """生成总结。**总结是唯一一个没有 AI 也能出结果的命令**：叙述部分留空，数字照写。"""
    facts = collect(cfg, period, anchor)
    payload: Dict[str, Any] = {}
    mode = "none"
    if client is not None and client.available:
        payload = client.json(summary_prompt(facts, lang), temperature=0.3) or {}
        mode = "llm" if payload else "none"
    text = render(facts, payload, lang)
    if dry_run:
        return SummaryOutcome(label=facts.label, period=period, path=None, text=text, mode=mode, dry_run=True)
    if period == "month":
        path = cfg.month_report_path(facts.label)
    else:
        week = facts.weeks[0] if facts.weeks else 1
        path = cfg.week_report_dir / f"{facts.start.isoformat()}_{week_label(week)}总结.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return SummaryOutcome(label=facts.label, period=period, path=path, text=text, mode=mode)
