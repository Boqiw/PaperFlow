"""论文数据模型与归一化。

所有数据源的返回值都必须经过 :func:`enrich_paper` 这一道"漏斗"，
这样下游（排序、Zotero）永远只面对同一种字典结构。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

FIELDS = (
    "paperId,title,abstract,year,authors,venue,citationCount,influentialCitationCount,"
    "openAccessPdf,externalIds,url,publicationDate"
)
ABSTRACT_LIMIT = 2400


def normalise_title(title: str) -> str:
    """标题归一化，用于跨源去重。"""
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def clean_doi(value: Optional[str]) -> str:
    """去掉 DOI 的 URL 前缀，统一成裸 DOI（Crossref 风格）。"""
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", (value or "").strip(), flags=re.I)


def paper_key(paper: Dict[str, Any]) -> str:
    """去重键：优先 DOI，其次归一化标题。"""
    ext = paper.get("externalIds") or {}
    doi = (ext.get("DOI") or "").lower().strip()
    return f"doi:{doi}" if doi else f"title:{normalise_title(paper.get('title', ''))}"


def enrich_paper(paper: Dict[str, Any], source: str = "") -> Dict[str, Any]:
    """唯一归一化入口：补齐字段、裁剪摘要、算出派生的便利字段。"""
    paper = dict(paper)
    paper["authors"] = paper.get("authors") or []
    # 个别 OpenAlex 记录携带的是整篇文档的词索引而非摘要，需要裁掉，
    # 否则关键词重合度会被系统性抬高。
    abstract = paper.get("abstract") or ""
    if len(abstract) > ABSTRACT_LIMIT:
        abstract = abstract[:ABSTRACT_LIMIT].rstrip() + " ..."
    paper["abstract"] = abstract
    paper["externalIds"] = paper.get("externalIds") or {}
    paper["key"] = paper_key(paper)
    paper["doi"] = paper["externalIds"].get("DOI", "")
    paper["open_pdf"] = (paper.get("openAccessPdf") or {}).get("url", "")
    paper["author_text"] = ", ".join(a.get("name", "") for a in paper["authors"][:6])
    if source:
        paper["source"] = source
    return paper


def dedupe(papers: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 :func:`paper_key` 去重，并丢弃没有标题的噪声记录。"""
    seen = set()
    result = []
    for paper in papers:
        key = paper.get("key") or paper_key(paper)
        if key in seen or not paper.get("title"):
            continue
        seen.add(key)
        result.append(paper)
    return result
