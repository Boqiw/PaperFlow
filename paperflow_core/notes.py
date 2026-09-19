"""笔记读取与周报的机器可读标记。

两头都只碰文件系统，所以这套东西和 llm-for-zotero 是**共用一个文件夹**，
不是互相调用：

    llm-for-zotero 把笔记写成 .md 落到当周文件夹  →  week 读它

周报末尾会附一行 ``<!-- PF:WEEK {...} -->``（Obsidian 里不显示）。
``week`` 做全局分析时**只读这一行，不读周报正文**——正文越写越长，
读 52 份正文必然漏掉早期的；读 52 行 JSON 永远不会漏。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .vault import VaultConfig

LIMIT_PER_NOTE = 4000
TOTAL_LIMIT = 24000

#: 匹配 ``<!-- PF:WEEK {...} -->``。
#: ``(?!-->)`` 这一段是关键：正文里绝不可能出现 ``-->``，所以一个写坏的块
#: （比如手改周报时把 JSON 弄坏了）只会在它自己身上匹配失败，不会贪婪地
#: 把后面那个完好的块一起吞掉——历史一旦丢一段，全局回顾就失真了。
PF_PATTERN = re.compile(r"<!--\s*PF:WEEK\s*(\{(?:(?!-->).)*?\})\s*-->", re.DOTALL)


def title_key(title: str) -> str:
    """标题归一化：小写、非字母数字压成单空格。"""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (title or "").lower())).strip()


def hint_key(title: str) -> str:
    """只用标题前 6 个词做匹配——笔记里经常只抄半截标题。"""
    return " ".join(title_key(title).split()[:6])


@dataclass
class Note:
    week: int
    path: Path
    text: str

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class Corpus:
    """一次 ``week`` 读进来的全部笔记。"""

    notes: List[Note] = field(default_factory=list)
    trimmed: bool = False
    _index: List[str] = field(default_factory=list, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._index = [title_key(f"{note.name} {note.text}") for note in self.notes]

    @property
    def weeks(self) -> List[int]:
        return sorted({note.week for note in self.notes})

    @property
    def empty(self) -> bool:
        return not self.notes

    def mentions(self, title: str) -> bool:
        """笔记里是否出现过这篇论文（用标题前 6 个词判断，是启发式的）。"""
        key = hint_key(title)
        if len(key) < 12:
            return False
        return any(key in text for text in self._index)

    def prompt_block(self, lang: str = "zh") -> str:
        """渲染成 prompt 里的笔记正文段。"""
        if self.empty:
            return "(这一周没有任何笔记)" if lang == "zh" else "(no notes this week)"
        chunks: List[str] = []
        for note in self.notes:
            body = note.text.strip()
            if len(body) > LIMIT_PER_NOTE:
                body = body[:LIMIT_PER_NOTE] + f"\n……（截断，全文 {len(note.text)} 字）"
            chunks.append(f"### [第{note.week}周] {note.name}\n\n{body}")
        return "\n\n".join(chunks)


def read_notes(
    cfg: VaultConfig,
    weeks: Sequence[int],
    per_note: int = LIMIT_PER_NOTE,
    total: int = TOTAL_LIMIT,
) -> Corpus:
    """读若干周文件夹里的笔记，总量超预算就截断并打标记。

    ``per_note`` / ``total`` 都以字符计。截断是必要的：一周写十几条长笔记
    很正常，无上限会让 prompt 直接爆掉。
    """
    notes: List[Note] = []
    trimmed = False
    used = 0
    for week in weeks:
        for path in cfg.notes_in(week):
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not raw.strip():
                continue
            text = raw
            if per_note and len(text) > per_note:
                text = text[:per_note]
                trimmed = True
            if total:
                remaining = total - used
                if remaining <= 0:
                    return Corpus(notes=notes, trimmed=True)
                if len(text) > remaining:
                    text = text[:remaining]
                    trimmed = True
            used += len(text)
            notes.append(Note(week=week, path=path, text=text))
    return Corpus(notes=notes, trimmed=trimmed)


# ------------------------------------------------------- 周报的机器可读块


def render_pf_block(payload: Dict[str, Any]) -> str:
    return "<!-- PF:WEEK " + json.dumps(payload, ensure_ascii=False) + " -->"


def parse_pf_blocks(text: str) -> List[Dict[str, Any]]:
    """从一份周报里取出所有 PF:WEEK 块（坏掉的静默跳过）。"""
    out: List[Dict[str, Any]] = []
    for raw in PF_PATTERN.findall(text or ""):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            out.append(payload)
    return out


def read_pf_blocks(report_dir: Path) -> List[Dict[str, Any]]:
    """按文件名顺序读遍历史周报，返回按周号排好的全局摘要。"""
    if not report_dir.is_dir():
        return []
    blocks: List[Dict[str, Any]] = []
    for path in sorted(report_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        blocks.extend(parse_pf_blocks(text))
    blocks.sort(key=lambda item: int(item.get("week") or 0))
    return blocks


def global_history_block(
    blocks: Sequence[Dict[str, Any]],
    stats_by_week: Optional[Dict[int, Dict[str, int]]] = None,
    lang: str = "zh",
) -> str:
    """把历史周报的块渲染成「每周一行」的全局视图。

    执行情况（读了没读）取自**账本**而不是块本身：块只在写完那一周写一次，
    账本每周都会重新对账，所以账本才是最新的。
    """
    if not blocks:
        return "(还没有历史周报)" if lang == "zh" else "(no weekly reports yet)"
    lookup = stats_by_week or {}
    lines: List[str] = []
    for item in blocks:
        week = int(item.get("week") or 0)
        window = item.get("window") or ""
        themes = item.get("themes") or "（未归类）"
        titles = item.get("titles") or []
        counts = lookup.get(week) or {}
        if counts:
            head = (
                f"第{week}周 {window}：推荐 {counts.get('pushed', len(titles))}，"
                f"已读 {counts.get('read', 0)}，跳过 {counts.get('skipped', 0)}，"
                f"未标记 {counts.get('unmarked', 0)}｜主题 {themes}"
            )
        else:
            head = f"第{week}周 {window}：推荐 {len(titles)}｜主题 {themes}"
        lines.append(head)
        for title in titles:
            lines.append(f"    推 → {title}")
        uncovered = item.get("uncovered")
        if uncovered:
            lines.append(f"    未被覆盖的长期目标 → {uncovered}")
    return "\n".join(lines)
