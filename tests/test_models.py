"""论文数据模型测试：归一化、去重、文件名生成。"""

from __future__ import annotations

from paperflow_core import models


def test_normalise_title_collapses_punctuation_and_case():
    assert models.normalise_title("Brain State: Dynamics!") == "brain state dynamics"
    assert models.normalise_title("A   B") == "a b"
    assert models.normalise_title(None) == ""


def test_clean_doi_strips_url_prefix():
    assert models.clean_doi("https://doi.org/10.1/abc") == "10.1/abc"
    assert models.clean_doi("http://dx.doi.org/10.1/abc") == "10.1/abc"
    assert models.clean_doi("HTTPS://DOI.ORG/10.1/abc") == "10.1/abc"
    assert models.clean_doi("10.1/abc") == "10.1/abc"
    assert models.clean_doi(None) == ""


def test_paper_key_prefers_doi_over_title():
    with_doi = {"title": "T", "externalIds": {"DOI": "10.1/ABC"}}
    assert models.paper_key(with_doi) == "doi:10.1/abc"
    without_doi = {"title": "Brain State!"}
    assert models.paper_key(without_doi) == "title:brain state"


def test_enrich_paper_fills_derived_fields():
    paper = models.enrich_paper(
        {
            "title": "T",
            "abstract": "a",
            "authors": [{"name": "Ada"}, {"name": "Bob"}],
            "externalIds": {"DOI": "10.1/x"},
            "openAccessPdf": {"url": "http://pdf"},
        },
        source="openalex",
    )
    assert paper["key"] == "doi:10.1/x"
    assert paper["doi"] == "10.1/x"
    assert paper["open_pdf"] == "http://pdf"
    assert paper["author_text"] == "Ada, Bob"
    assert paper["source"] == "openalex"


def test_enrich_paper_handles_missing_fields():
    paper = models.enrich_paper({})
    assert paper["authors"] == []
    assert paper["abstract"] == ""
    assert paper["externalIds"] == {}
    assert paper["doi"] == ""
    assert paper["open_pdf"] == ""
    assert paper["author_text"] == ""


def test_enrich_paper_truncates_overlong_abstract():
    paper = models.enrich_paper({"title": "T", "abstract": "x" * (models.ABSTRACT_LIMIT + 500)})
    assert len(paper["abstract"]) <= models.ABSTRACT_LIMIT + 5
    assert paper["abstract"].endswith("...")


def test_dedupe_drops_duplicates_and_titleless_records():
    papers = [
        models.enrich_paper({"title": "A", "externalIds": {"DOI": "10.1/x"}}),
        models.enrich_paper({"title": "A (preprint)", "externalIds": {"DOI": "10.1/x"}}),
        models.enrich_paper({"title": ""}),
        models.enrich_paper({"title": "B"}),
    ]
    kept = models.dedupe(papers)
    assert [p["title"] for p in kept] == ["A", "B"]


def test_dedupe_uses_title_when_no_doi():
    papers = [
        models.enrich_paper({"title": "Brain State!"}),
        models.enrich_paper({"title": "brain   state"}),
    ]
    assert len(models.dedupe(papers)) == 1
