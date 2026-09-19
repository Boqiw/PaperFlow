"""数据源注册表与适配器工具测试（全部离线）。"""

from __future__ import annotations

import pytest

from paperflow_core.sources import arxiv, openalex, registry


def test_parse_sources_handles_aliases_and_separators():
    assert registry.parse_sources("s2, openalex") == ["semanticscholar", "openalex"]
    assert registry.parse_sources("openalex;arxiv") == ["openalex", "arxiv"]
    assert registry.parse_sources("ncbi") == ["pubmed"]
    assert registry.parse_sources("semantic-scholar") == ["semanticscholar"]


def test_parse_sources_dedupes():
    assert registry.parse_sources("s2,semanticscholar") == ["semanticscholar"]


def test_parse_sources_auto_and_empty_mean_let_auto_decide():
    assert registry.parse_sources("auto") == []
    assert registry.parse_sources(" AUTO ") == []
    assert registry.parse_sources(None) == []
    assert registry.parse_sources("") == []


def test_parse_sources_rejects_unknown_names():
    with pytest.raises(SystemExit):
        registry.parse_sources("scopus")


def test_plan_sources_respects_explicit_choice():
    assert registry.plan_sources(["arxiv"]) == ["arxiv"]


def test_plan_sources_falls_back_to_openalex_without_s2_key(monkeypatch):
    monkeypatch.delenv("S2_API_KEY", raising=False)
    assert registry.plan_sources([]) == ["openalex", "arxiv", "pubmed"]


def test_plan_sources_prefers_semantic_scholar_with_s2_key(monkeypatch):
    monkeypatch.setenv("S2_API_KEY", "dummy")
    assert registry.plan_sources([]) == ["semanticscholar", "arxiv", "pubmed"]


def test_build_queries_adds_review_and_modeling_variants():
    queries = registry.build_queries("brain state")
    assert queries[0] == "brain state"
    assert len(queries) == len(registry.QUERY_SUFFIXES)
    assert len(set(queries)) == len(queries)


def test_backend_pause_is_more_conservative_without_keys(monkeypatch):
    monkeypatch.delenv("S2_API_KEY", raising=False)
    slow = registry.backend_pause("semanticscholar")
    monkeypatch.setenv("S2_API_KEY", "dummy")
    fast = registry.backend_pause("semanticscholar")
    assert fast < slow
    assert registry.backend_pause("arxiv") >= 1.0


def test_every_registered_source_is_labeled():
    for name in registry.SOURCE_BACKENDS:
        assert name in registry.SOURCE_LABELS, f"{name} 缺少展示名"
    for alias, name in registry.SOURCE_ALIASES.items():
        assert name in registry.SOURCE_BACKENDS, f"别名 {alias} 指向未注册的后端"


# ----------------------------------------------------------- OpenAlex 摘要


def test_openalex_abstract_rebuilds_from_inverted_index():
    inverted = {"World": [1], "Hello": [0], "again": [2]}
    assert openalex.openalex_abstract(inverted) == "Hello World again"


def test_openalex_abstract_handles_empty_input():
    assert openalex.openalex_abstract(None) == ""
    assert openalex.openalex_abstract({}) == ""


# --------------------------------------------------------------- arXiv 查询


def test_arxiv_query_joins_terms_with_and():
    assert arxiv.arxiv_query("brain state") == "all:brain AND all:state"
    assert arxiv.arxiv_query("  brain   state  ") == "all:brain AND all:state"
    # 空查询退化成 `all:`，不会抛错，也不会拼出悬空的 AND。
    assert arxiv.arxiv_query("") == "all:"


def test_arxiv_uses_export_endpoint():
    assert arxiv.QUERY_URL.startswith("https://export.arxiv.org/")
