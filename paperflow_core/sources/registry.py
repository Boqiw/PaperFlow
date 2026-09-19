"""数据源注册表：统一的别名解析、限速与调度。

新增数据源只需三步：
1. 写一个 ``search(query, limit, min_year)`` 适配器模块；
2. 在 :data:`SOURCE_BACKENDS` 里注册；
3. 在 :data:`SOURCE_LABELS` / :data:`SOURCE_ALIASES` 里加展示名和别名。
"""

from __future__ import annotations

import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from . import arxiv, openalex, pubmed, semantic_scholar

SOURCE_LABELS = {
    "semanticscholar": "Semantic Scholar",
    "openalex": "OpenAlex",
    "arxiv": "arXiv",
    "pubmed": "PubMed",
}

SOURCE_ALIASES = {
    "s2": "semanticscholar",
    "semanticscholar": "semanticscholar",
    "semantic-scholar": "semanticscholar",
    "openalex": "openalex",
    "open-alex": "openalex",
    "arxiv": "arxiv",
    "pubmed": "pubmed",
    "pub-med": "pubmed",
    "ncbi": "pubmed",
}

SOURCE_BACKENDS = {
    "semanticscholar": semantic_scholar.search,
    "openalex": openalex.search,
    "arxiv": arxiv.search,
    "pubmed": pubmed.search,
}

#: 默认检索用的查询变体（主题本身 + 综述 + 方法向）。
QUERY_SUFFIXES = ("", " review", " computational modeling")


def parse_sources(raw: Optional[str]) -> List[str]:
    """把 ``--sources`` 解析成后端名列表；返回空列表表示"交给 auto 决定"。"""
    if not raw or raw.strip().lower() == "auto":
        return []
    names: List[str] = []
    for part in re.split(r"[,\s;]+", raw.strip()):
        if not part:
            continue
        name = SOURCE_ALIASES.get(part.lower())
        if not name:
            raise SystemExit(f"Unknown source {part!r}. Choose from: s2, openalex, arxiv, pubmed, or auto.")
        if name not in names:
            names.append(name)
    return names


def plan_sources(explicit: List[str]) -> List[str]:
    """auto 模式：有 S2 key 时首选 Semantic Scholar，否则 OpenAlex；再补 arXiv 与 PubMed。"""
    if explicit:
        return list(explicit)
    plan = ["semanticscholar"] if os.getenv("S2_API_KEY") else ["openalex"]
    for name in ("arxiv", "pubmed"):
        if name not in plan:
            plan.append(name)
    return plan


def backend_pause(name: str) -> float:
    """按各 API 公开的配额节流（没配 key 时更保守）。"""
    if name == "semanticscholar":
        return 0.2 if os.getenv("S2_API_KEY") else 1.1
    if name == "arxiv":
        return 3.0
    if name == "pubmed":
        return 0.12 if os.getenv("NCBI_API_KEY") else 0.4
    return 0.2


def build_queries(topic: str) -> List[str]:
    """把 ``topic`` 展开成若干条检索式。

    用 ``|`` 分隔多个方向时（``brain state|closed-loop``），逐条原样检索——
    一周要同时跟几条线，这是唯一需要的扩展。
    单一主题时保留默认后缀（主题本身 + review + computational modeling）：
    冷启动只给一个宽泛方向，也能凑够候选。
    """
    parts = [part.strip() for part in (topic or "").split("|") if part.strip()]
    if len(parts) > 1:
        return parts
    base = parts[0] if parts else ""
    return [base + suffix for suffix in QUERY_SUFFIXES]


def run_backend(
    name: str,
    queries: List[str],
    min_year: Optional[int],
    limit: int,
) -> Tuple[List[Dict[str, Any]], bool]:
    """用一个后端的多个查询变体检索。返回 ``(papers, 是否至少成功一次)``。"""
    search = SOURCE_BACKENDS[name]
    papers: List[Dict[str, Any]] = []
    succeeded = False
    for index, query in enumerate(queries):
        if index:
            time.sleep(backend_pause(name))
        try:
            papers.extend(search(query, limit=limit, min_year=min_year))
            succeeded = True
        except Exception as exc:
            print(f"Warning: {SOURCE_LABELS[name]} search failed for {query!r}: {exc}", file=sys.stderr)
    return papers, succeeded
