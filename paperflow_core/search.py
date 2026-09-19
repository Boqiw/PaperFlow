"""检索编排：把多个数据源的结果汇聚成一份候选列表。

``build_candidates`` 是唯一对外入口，负责：调度各后端 → 合并种子论文推荐
→ 去重 → 补齐引用数 → **过引用门槛** → 打分 → **过年份窗口** → 跨源轮转取样。

两道闸门都放在打分**之前/之后**各有理由：引用门槛在打分前，候选池从一开始就只装
高引用文献，免得低引用的噪声把 40 个名额占满；年份窗口在打分后，因为要先有分数
才知道「窗口内的哪几篇最值得推」。
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional

from .models import dedupe
from .ranking import (
    CITATION_BASE,
    CITATION_BASE_YEARS,
    YEAR_WINDOW,
    YEAR_WINDOW_MAX,
    diversify,
    score_all,
    select_by_year_window,
    split_by_citation_floor,
)
from .sources import openalex, semantic_scholar
from .sources.registry import build_queries, parse_sources, plan_sources, run_backend


def build_candidates(
    topic: str,
    seeds: List[str],
    min_year: Optional[int],
    max_candidates: int,
    context: str = "",
    sources: Optional[str] = None,
    min_citations: Optional[int] = None,
    years: int = YEAR_WINDOW,
    max_years: int = YEAR_WINDOW_MAX,
) -> List[Dict[str, Any]]:
    """检索、过引用门槛、排序、过年份窗口，返回候选论文。

    ``sources`` 为 None/"auto" 时按 :func:`plan_sources` 决定；显式指定则只用指定后端。
    ``context`` 是长期目标 + 最近笔记拼成的纯文本，只用来给打分的词重合项加分。
    ``min_citations`` 是引用门槛基准：None 用 :data:`ranking.CITATION_BASE`，
    0 表示不设门槛（会用标准错误输出说明筛掉了多少篇）。
    ``years`` 是年份窗口（默认近 5 年）：先只装窗口内的，凑不满 ``max_candidates``
    才用 ``max_years`` 年以内的补足。0 表示不限年份。
    """
    base = CITATION_BASE if min_citations is None else int(min_citations)
    parsed = parse_sources(sources)
    auto = not parsed
    queue = plan_sources(parsed)
    queries = build_queries(topic)
    papers: List[Dict[str, Any]] = []

    if "semanticscholar" in queue and not os.getenv("S2_API_KEY"):
        print("Note: S2_API_KEY is not set; Semantic Scholar throttles anonymous traffic hard.", file=sys.stderr)

    index = 0
    while index < len(queue):
        name = queue[index]
        index += 1
        found, succeeded = run_backend(name, queries, min_year, max_candidates)
        papers.extend(found)
        # 仅 auto 模式：首选后端整体失败（典型是匿名 429）时回退到 OpenAlex。
        if not succeeded and auto and name == "semanticscholar" and "openalex" not in queue:
            print("Note: Semantic Scholar unavailable, falling back to OpenAlex.", file=sys.stderr)
            queue.append("openalex")

    seed_ids: List[str] = []
    for seed in seeds:
        paper = semantic_scholar.paper_lookup(seed)
        if paper:
            papers.append(paper)
            seed_ids.append(paper["paperId"])
    if seed_ids:
        try:
            papers.extend(semantic_scholar.recommendations(seed_ids, limit=max_candidates))
        except Exception as exc:
            print(f"Warning: recommendation lookup failed: {exc}", file=sys.stderr)

    papers = dedupe(papers)
    openalex.backfill_citations(papers)
    total = len(papers)
    papers, below_floor = split_by_citation_floor(papers, base)
    if below_floor and base > 0:
        print(
            f"Note: {len(below_floor)} of {total} candidates had too few citations "
            f"(floor: {base} for papers {CITATION_BASE_YEARS}+ years old, scaled down for newer ones) "
            "and were dropped. Use --min-citations 0 to disable this gate.",
            file=sys.stderr,
        )
    score_all(papers, context, topic)
    papers.sort(key=lambda p: p["heuristic_score"], reverse=True)
    papers = select_by_year_window(papers, max_candidates, years, max_years)
    return diversify(papers, max_candidates)
