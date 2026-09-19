"""候选池的质量闸门：引用门槛 + 年份窗口 + 确定性打分。

这个文件的目的是把两条要求钉在代码里：
「推荐的文章一定要引用量高」——引用不够的论文，不管标题多贴都不许出现在候选池里；
「只推最近的工作」——近 5 年优先，凑不够才放宽到近 10 年，再老的一律不要。
"""

from __future__ import annotations

import datetime as dt

from paperflow_core import ranking

THIS_YEAR = dt.date.today().year


def paper(year=None, citations=0, influential=0, **overrides):
    item = {
        "paperId": f"p:{year}:{citations}",
        "title": "Brain state dynamics",
        "abstract": "A computational model of neural dynamics.",
        "year": year,
        "citationCount": citations,
        "influentialCitationCount": influential,
        "source": "openalex",
    }
    item.update(overrides)
    return item


# --------------------------------------------------------------- citation_signal


def test_citation_signal_takes_the_larger_of_the_two_fields():
    """OpenAlex 把 influential 写死成 0，所以不能「谁非空用谁」。"""
    assert ranking.citation_signal({"citationCount": 120, "influentialCitationCount": 0}) == 120
    assert ranking.citation_signal({"citationCount": 0, "influentialCitationCount": 7}) == 7
    assert ranking.citation_signal({"influentialCitationCount": 9}) == 9


def test_citation_signal_tolerates_missing_fields():
    assert ranking.citation_signal({}) == 0
    assert ranking.citation_signal({"citationCount": None}) == 0
    assert ranking.citation_signal({"citationCount": "12"}) == 12


# --------------------------------------------------------------- citation_floor


def test_citation_floor_scales_by_age():
    """老论文要求高、新论文按比例放宽——但永远不是 0。"""
    base = 50
    assert ranking.citation_floor(THIS_YEAR - ranking.CITATION_BASE_YEARS, base) == 50
    assert ranking.citation_floor(THIS_YEAR - 20, base) == 50
    assert ranking.citation_floor(THIS_YEAR - 3, base) == 20
    assert ranking.citation_floor(THIS_YEAR, base) == 5
    assert ranking.citation_floor(THIS_YEAR - 1, base) == 5


def test_citation_floor_is_never_zero_while_the_gate_is_on():
    """哪怕论文刚挂上网，也要有引用才进得来：0 引用的论文一律挡掉。"""
    for year in (THIS_YEAR, THIS_YEAR - 1, None, 0):
        assert ranking.citation_floor(year, 50) >= 1


def test_citation_floor_without_a_year_is_the_strictest_tier():
    assert ranking.citation_floor(None, 50) == ranking.citation_floor(1990, 50)


def test_citation_floor_can_be_disabled():
    assert ranking.citation_floor(1990, 0) == 0
    assert ranking.citation_floor(None, -1) == 0


# ------------------------------------------------------------ passes / split


def test_passes_citation_floor():
    assert ranking.passes_citation_floor(paper(THIS_YEAR - 10, citations=50))
    assert not ranking.passes_citation_floor(paper(THIS_YEAR - 10, citations=49))
    assert ranking.passes_citation_floor(paper(THIS_YEAR - 1, citations=5))
    assert not ranking.passes_citation_floor(paper(THIS_YEAR - 1, citations=4))


def test_a_zero_citation_paper_never_passes():
    for year in (THIS_YEAR, THIS_YEAR - 1, THIS_YEAR - 6, None):
        assert not ranking.passes_citation_floor(paper(year, citations=0))


def test_everything_passes_when_the_gate_is_off():
    assert ranking.passes_citation_floor(paper(THIS_YEAR, citations=0), base=0)


def test_split_by_citation_floor_keeps_the_order():
    pool = [
        paper(THIS_YEAR - 6, citations=1000),
        paper(THIS_YEAR, citations=0),
        paper(THIS_YEAR - 3, citations=30),
    ]
    kept, dropped = ranking.split_by_citation_floor(pool, base=50)
    assert [item["citationCount"] for item in kept] == [1000, 30]
    assert [item["citationCount"] for item in dropped] == [0]


# --------------------------------------------------------------- heuristic_score


def test_more_citations_score_higher():
    fresh = paper(2020, citations=5)
    classic = paper(2020, citations=5000)
    assert ranking.heuristic_score(classic, "", "brain state") > ranking.heuristic_score(
        fresh, "", "brain state"
    )


def test_a_heavily_cited_paper_beats_a_fresh_uncited_one():
    """这条就是「宁推经典综述，不推零引用新预印本」的量化版本。"""
    old_review = paper(2010, citations=4000, title="Neural dynamics: a review", abstract="")
    new_preprint = paper(THIS_YEAR, citations=0, title="Neural dynamics: a review", abstract="")
    assert ranking.heuristic_score(old_review, "", "neural dynamics") > ranking.heuristic_score(
        new_preprint, "", "neural dynamics"
    )


def test_citation_weight_is_capped():
    """引用分封顶，否则一篇十万引用的论文会把其他信号全压掉。"""
    a = ranking.heuristic_score(paper(2010, citations=10_000), "", "")
    b = ranking.heuristic_score(paper(2010, citations=1_000_000), "", "")
    assert a == b


def test_score_stays_within_ten():
    monster = paper(
        THIS_YEAR - 1,
        citations=999_999,
        title="brain state neural dynamics population dynamics state transition "
        "connectivity eeg fmri computational model",
        abstract="brain state neural dynamics population dynamics state transition "
        "connectivity eeg fmri computational model",
    )
    score = ranking.heuristic_score(monster, "brain state eeg fmri connectivity", "eeg fmri")
    assert 0.0 <= score <= 10.0


def test_score_all_writes_the_field_in_place():
    papers = [paper(2020, citations=10), paper(2020, citations=1000)]
    ranking.score_all(papers, "eeg", "eeg")
    assert all("heuristic_score" in item for item in papers)


# --------------------------------------------------------------- year window

TODAY = dt.date(2026, 9, 24)


def scored(pid, year, score):
    """带分数的论文（年份窗口是在打分之后生效的，所以候选得有分数）。"""
    return paper(year, citations=10, paperId=pid, heuristic_score=score)


def test_year_cutoff_counts_back_from_this_year():
    assert ranking.year_cutoff(5, TODAY) == 2021
    assert ranking.year_cutoff(10, TODAY) == 2016
    assert ranking.year_cutoff(0, TODAY) == 0
    assert ranking.year_cutoff(-3, TODAY) == 0


def test_default_window_is_five_years_and_at_most_ten():
    assert ranking.YEAR_WINDOW == 5
    assert ranking.YEAR_WINDOW_MAX == 10
    assert ranking.YEAR_WINDOW < ranking.YEAR_WINDOW_MAX


def test_in_year_window_needs_a_readable_year():
    assert ranking.in_year_window(scored("a", 2021, 1.0), 5, TODAY)
    assert not ranking.in_year_window(scored("b", 2020, 1.0), 5, TODAY)
    # 年份读不到的当作「不新」：宁可不推，也不把它当新文章。
    assert not ranking.in_year_window(scored("c", None, 1.0), 5, TODAY)
    assert not ranking.in_year_window(scored("d", 0, 1.0), 5, TODAY)
    assert ranking.in_year_window(scored("e", 1970, 1.0), 0, TODAY)


def test_select_by_year_window_keeps_the_recent_ones_only():
    pool = [
        scored("old-high", 2015, 9.0),
        scored("new-high", 2023, 8.0),
        scored("new-low", 2024, 1.0),
    ]
    kept = ranking.select_by_year_window(pool, 2, 5, 10, TODAY)
    assert [item["paperId"] for item in kept] == ["new-high", "new-low"]


def test_select_by_year_window_never_reaches_past_the_ceiling():
    pool = [scored("ancient", 2010, 99.0), scored("recent", 2025, 1.0)]
    kept = ranking.select_by_year_window(pool, 40, 5, 10, TODAY)
    assert [item["paperId"] for item in kept] == ["recent"]


def test_select_by_year_window_tops_up_in_score_order():
    """放宽那一档也是分高的先入选，而且总人数不超 max_candidates。

    ``pool`` 跟 ``search.py`` 的调用点一样，已经按 ``heuristic_score`` 降序排好了。
    """
    pool = [
        scored("too-old", 2014, 100.0),
        scored("widen-high", 2017, 9.0),
        scored("widen-low", 2018, 2.0),
        scored("recent", 2025, 1.0),
    ]
    kept = ranking.select_by_year_window(pool, 2, 5, 10, TODAY)
    assert [item["paperId"] for item in kept] == ["widen-high", "recent"]


def test_select_by_year_window_with_zero_years_is_a_plain_truncation():
    pool = [scored("b", 2025, 2.0), scored("a", 1999, 1.0)]
    assert ranking.select_by_year_window(pool, 5, 0, 0, TODAY) == pool


def test_select_by_year_window_lets_unknown_years_in_only_as_filler():
    unknown = scored("unknown", None, 5.0)
    recent = scored("recent", 2025, 1.0)
    # 窗口够数就不要它。
    assert [p["paperId"] for p in ranking.select_by_year_window([unknown, recent], 1, 5, 10, TODAY)] == [
        "recent"
    ]
    # 窗口不够数时它能补进来。
    assert [p["paperId"] for p in ranking.select_by_year_window([unknown, recent], 2, 5, 10, TODAY)] == [
        "unknown",
        "recent",
    ]


def test_year_window_stats_counts_each_band():
    pool = [scored("a", 2025, 1.0), scored("b", 2019, 1.0), scored("c", 2000, 1.0), scored("d", None, 1.0)]
    stats = ranking.year_window_stats(pool, 5, 10, TODAY)
    assert stats == {"total": 4, "in_window": 1, "widened": 1, "unknown": 1, "oldest": 2000, "newest": 2025}


def test_year_window_stats_can_handle_an_empty_pool():
    stats = ranking.year_window_stats([], 5, 10, TODAY)
    assert stats["total"] == 0
    assert stats["oldest"] == 0


# --------------------------------------------------------------------- diversify


def test_diversify_rotates_between_sources():
    pool = [paper(2020, citations=i, source="openalex" if i % 2 else "pubmed") for i in range(6)]
    picked = ranking.diversify(pool, 4)
    assert len(picked) == 4
    assert len({item["source"] for item in picked}) == 2


def test_diversify_with_one_source_is_a_plain_truncation():
    pool = [paper(2020, citations=i) for i in range(6)]
    assert ranking.diversify(pool, 3) == pool[:3]
