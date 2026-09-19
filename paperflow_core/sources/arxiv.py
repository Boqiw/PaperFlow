"""arXiv 适配器：预印本最新，但没有引用数（由 OpenAlex 补齐）。

踩过的坑：必须用 ``requests`` 取；``urllib`` 会被返回 HTTP 406。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

from ..http_client import api_get_bytes
from ..models import clean_doi, enrich_paper, normalise_title
from ._xml import xml_text

QUERY_URL = "https://export.arxiv.org/api/query"
SOURCE_NAME = "arxiv"
NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def arxiv_query(query: str) -> str:
    """arXiv 本身没有相关度排序，所以把所有词 AND 起来跨字段匹配。"""
    terms = [t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9.+-]*", query) if len(t) > 1]
    return " AND ".join(f"all:{t}" for t in terms) if terms else f"all:{query}"


def search(query: str, limit: int = 30, min_year: Optional[int] = None) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {
        "search_query": arxiv_query(query),
        "start": 0,
        "max_results": min(max(limit, 1), 100),
        "sortBy": "relevance",
        "sortOrder": "descending",
    }
    root = ET.fromstring(api_get_bytes(QUERY_URL, params=params))

    papers: List[Dict[str, Any]] = []
    for entry in root.findall("a:entry", NS):
        title = xml_text(entry.find("a:title", NS))
        published = xml_text(entry.find("a:published", NS))
        year = int(published[:4]) if published[:4].isdigit() else None
        if min_year and year and year < min_year:
            continue
        raw_id = xml_text(entry.find("a:id", NS))
        short = re.sub(r"v\d+$", "", raw_id.rstrip("/").split("/abs/")[-1])
        doi = clean_doi(xml_text(entry.find("arxiv:doi", NS)))

        external: Dict[str, str] = {}
        if short:
            external["ArXiv"] = short
        if doi:
            external["DOI"] = doi
        papers.append(
            enrich_paper(
                {
                    "paperId": f"arxiv:{short}" if short else f"title:{normalise_title(title)}",
                    "title": title,
                    "abstract": xml_text(entry.find("a:summary", NS)),
                    "year": year,
                    "authors": [{"name": xml_text(a.find("a:name", NS))} for a in entry.findall("a:author", NS)],
                    "venue": xml_text(entry.find("arxiv:journal_ref", NS)) or "arXiv",
                    "citationCount": 0,
                    "influentialCitationCount": 0,
                    "openAccessPdf": {"url": f"https://arxiv.org/pdf/{short}" if short else ""},
                    "externalIds": external,
                    "url": raw_id.replace("http://", "https://", 1),
                    "publicationDate": published[:10],
                },
                source=SOURCE_NAME,
            )
        )
    return papers
