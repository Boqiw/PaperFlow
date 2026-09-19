"""笔记的读取、截断，以及周报里那个机器可读的 PF:WEEK 块。"""

from __future__ import annotations

import json

from paperflow_core import notes


def test_read_notes_picks_up_markdown_except_the_queue(cfg):
    folder = cfg.ensure_week_dir(1)
    (folder / "读了什么.md").write_text("metastability 很关键", encoding="utf-8")
    (folder / "阅读清单.md").write_text("这是脚本写的", encoding="utf-8")
    (folder / "note.txt").write_text("不是 markdown", encoding="utf-8")

    corpus = notes.read_notes(cfg, [1])
    assert [note.name for note in corpus.notes] == ["读了什么.md"]
    assert corpus.weeks == [1]
    assert not corpus.trimmed


def test_read_notes_skips_empty_files(cfg):
    (cfg.ensure_week_dir(1) / "空的.md").write_text("   \n\n", encoding="utf-8")
    assert notes.read_notes(cfg, [1]).empty


def test_read_notes_truncates_one_huge_note(cfg):
    (cfg.ensure_week_dir(1) / "很长.md").write_text("奶" * 5000, encoding="utf-8")
    corpus = notes.read_notes(cfg, [1], per_note=100, total=10000)
    assert corpus.trimmed
    assert len(corpus.notes[0].text) == 100


def test_read_notes_stops_when_the_total_budget_is_gone(cfg):
    folder = cfg.ensure_week_dir(1)
    for index in range(5):
        (folder / f"笔记{index}.md").write_text("字" * 100, encoding="utf-8")
    corpus = notes.read_notes(cfg, [1], per_note=1000, total=250)
    assert corpus.trimmed
    assert sum(len(note.text) for note in corpus.notes) <= 250
    assert len(corpus.notes) == 3


def test_read_notes_reads_several_weeks_in_order(cfg):
    (cfg.ensure_week_dir(1) / "a.md").write_text("第一周", encoding="utf-8")
    (cfg.ensure_week_dir(2) / "b.md").write_text("第二周", encoding="utf-8")
    corpus = notes.read_notes(cfg, [1, 2])
    assert [note.week for note in corpus.notes] == [1, 2]
    assert corpus.weeks == [1, 2]


def test_mentions_matches_a_note_that_cites_the_title(cfg):
    (cfg.ensure_week_dir(1) / "笔记.md").write_text(
        "今天读了 Metastability in resting-state brain dynamics 这篇，HMM 的部分没看懂。",
        encoding="utf-8",
    )
    corpus = notes.read_notes(cfg, [1])
    assert corpus.mentions("Metastability in resting-state brain dynamics: a review")
    assert not corpus.mentions("A completely different paper about protein folding")


def test_mentions_ignores_titles_that_are_too_short_to_be_informative(cfg):
    (cfg.ensure_week_dir(1) / "笔记.md").write_text("Editorial", encoding="utf-8")
    assert not notes.read_notes(cfg, [1]).mentions("Editorial")


def test_prompt_block_labels_the_week_and_note_name(cfg):
    (cfg.ensure_week_dir(2) / "卡点.md").write_text("不知道 metastability 怎么估计", encoding="utf-8")
    block = notes.read_notes(cfg, [2]).prompt_block("zh")
    assert "[第2周]" in block
    assert "卡点.md" in block
    assert "怎么估计" in block


def test_prompt_block_says_so_when_empty(cfg):
    assert "没有任何笔记" in notes.read_notes(cfg, [1]).prompt_block("zh")


def test_pf_block_round_trip():
    payload = {"week": 3, "window": "2026-10-05..2026-10-11", "titles": ["A", "B"], "mode": "llm"}
    text = "# 周报\n\n正文\n\n" + notes.render_pf_block(payload) + "\n"
    assert notes.parse_pf_blocks(text) == [payload]


def test_parse_pf_blocks_survives_a_broken_block():
    text = "<!-- PF:WEEK {oops -->\n<!-- PF:WEEK {\"week\": 2} -->"
    assert notes.parse_pf_blocks(text) == [{"week": 2}]


def test_read_pf_blocks_sorts_by_week_across_files(cfg, vault):
    from tests.conftest import write_report_block

    write_report_block(cfg, 2, titles=["第二周那篇"])
    write_report_block(cfg, 1, titles=["第一周那篇"])
    blocks = notes.read_pf_blocks(cfg.week_report_dir)
    assert [block["week"] for block in blocks] == [1, 2]


def test_read_pf_blocks_on_a_missing_directory(tmp_path):
    assert notes.read_pf_blocks(tmp_path / "没有这个文件夹") == []


def test_global_history_block_uses_ledger_stats_when_available():
    blocks = [
        {"week": 1, "window": "a..b", "themes": "EEG×2", "titles": ["T1", "T2"], "uncovered": "闭环实验"},
    ]
    text = notes.global_history_block(blocks, {1: {"pushed": 2, "read": 1, "skipped": 0, "unmarked": 1}})
    assert "第1周" in text
    assert "已读 1" in text
    assert "推 → T1" in text
    assert "未被覆盖的长期目标" in text
    assert "闭环实验" in text


def test_global_history_block_falls_back_to_title_count():
    blocks = [{"week": 4, "window": "", "themes": "", "titles": ["A", "B", "C"]}]
    text = notes.global_history_block(blocks, {})
    assert "推荐 3" in text


def test_global_history_block_says_when_there_is_no_history():
    assert "还没有" in notes.global_history_block([], {})


def test_pf_block_is_valid_json_so_it_survives_a_manual_edit():
    payload = {"week": 1, "titles": ["中文标题也可以"]}
    raw = notes.render_pf_block(payload)
    body = raw[len("<!-- PF:WEEK ") : -len(" -->")]
    assert json.loads(body) == payload
