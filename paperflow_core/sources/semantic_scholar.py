"""Semantic Scholar 适配器：引用数据最全，但匿名流量限流很严（HTTP 429）。"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

from ..http_client import api_get, api_post
from ..models import FIELDS, enrich_paper

SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
GRAPH_URL = "https://api.semanticscholar.org/graph/v1/paper/"
RECOMMEND_URL = "https://api.semanticscholar.org/recommendations/v1/papers"
SOURCE_NAME = "semanticscholar"


def search(query: str, limit: int = 30, min_year: Optional[int] = None) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {"query": query, "limit": min(limit, 100), "fields": FIELDS}
    if min_year:
        params["year"] = f"{min_year}-"
    data = api_get(SEARCH_URL, params=params)
    return [enrich_paper(paper, source=SOURCE_NAME) for paper in data.get("data", [])]


def paper_lookup(identifier: str) -> Optional[Dict[str, Any]]:
    """按 Semantic Scholar ID / DOI / 论文 URL 查单篇（失败返回 None，不抛异常）。"""
    value = (identifier or "").strip()
    if not value:
        return None
    if value.lower().startswith("https://www.semanticscholar.org/paper/"):
        value = value.rstrip("/").split("/")[-1]
    if re.match(r"^10\.\d+/.+", value, re.I):
        value = "DOI:" + value
    url = GRAPH_URL + quote(value, safe=":")
    try:
        return enrich_paper(api_get(url, params={"fields": FIELDS}), source=SOURCE_NAME)
    except requests.HTTPError:
        return None


def recommendations(seed_ids: List[str], limit: int = 30) -> List[Dict[str, Any]]:
    """基于种子论文的相似推荐（Semantic Scholar 的推荐端点）。"""
    if not seed_ids:
        return []
    payload = {"positivePaperIds": seed_ids, "negativePaperIds": []}
    params = {"limit": min(limit, 500), "fields": FIELDS}
    data = api_post(RECOMMEND_URL, payload, params=params)
    if isinstance(data, list):
        papers = data
    elif isinstance(data, dict) and "recommendedPapers" in data:
        papers = data["recommendedPapers"]
    else:
        papers = data.get("data", []) if isinstance(data, dict) else []
    return [enrich_paper(paper, source=SOURCE_NAME) for paper in papers[:limit]]
