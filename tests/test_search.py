"""检索编排测试：引用门槛必须在打分之前生效，年份窗口在打分之后生效。

这些用例不联网——数据源整体被替换掉。它们钉住三件事：
低引用的候选在进 prompt 之前就被丢掉；门槛可以关掉（``min_citations=0``）；
候选池优先装近 5 年的，装不满才用近 10 年以内的补，再老的一律不要。
"""

from __future__ import annotations

import datetime as dt

from paperflow_core import search

THIS_YEAR = dt.date.today().year


def paper(pid, year, citations, source="openalex"):
    return {
        "paperId": pid,
        "title": f"Brain state dynamics {pid}",
        "abstract": "A computational model of neural dynamics.",
        "year": year,
        "citationCount": citations,
        "influentialCitationCount": 0,
        "source": source,
        "externalIds": {"DOI": f"10.1000/{pid}"},
        "authors": [],
    }


def patch_sources(monkeypatch, pool):
    """把后端换成一份固定的候选，并让引用回填变成空操作。"""
    monkeypatch.setattr(
        search,
        "run_backend",
        lambda name, queries, min_year, max_candidates: (list(pool), True),
    )
    monkeypatch.setattr(search.openalex, "backfill_citations", lambda papers: None)


POOL = [
    # 9 年前的老经典：过了引用门槛、也在「近 10 年」里，但不在「近 5 年」那一档。
    paper("classic", THIS_YEAR - 9, 3200),
    paper("recent-solid", THIS_YEAR - 3, 60),
    paper("zero-citation", THIS_YEAR - 8, 0),
    paper("fresh-zero", THIS_YEAR, 0),
]


def test_build_candidates_drops_papers_below_the_citation_floor(monkeypatch, capsys):
    patch_sources(monkeypatch, POOL)

    kept = search.build_candidates("brain state", [], 2000, 40, sources="openalex")

    assert [item["paperId"] for item in kept] == ["classic", "recent-solid"]
    err = capsys.readouterr().err
    assert "too few citations" in err
    assert "--min-citations 0" in err


def test_build_candidates_reports_how_many_were_dropped(monkeypatch, capsys):
    patch_sources(monkeypatch, POOL)

    search.build_candidates("brain state", [], 2000, 40, sources="openalex")

    assert "2 of 4 candidates" in capsys.readouterr().err


def test_build_candidates_can_disable_the_gate(monkeypatch, capsys):
    patch_sources(monkeypatch, POOL)

    kept = search.build_candidates("brain state", [], 2000, 40, sources="openalex", min_citations=0)

    assert len(kept) == 4
    assert "too few citations" not in capsys.readouterr().err


def test_build_candidates_scores_and_sorts_by_score(monkeypatch):
    patch_sources(monkeypatch, POOL)

    kept = search.build_candidates(
        "brain state", [], 2000, 40, context="neural dynamics", sources="openalex"
    )

    scores = [item["heuristic_score"] for item in kept]
    assert scores == sorted(scores, reverse=True)
    # 被引 3200 的老经典应该排在被引 60 的新论文前面。
    assert kept[0]["paperId"] == "classic"


def test_build_candidates_respects_max_candidates(monkeypatch):
    patch_sources(monkeypatch, POOL)

    kept = search.build_candidates("brain state", [], 2000, 1, sources="openalex")

    assert len(kept) == 1


def test_build_candidates_fills_up_from_the_recent_window_first(monkeypatch):
    """候选池优先只装近 5 年的：窗口内够数，老的就不再进来。"""
    pool = [
        paper("fresh-1", THIS_YEAR, 100),
        paper("fresh-2", THIS_YEAR - 1, 100),
        paper("fresh-3", THIS_YEAR - 4, 100),
        paper("old-1", THIS_YEAR - 7, 5000),
        paper("old-2", THIS_YEAR - 9, 5000),
    ]
    patch_sources(monkeypatch, pool)

    kept = search.build_candidates("brain state", [], 2000, 3, sources="openalex")

    assert {item["paperId"] for item in kept} == {"fresh-1", "fresh-2", "fresh-3"}


def test_build_candidates_widens_to_ten_years_when_the_recent_window_is_thin(monkeypatch):
    """近 5 年凑不满候选数：用近 10 年以内的补足，但不越过 10 年。"""
    pool = [
        paper("fresh-1", THIS_YEAR - 2, 100),
        paper("old-1", THIS_YEAR - 7, 5000),
        paper("old-2", THIS_YEAR - 9, 5000),
        paper("ancient", THIS_YEAR - 12, 9000),
    ]
    patch_sources(monkeypatch, pool)

    kept = search.build_candidates("brain state", [], 2000, 3, sources="openalex")

    ids = {item["paperId"] for item in kept}
    assert ids == {"fresh-1", "old-1", "old-2"}
    # 12 年前的老经典就算被引 9000 次也不能进来。
    assert "ancient" not in ids


def test_build_candidates_can_disable_the_year_window(monkeypatch):
    """``years=0`` 关掉年份窗口：年份再老也只看引用门槛。"""
    pool = [paper("fresh-1", THIS_YEAR - 2, 100), paper("ancient", THIS_YEAR - 12, 9000)]
    patch_sources(monkeypatch, pool)

    kept = search.build_candidates(
        "brain state", [], 2000, 10, sources="openalex", years=0, max_years=0
    )

    assert {item["paperId"] for item in kept} == {"fresh-1", "ancient"}


def test_build_candidates_falls_back_to_openalex_when_s2_fails(monkeypatch, capsys):
    monkeypatch.setattr(search, "plan_sources", lambda parsed: ["semanticscholar"])
    seen = []

    def fake_run(name, queries, min_year, max_candidates):
        seen.append(name)
        return ([], name != "semanticscholar")

    monkeypatch.setattr(search, "run_backend", fake_run)
    monkeypatch.setattr(search.openalex, "backfill_citations", lambda papers: None)

    search.build_candidates("brain state", [], 2000, 5, sources="auto", min_citations=0)

    assert seen == ["semanticscholar", "openalex"]
    assert "falling back to OpenAlex" in capsys.readouterr().err


def test_build_candidates_keeps_seed_lookups(monkeypatch):
    """种子论文是用户点名要的，照收；顺便拿它去要推荐。"""
    seed = paper("seed-1", THIS_YEAR - 7, 900)
    rec = paper("rec-1", THIS_YEAR - 6, 700)
    monkeypatch.setattr(search, "run_backend", lambda *a, **k: ([], True))
    monkeypatch.setattr(search.openalex, "backfill_citations", lambda papers: None)
    monkeypatch.setattr(search.semantic_scholar, "paper_lookup", lambda value: dict(seed))
    monkeypatch.setattr(search.semantic_scholar, "recommendations", lambda ids, limit=0: [dict(rec)])
    monkeypatch.setattr(search, "plan_sources", lambda parsed: [])

    kept = search.build_candidates(
        "brain state", ["s2:seed-1"], 2000, 40, sources="openalex", min_citations=0
    )

    assert [item["paperId"] for item in kept] == ["seed-1", "rec-1"]


def test_build_candidates_survives_a_broken_recommendation_lookup(monkeypatch, capsys):
    seed = paper("seed-1", THIS_YEAR - 7, 900)
    monkeypatch.setattr(search, "run_backend", lambda *a, **k: ([], True))
    monkeypatch.setattr(search.openalex, "backfill_citations", lambda papers: None)
    monkeypatch.setattr(search.semantic_scholar, "paper_lookup", lambda value: dict(seed))

    def boom(ids, limit=0):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(search.semantic_scholar, "recommendations", boom)

    kept = search.build_candidates(
        "brain state", ["s2:seed-1"], 2000, 40, sources="openalex", min_citations=0
    )

    assert [item["paperId"] for item in kept] == ["seed-1"]
    assert "recommendation lookup failed" in capsys.readouterr().err
