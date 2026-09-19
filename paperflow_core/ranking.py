"""候选池的质量闸门：引用门槛 + 年份窗口 + 确定性打分 + 跨源轮转取样。

四件事各管一段：

1. :func:`split_by_citation_floor` —— **硬门槛**。按论文年龄分档要求引用数，
   把「刚挂上 arXiv、还没人引」和「低引用的边缘文献」挡在候选池外面。
   引用不够的论文，分数再高也不会出现在推荐里。
2. :func:`select_by_year_window` —— **年份窗口**。优先只装近 :data:`YEAR_WINDOW` 年的论文；
   这一档凑不满候选池时，才用近 :data:`YEAR_WINDOW_MAX` 年以内、更老的那些补足。
3. :func:`heuristic_score` —— 决定送进 prompt 的顺序与取舍（几百篇塞不进去）。
   引用量在总分里占明显权重：越老、被引越多的论文越靠前。
4. :func:`diversify` —— 跨源轮转取样，避免单一数据源淹没结果。

这里**不负责**「没有 AI 时也能给出结果」：没有 AI 就不产出清单，
见 :mod:`paperflow_core.weekly`。
"""

from __future__ import annotations

import datetime as dt
import math
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

CORE_TERMS = (
    "brain state",
    "neural dynamics",
    "population dynamics",
    "state transition",
    "connectivity",
    "eeg",
    "fmri",
    "computational model",
)

RECENCY_YEARS = 4

#: 引用门槛基准所对应的论文年龄：发表满这么多年的论文，至少要 CITATION_BASE 次引用。
CITATION_BASE_YEARS = 5
#: 引用门槛基准（可用 ``--min-citations`` 覆盖）。
CITATION_BASE = 50
#: 年龄分档 → 相对基准的倍数。新论文还没来得及积累引用，所以要求更低；
#: 但倍数不设 0 档：只要门槛开着（base > 0），**一次引用都没有的论文一律进不了候选池**。
CITATION_TIERS: Tuple[Tuple[int, float], ...] = ((CITATION_BASE_YEARS, 1.0), (2, 0.4), (0, 0.1))
#: 引用量在启发式总分（0-10）里最多加多少分。给得比时效性高得多：
#: 你明确要求「推荐的文章一定要引用量高」，所以排序也向高引文献倾斜。
CITATION_WEIGHT = 3.0

#: 「近 N 年」年份窗口：候选池优先只装近这么多年的论文（0 表示不限年份）。
YEAR_WINDOW = 5
#: 近 :data:`YEAR_WINDOW` 年凑不满候选池时，最多放宽到这个年数。
YEAR_WINDOW_MAX = 10


def citation_signal(paper: Dict[str, Any]) -> int:
    """取论文的引用数。

    只有 Semantic Scholar 提供 ``influentialCitationCount``，OpenAlex/PubMed 只给
    ``citationCount``（而且 OpenAlex 会把 influential 写死成 0）。所以取两者的
    **最大值**而不是「谁非空用谁」：任何一个字段有值，都不会被另一个字段的 0 拖低。
    """
    return max(int(paper.get("citationCount") or 0), int(paper.get("influentialCitationCount") or 0))


def citation_floor(year: Optional[int], base: int = CITATION_BASE) -> int:
    """按发表年份算这篇论文的引用门槛。``base <= 0`` 表示不设门槛（恒为 0）。

    年份读不到（None / 0）时按**最严**的一档处理：宁可不推，也不推一篇连年份都查不到的。
    """
    if base <= 0:
        return 0
    age = dt.date.today().year - int(year or 0)
    factor = CITATION_TIERS[-1][1]
    for min_age, value in CITATION_TIERS:
        if age >= min_age:
            factor = value
            break
    return max(1, int(round(base * factor)))


def passes_citation_floor(paper: Dict[str, Any], base: int = CITATION_BASE) -> bool:
    """这篇论文的引用量够不够门槛。"""
    floor = citation_floor(paper.get("year"), base)
    return floor <= 0 or citation_signal(paper) >= floor


def split_by_citation_floor(
    papers: List[Dict[str, Any]], base: int = CITATION_BASE
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """按门槛把候选分成（够格的、不够格的）。调用方可以把后者报给你看。"""
    kept: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    for paper in papers:
        (kept if passes_citation_floor(paper, base) else dropped).append(paper)
    return kept, dropped


def year_cutoff(years: int, today: Optional[dt.date] = None) -> int:
    """「近 N 年」的最早年份（含）。``years <= 0`` 返回 0，表示不限年份。"""
    if years <= 0:
        return 0
    return (today or dt.date.today()).year - int(years)


def in_year_window(paper: Dict[str, Any], years: int, today: Optional[dt.date] = None) -> bool:
    """这篇论文在「近 N 年」窗口内吗。

    年份读不到（None / 0）时算**不在**窗口内：宁可不推，也不把一篇查不到年份的文献
    当成新文章。它仍然可能在放宽那一档里被收回来——见 :func:`select_by_year_window`。
    """
    if years <= 0:
        return True
    year = int(paper.get("year") or 0)
    return bool(year) and year >= year_cutoff(years, today)


def select_by_year_window(
    papers: List[Dict[str, Any]],
    max_candidates: int,
    years: int = YEAR_WINDOW,
    max_years: int = YEAR_WINDOW_MAX,
    today: Optional[dt.date] = None,
) -> List[Dict[str, Any]]:
    """按年份窗口截取候选：窗口内的先拿，不够再用窗口外（不超过 ``max_years``）补足。

    ``papers`` 必须是**已经排好序**的（调用方先按 ``heuristic_score`` 降序排），
    这样即使在放宽的那一档里，也是分高的先入选。窗口只决定「**谁能进候选池**」，
    候选中谁排前面仍然由 ``heuristic_score`` 决定——所以进来的永远是「窗口内分数最高的
    那些 + 名额没填满时分最高的老论文」。

    ``years <= 0`` 表示不限年份，等价于直接取前 ``max_candidates`` 篇。
    ``max_years`` 小于 ``years`` 时按 ``years`` 算，不会比窗口本身还紧。
    """
    if years <= 0:
        picked = list(papers[:max_candidates])
        picked.sort(key=lambda item: item.get("heuristic_score", 0.0), reverse=True)
        return picked
    ceiling = year_cutoff(max(int(max_years), int(years)), today)
    recent: List[Dict[str, Any]] = []
    older: List[Dict[str, Any]] = []
    for paper in papers:
        if in_year_window(paper, years, today):
            recent.append(paper)
            continue
        year = int(paper.get("year") or 0)
        if not year or year >= ceiling:
            older.append(paper)
    picked = recent[:max_candidates]
    if len(picked) < max_candidates:
        picked.extend(older[: max_candidates - len(picked)])
    picked.sort(key=lambda item: item.get("heuristic_score", 0.0), reverse=True)
    return picked


def year_window_stats(
    papers: Sequence[Dict[str, Any]],
    years: int,
    max_years: int = YEAR_WINDOW_MAX,
    today: Optional[dt.date] = None,
) -> Dict[str, int]:
    """这份候选的年份构成，给报告用。

    ``in_window`` 是近 ``years`` 年的；``widened`` 是更老、但仍在 ``max_years`` 以内的；
    ``unknown`` 是查不到年份的。``oldest`` / ``newest`` 只统计有年份的那些。
    """
    stats = {"total": 0, "in_window": 0, "widened": 0, "unknown": 0, "oldest": 0, "newest": 0}
    if years <= 0:
        ceiling = 0
    else:
        ceiling = year_cutoff(max(int(max_years), int(years)), today)
    for paper in papers:
        stats["total"] += 1
        year = int(paper.get("year") or 0)
        if not year:
            stats["unknown"] += 1
            continue
        stats["oldest"] = year if not stats["oldest"] else min(stats["oldest"], year)
        stats["newest"] = max(stats["newest"], year)
        if years <= 0 or year >= year_cutoff(years, today):
            stats["in_window"] += 1
        elif year >= ceiling:
            stats["widened"] += 1
    return stats


def heuristic_score(paper: Dict[str, Any], context: str, topic: str) -> float:
    """基于主题词重合、核心术语、时效性与引用量的确定性打分（0-10）。

    ``context`` 是「你的长期目标 + 最近的笔记」拼起来的一段纯文本。
    这项分数只决定候选进 prompt 的顺序，**不是**质量判断——质量由引用门槛负责。
    每一项都封了顶，否则一段很长的 goals 会把所有候选都打到饱和，排序就失去意义。
    """
    text = (paper.get("title", "") + " " + paper.get("abstract", "")).lower()
    terms = set(re.findall(r"[a-z][a-z-]{3,}", (topic + " " + (context or "")).lower()))
    overlap = min(sum(1 for term in terms if term in text), 10)
    core_hits = min(sum(1 for term in CORE_TERMS if term in text), 4)
    year = paper.get("year") or 0
    recency = 0.5 if year >= dt.date.today().year - RECENCY_YEARS else 0.0
    # 对数刻度：10 次引用约 +1.0，1000 次左右顶到 CITATION_WEIGHT，再多也不无限加分。
    citations = min(math.log10(citation_signal(paper) + 1) / 3.0, 1.0) * CITATION_WEIGHT
    return round(min(10.0, 1.0 + overlap * 0.15 + core_hits * 0.5 + recency + citations), 2)


def score_all(papers: List[Dict[str, Any]], context: str, topic: str) -> None:
    """原地写入 ``heuristic_score``。"""
    for paper in papers:
        paper["heuristic_score"] = round(heuristic_score(paper, context, topic), 2)


def diversify(papers: List[Dict[str, Any]], max_candidates: int) -> List[Dict[str, Any]]:
    """排序后在各数据源之间轮转取样，避免单一源淹没结果。"""
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for paper in papers:
        source = paper.get("source") or "other"
        if source not in buckets:
            buckets[source] = []
            order.append(source)
        buckets[source].append(paper)
    if len(order) <= 1:
        return papers[:max_candidates]

    picked: List[Dict[str, Any]] = []
    index = 0
    while len(picked) < max_candidates:
        added = False
        for source in order:
            if index < len(buckets[source]):
                picked.append(buckets[source][index])
                added = True
                if len(picked) >= max_candidates:
                    break
        if not added:
            break
        index += 1
    return picked
