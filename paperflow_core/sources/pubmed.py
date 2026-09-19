"""NCBI PubMed 适配器：生物医学/临床方向覆盖好，无需 key。"""

from __future__ import annotations

import datetime as dt
import os
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

from ..config import contact_email
from ..http_client import api_get, api_get_bytes
from ..models import clean_doi, enrich_paper, normalise_title
from ._xml import xml_text

SOURCE_NAME = "pubmed"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"


def pubmed_params(extra: Dict[str, Any]) -> Dict[str, Any]:
    params: Dict[str, Any] = {"db": "pubmed", "tool": "paperflow"}
    params.update(extra)
    email = contact_email()
    if email:
        params["email"] = email
    if os.getenv("NCBI_API_KEY"):
        params["api_key"] = os.environ["NCBI_API_KEY"]
    return params


def _pub_year(body: Any) -> Optional[int]:
    journal = body.find("Journal")
    issue = journal.find("JournalIssue") if journal is not None else None
    pub_date = issue.find("PubDate") if issue is not None else None
    if pub_date is None:
        return None
    year_node = pub_date.find("Year")
    if year_node is not None and (year_node.text or "").strip().isdigit():
        return int(year_node.text.strip())
    match = re.match(r"(\d{4})", xml_text(pub_date.find("MedlineDate")))
    return int(match.group(1)) if match else None


def search(query: str, limit: int = 30, min_year: Optional[int] = None) -> List[Dict[str, Any]]:
    search_params = pubmed_params(
        {"term": query, "retmax": min(max(limit, 1), 200), "retmode": "json", "sort": "relevance"}
    )
    if min_year:
        search_params.update({"datetype": "pdat", "mindate": str(min_year), "maxdate": str(dt.date.today().year)})
    found = api_get(EUTILS + "esearch.fcgi", params=search_params)
    pmids = (found.get("esearchresult") or {}).get("idlist") or []
    if not pmids:
        return []

    root = ET.fromstring(
        api_get_bytes(EUTILS + "efetch.fcgi", params=pubmed_params({"id": ",".join(pmids), "retmode": "xml"}))
    )

    papers: List[Dict[str, Any]] = []
    for article in root.findall("PubmedArticle"):
        citation = article.find("MedlineCitation")
        body = citation.find("Article") if citation is not None else None
        if body is None:
            continue
        title = xml_text(body.find("ArticleTitle"))

        abstract = ""
        abstract_node = body.find("Abstract")
        if abstract_node is not None:
            abstract = " ".join(part for part in (xml_text(t) for t in abstract_node.findall("AbstractText")) if part)

        journal = body.find("Journal")
        authors: List[Dict[str, str]] = []
        author_list = body.find("AuthorList")
        if author_list is not None:
            for author in author_list.findall("Author"):
                name = " ".join(
                    x for x in (xml_text(author.find("ForeName")), xml_text(author.find("LastName"))) if x
                )
                if not name:
                    name = xml_text(author.find("CollectiveName"))
                if name:
                    authors.append({"name": name})

        pmid = xml_text(citation.find("PMID")) if citation is not None else ""
        external: Dict[str, str] = {}
        if pmid:
            external["PubMed"] = pmid
        pmc_id = ""
        data_node = article.find("PubmedData")
        id_list = data_node.find("ArticleIdList") if data_node is not None else None
        if id_list is not None:
            for node in id_list.findall("ArticleId"):
                value = (node.text or "").strip()
                kind = (node.get("IdType") or "").lower()
                if not value:
                    continue
                if kind == "doi":
                    external["DOI"] = clean_doi(value)
                elif kind == "pmc":
                    pmc_id = value
                    external["PMCID"] = value

        year = _pub_year(body)
        papers.append(
            enrich_paper(
                {
                    "paperId": f"pubmed:{pmid}" if pmid else f"title:{normalise_title(title)}",
                    "title": title,
                    "abstract": abstract,
                    "year": year,
                    "authors": authors,
                    "venue": xml_text(journal.find("Title")) if journal is not None else "",
                    "citationCount": 0,
                    "influentialCitationCount": 0,
                    "openAccessPdf": {
                        "url": f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc_id}/pdf/" if pmc_id else ""
                    },
                    "externalIds": external,
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
                    "publicationDate": str(year) if year else "",
                },
                source=SOURCE_NAME,
            )
        )
    return papers
