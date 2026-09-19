"""扁平词表：检索式从哪里来，主题归类的依据。"""

from __future__ import annotations

from paperflow_core import terms


def test_terms_is_a_flat_english_to_chinese_table():
    assert terms.TERMS and all(isinstance(term, str) and isinstance(label, str) for term, label in terms.TERMS)
    # 词表必须唯一：同一个英文词不能对应两个中文标签，否则归类会重复计数。
    assert len({term for term, _ in terms.TERMS}) == len(terms.TERMS)
    assert dict(terms.TERMS)["eeg"] == "EEG"


def test_count_terms_ranks_by_hits():
    counts = dict(terms.count_terms("EEG EEG EEG fMRI"))
    assert counts["eeg"] == 3
    assert counts["fmri"] == 1
    assert "attractor" not in counts


def test_count_terms_matches_whole_words_only():
    """``eeg`` 不该被 ``breege`` 之类的词命中，否则检索式会全歪。"""
    assert dict(terms.count_terms("breege")) == {}
    assert dict(terms.count_terms("eeg"))["eeg"] == 1


def test_count_terms_is_case_insensitive():
    assert terms.count_terms("Metastability metastability")[0] == ("metastability", 2)


def test_pick_queries_uses_the_text(cfg):
    picked = terms.pick_queries("我读了 metastability 和 eeg 的论文，还有 criticality")
    assert picked[0] == "metastability"
    assert {"metastability", "eeg", "criticality"} <= set(picked)


def test_pick_queries_falls_back_when_nothing_matches():
    assert terms.pick_queries("今天天气不错") == list(terms.FALLBACK_QUERIES)


def test_pick_queries_respects_limit():
    text = "eeg meg fmri bold microstate attractor"
    assert len(terms.pick_queries(text, limit=2)) == 2


def test_tag_titles_counts_per_label():
    tagged = dict(
        terms.tag_titles(
            [
                "Metastability in resting-state EEG",
                "An EEG microstate study",
                "A spiking network model",
            ]
        )
    )
    assert tagged["EEG"] == 2


def test_theme_line_is_human_readable():
    line = terms.theme_line(["Metastability in resting-state EEG", "Another EEG study"])
    assert "×" in line
    assert "EEG×2" in line


def test_theme_line_handles_nothing():
    assert terms.theme_line([]) == "（未归类）"
    assert terms.theme_line(["完全无关的标题"]) == "（未归类）"
