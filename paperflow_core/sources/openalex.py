"""OpenAlex 适配器：无需 key、覆盖广，并负责为 arXiv/PubMed 补齐引用数。

踩过的坑（已验证）
------------------
* 必须用 ``filter=title_and_abstract.search:...``。裸 ``search=`` 会全文匹配，
  把 AlphaFold / BRASTS 这种完全无关的工作也拖进来。
* 摘要存的是"词 → 位置列表"的倒排索引，需要重建；部分记录是整个文档的词索引。
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional

from ..config import contact_email
from ..http_client import api_get
from ..models import clean_doi, enrich_paper, normalise_title

WORKS_URL = "https://api.openalex.org/works"
SOURCE_NAME = "openalex"
SELECT = (
    "id,doi,title,publication_year,publication_date,authorships,cited_by_count,"
    "primary_location,best_oa_location,abstract_inverted_index,ids"
)
BACKFILL_BATCH = 25


def openalex_abstract(inverted: Optional[Dict[str, List[int]]]) -> str:
    """把倒排索引重建成可读摘要。"""
    if not inverted:
        return ""
    positioned = [(position, word) for word, positions in inverted.items() for position in (positions or [])]
    positioned.sort()
    return " ".join(word for _, word in positioned)


def search(query: str, limit: int = 30, min_year: Optional[int] = None) -> List[Dict[str, Any]]:
    filters = [f"title_and_abstract.search:{query}"]
    if min_year:
        filters.append(f"from_publication_date:{min_year}-01-01")
    params: Dict[str, Any] = {
        "filter": ",".join(filters),
        "per-page": min(max(limit, 1), 200),
        "select": SELECT,
        "sort": "relevance_score:desc",
    }
    email = contact_email()
    if email:
        params["mailto"] = email
    data = api_get(WORKS_URL, params=params)

    papers: List[Dict[str, Any]] = []
    for work in data.get("results", []):
        ids = work.get("ids") or {}
        doi = clean_doi(work.get("doi") or ids.get("doi"))
        openalex_id = (ids.get("openalex") or work.get("id") or "").rstrip("/").split("/")[-1]
        pmid = (ids.get("pmid") or "").rstrip("/").split("/")[-1]
        primary = work.get("primary_location") or {}
        best = work.get("best_oa_location") or {}
        title = work.get("title") or work.get("display_name") or ""

        external: Dict[str, str] = {}
        if doi:
            external["DOI"] = doi
        if pmid:
            external["PubMed"] = pmid
        papers.append(
            enrich_paper(
                {
                    "paperId": f"openalex:{openalex_id}" if openalex_id else f"title:{normalise_title(title)}",
                    "title": title,
                    "abstract": openalex_abstract(work.get("abstract_inverted_index")),
                    "year": work.get("publication_year"),
                    "authors": [
                        {"name": (a.get("author") or {}).get("display_name", "")}
                        for a in (work.get("authorships") or [])
                    ],
                    "venue": (primary.get("source") or {}).get("display_name") or "",
                    "citationCount": work.get("cited_by_count") or 0,
                    "influentialCitationCount": 0,
                    "openAccessPdf": {"url": best.get("pdf_url") or primary.get("pdf_url") or ""},
                    "externalIds": external,
                    "url": primary.get("landing_page_url") or (f"https://doi.org/{doi}" if doi else ""),
                    "publicationDate": work.get("publication_date") or "",
                },
                source=SOURCE_NAME,
            )
        )
    return papers


def backfill_citations(papers: List[Dict[str, Any]]) -> None:
    """把 OpenAlex 的引用数与缺失摘要补到 arXiv/PubMed 记录上。

    不做这一步，arXiv/PubMed 会因为缺少引用数而在排序里被系统性压低。
    直接原地修改 ``papers`` 里的字典。
    """
    targets = [
        paper
        for paper in papers
        if paper.get("source") in {"arxiv", "pubmed"} and clean_doi(paper.get("doi")) and not paper.get("citationCount")
    ]
    if not targets:
        return

    by_doi: Dict[str, Dict[str, Any]] = {}
    for paper in targets:
        by_doi.setdefault(clean_doi(paper["doi"]).lower(), paper)

    items = list(by_doi.items())
    for start in range(0, len(items), BACKFILL_BATCH):
        chunk = items[start : start + BACKFILL_BATCH]
        params: Dict[str, Any] = {
            "filter": "doi:" + "|".join(doi for doi, _ in chunk),
            "per-page": len(chunk),
            "select": "doi,cited_by_count,abstract_inverted_index",
        }
        email = contact_email()
        if email:
            params["mailto"] = email
        try:
            data = api_get(WORKS_URL, params=params)
        except Exception as exc:  # 补齐失败不该中断整条流程
            print(f"Warning: OpenAlex citation backfill failed: {exc}", file=sys.stderr)
            return
        for work in data.get("results", []):
            target = by_doi.get(clean_doi(work.get("doi")).lower())
            if not target:
                continue
            target["citationCount"] = work.get("cited_by_count") or 0
            if not target.get("abstract"):
                target["abstract"] = openalex_abstract(work.get("abstract_inverted_index"))
