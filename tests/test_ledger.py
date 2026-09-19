"""进度.csv：唯一的去重依据，也是「读了没读」的真相来源。"""

from __future__ import annotations

from paperflow_core import ledger


def row(**overrides):
    base = {
        "week": "1",
        "pushed_on": "2026-09-21",
        "paper_id": "s2:1",
        "title": "Metastability in resting-state brain dynamics",
        "doi": "10.1000/xyz",
        "year": "2023",
        "venue": "NeuroImage",
        "url": "https://example.org/p",
        "role": "deep",
        "status": ledger.STATUS_TODO,
        "evidence": "0",
    }
    base.update(overrides)
    return base


def test_key_prefers_doi_then_id_then_long_title():
    assert ledger.key_of_paper({"doi": "10.1000/xyz", "paperId": "s2:1", "title": "x"}) == "doi:10.1000/xyz"
    assert ledger.key_of_paper({"paperId": "s2:1", "title": "x"}) == "id:s2:1"
    assert (
        ledger.key_of_paper({"title": "Metastability in resting-state brain dynamics"})
        == "title:metastability in resting-state brain dynamics"
    )


def test_short_titles_are_not_usable_as_identity():
    """「Editorial」这类短标题会把不相关的论文判成同一篇，宁可不判。"""
    assert ledger.key_of_paper({"title": "Editorial"}) == ""
    assert ledger.key_of_paper({}) == ""


def test_doi_key_ignores_case_and_url_prefix():
    assert ledger.key_of_paper({"doi": "https://doi.org/10.1000/XYZ"}) == ledger.key_of_paper({"doi": "10.1000/xyz"})


def test_write_then_read_ledger_round_trip(tmp_path):
    path = tmp_path / "进度.csv"
    ledger.write_ledger(path, [row()])
    rows = ledger.read_ledger(path)
    assert len(rows) == 1
    assert rows[0]["title"] == "Metastability in resting-state brain dynamics"
    assert rows[0]["status"] == ledger.STATUS_TODO


def test_write_ledger_creates_the_header_for_an_empty_list(tmp_path):
    path = tmp_path / "进度.csv"
    ledger.write_ledger(path, [])
    assert path.read_text(encoding="utf-8").strip() == ",".join(ledger.LEDGER_FIELDS)


def test_ledger_escapes_commas_and_newlines(tmp_path):
    path = tmp_path / "进度.csv"
    ledger.write_ledger(path, [row(title="A, B: a study of\nthings that matter")])
    assert ledger.read_ledger(path)[0]["title"] == "A, B: a study of\nthings that matter"


def test_read_ledger_backs_up_a_file_with_the_wrong_schema(tmp_path):
    path = tmp_path / "进度.csv"
    path.write_text("concept,level\nbrain_state,2\n", encoding="utf-8")
    assert ledger.read_ledger(path) == []
    assert path.with_name("进度.csv.bak").exists()


def test_read_ledger_of_a_missing_file_is_empty(tmp_path):
    assert ledger.read_ledger(tmp_path / "没有.csv") == []


def test_filter_unseen_drops_known_papers(paper):
    rows = [row(doi="10.1000/xyz")]
    fresh = ledger.filter_unseen([paper(), paper(doi="10.1000/new", paperId="s2:2")], rows)
    assert [item["paperId"] for item in fresh] == ["s2:2"]


def test_filter_unseen_keeps_papers_it_cannot_identify(paper):
    """算不出身份的论文宁可留下：漏一篇候选比重复推一篇更糟。"""
    assert len(ledger.filter_unseen([paper(doi="", paperId="", title="Editorial")], [row()])) == 1


def test_make_row_starts_as_unread(paper):
    made = ledger.make_row(paper(), 3, "2026-10-05", "deep")
    assert made["week"] == "3"
    assert made["status"] == ledger.STATUS_TODO
    assert made["evidence"] == "0"
    assert made["role"] == "deep"


def test_refresh_flips_script_written_todo_to_read():
    rows, report = ledger.refresh([row()], lambda title: True)
    assert rows[0]["status"] == ledger.STATUS_READ
    assert rows[0]["evidence"] == "1"
    assert report == {"flipped": 1, "with_notes": 1}


def test_refresh_never_overwrites_a_human_decision():
    """你在 CSV 里手写的「跳过」是最终决定，脚本不许改回去。"""
    rows, _ = ledger.refresh([row(status=ledger.STATUS_SKIP)], lambda title: False)
    assert rows[0]["status"] == ledger.STATUS_SKIP
    assert rows[0]["evidence"] == "0"


def test_refresh_clears_evidence_when_the_note_disappears():
    rows, _ = ledger.refresh([row(status=ledger.STATUS_READ, evidence="1")], lambda title: False)
    assert rows[0]["evidence"] == "0"
    assert rows[0]["status"] == ledger.STATUS_READ  # 已读是人写的，保留


def test_refresh_does_not_mutate_the_input():
    original = [row()]
    ledger.refresh(original, lambda title: True)
    assert original[0]["status"] == ledger.STATUS_TODO


def test_stats_counts_by_status():
    rows = [
        row(status=ledger.STATUS_TODO),
        row(status=ledger.STATUS_READ),
        row(status=ledger.STATUS_SKIP),
        row(status=""),
    ]
    assert ledger.stats(rows) == {"pushed": 4, "read": 1, "skipped": 1, "unmarked": 2}


def test_week_rows_filters_by_week():
    assert len(ledger.week_rows([row(week="1"), row(week="2")], 2)) == 1
