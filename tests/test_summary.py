"""周 / 月总结：数字必须来自脚本，AI 只负责叙述。"""

from __future__ import annotations

import datetime as dt

from paperflow_core import ledger, summary
from tests.conftest import write_week_note

WEEK1 = dt.date(2026, 9, 24)
WEEK2 = dt.date(2026, 9, 30)


def seed_ledger(cfg, paper, week, when, count=2, status=ledger.STATUS_TODO):
    rows = []
    for index in range(count):
        item = paper(paperId=f"s2:{week}-{index}", doi=f"10.1000/{week}{index}", title=f"Paper {week}-{index}")
        row = ledger.make_row(item, week, when, "deep")
        row["status"] = status
        rows.append(row)
    ledger.write_ledger(cfg.ledger_path, rows)
    return rows


# -------------------------------------------------------------------- _bounds


def test_week_bounds_label_and_range(cfg):
    label, start, end, weeks = summary._bounds(cfg, "week", WEEK1)
    assert label == "第1周"
    assert start == dt.date(2026, 9, 21)
    assert end == dt.date(2026, 9, 27)
    assert weeks == [1]


def test_month_bounds_cover_calendar_month_and_week_numbers(cfg):
    label, start, end, weeks = summary._bounds(cfg, "month", dt.date(2026, 10, 15))
    assert label == "2026-10"
    assert start == dt.date(2026, 10, 1)
    assert end == dt.date(2026, 10, 31)
    assert weeks == [2, 3, 4, 5, 6]


def test_month_before_the_start_date_has_no_weeks(cfg):
    _, _, _, weeks = summary._bounds(cfg, "month", dt.date(2026, 8, 15))
    assert weeks == []


# ------------------------------------------------------------------- collect


def test_collect_splits_pushed_and_carried(cfg, paper):
    """上个月推的、到现在还没读的，就是「遗留」——总结里必须点出来。"""
    carried = ledger.make_row(
        paper(paperId="s2:old", doi="10.1000/old", title="Older metastability paper"),
        1,
        "2026-09-21",
        "skim",
    )
    fresh = seed_ledger(cfg, paper, 6, "2026-10-26", count=2)
    ledger.write_ledger(cfg.ledger_path, [carried] + fresh)

    facts = summary.collect(cfg, "month", dt.date(2026, 10, 15))
    assert facts.label == "2026-10"
    assert len(facts.pushed) == 2
    assert [row["title"] for row in facts.carried] == ["Older metastability paper"]


def test_collect_counts_a_week_by_its_number_not_by_the_push_date(cfg, paper):
    """周日晚上先把下周的推荐跑出来，这批论文仍然属于「下一周」，不是「上期遗留」。"""
    seed_ledger(cfg, paper, 1, "2026-09-19", count=2)

    facts = summary.collect(cfg, "week", WEEK1)
    assert len(facts.pushed) == 2
    assert facts.carried == []
    assert facts.stats["pushed"] == 2


def test_collect_drops_carried_rows_once_they_are_read(cfg, paper):
    carried = ledger.make_row(
        paper(paperId="s2:old", doi="10.1000/old", title="Older metastability paper"),
        1,
        "2026-09-21",
        "skim",
    )
    ledger.write_ledger(cfg.ledger_path, [carried])
    write_week_note(cfg, 1, "笔记.md", "读了 Older metastability paper，没什么用。")

    facts = summary.collect(cfg, "month", dt.date(2026, 10, 15))
    assert facts.carried == []


def test_collect_reads_the_window_notes_and_history(cfg, paper):
    from tests.conftest import write_report_block

    seed_ledger(cfg, paper, 1, "2026-09-21")
    write_week_note(cfg, 1, "卡点.md", "HMM 的 K 怎么选")
    write_report_block(cfg, 1, titles=["A", "B"])

    facts = summary.collect(cfg, "week", WEEK1)
    assert [note.name for note in facts.notes] == ["卡点.md"]
    assert [block["week"] for block in facts.history] == [1]
    assert "长期目标" in facts.goals


def test_collect_themes_come_from_the_titles(cfg, paper):
    rows = seed_ledger(cfg, paper, 1, "2026-09-21", count=1)
    rows[0]["title"] = "Metastability in resting-state EEG"
    ledger.write_ledger(cfg.ledger_path, rows)
    assert "EEG" in summary.collect(cfg, "week", WEEK1).themes


def test_collect_stats_delegate_to_the_ledger(cfg, paper):
    seed_ledger(cfg, paper, 1, "2026-09-21", count=3, status=ledger.STATUS_READ)
    facts = summary.collect(cfg, "week", WEEK1)
    assert facts.stats == {"pushed": 3, "read": 3, "skipped": 0, "unmarked": 0}


def test_collect_on_an_empty_vault_is_safe(cfg):
    facts = summary.collect(cfg, "month", WEEK1)
    assert facts.pushed == []
    assert facts.carried == []
    assert facts.note_chars == 0


# ---------------------------------------------------------------- 提示与渲染


def test_facts_block_states_the_numbers(cfg, paper):
    seed_ledger(cfg, paper, 1, "2026-09-21", count=2, status=ledger.STATUS_READ)
    block = summary.facts_block(summary.collect(cfg, "week", WEEK1))
    assert "2" in block
    assert "第1周" in block


def test_summary_prompt_forbids_making_things_up(cfg, paper):
    seed_ledger(cfg, paper, 1, "2026-09-21")
    prompt = summary.summary_prompt(summary.collect(cfg, "week", WEEK1), "zh")
    assert "ONLY" in prompt  # 「只能用下面给的数字」这条硬规则必须在
    assert "第1周" in prompt


def test_render_has_a_script_generated_numbers_table(cfg, paper):
    seed_ledger(cfg, paper, 1, "2026-09-21", count=4, status=ledger.STATUS_READ)
    facts = summary.collect(cfg, "week", WEEK1)
    text = summary.render(facts, {"headline": "收尾了"}, "zh")
    assert text.startswith("# 周总结：第1周")
    assert "## 数字（脚本算的，不是 AI 说的）" in text
    assert "| 状态 | 角色 | 周 | 标题 |" in text
    assert "推荐 4 篇：已读 4" in text
    assert "收尾了" in text


def test_render_works_without_any_ai(cfg, paper):
    """没有 AI 也要出文件，只是叙述部分留空。"""
    seed_ledger(cfg, paper, 1, "2026-09-21")
    text = summary.render(summary.collect(cfg, "week", WEEK1), {}, "zh")
    assert text.strip()
    assert "第1周" in text


def test_render_includes_the_artifact_placeholder(cfg, paper):
    seed_ledger(cfg, paper, 1, "2026-09-21")
    text = summary.render(summary.collect(cfg, "week", WEEK1), {}, "zh")
    assert summary.ARTIFACT_PLACEHOLDER in text


def test_render_says_the_script_never_touches_git(cfg, paper):
    seed_ledger(cfg, paper, 1, "2026-09-21")
    assert "git" in summary.render(summary.collect(cfg, "week", WEEK1), {}, "zh")


def test_render_month_title(cfg, paper):
    seed_ledger(cfg, paper, 2, "2026-10-05")
    text = summary.render(summary.collect(cfg, "month", dt.date(2026, 10, 15)), {}, "zh")
    assert text.startswith("# 月度总结：2026-10")


# ---------------------------------------------------------------- run_summary


def test_run_summary_writes_a_month_file(cfg, paper):
    seed_ledger(cfg, paper, 2, "2026-10-05", count=2, status=ledger.STATUS_READ)
    outcome = summary.run_summary(cfg, period="month", anchor=dt.date(2026, 10, 15))
    assert outcome.path == cfg.month_report_dir / "2026-10.md"
    assert outcome.path.exists()
    assert outcome.path.read_text(encoding="utf-8") == outcome.text
    assert outcome.mode == "none"


def test_run_summary_writes_a_week_file(cfg, paper):
    seed_ledger(cfg, paper, 1, "2026-09-21")
    outcome = summary.run_summary(cfg, period="week", anchor=WEEK1)
    assert outcome.path.name == "2026-09-21_第1周总结.md"
    assert outcome.path.parent.name == "周报"


def test_run_summary_dry_run_writes_nothing(cfg, paper):
    seed_ledger(cfg, paper, 1, "2026-09-21")
    outcome = summary.run_summary(cfg, period="week", anchor=WEEK1, dry_run=True)
    assert outcome.path is None
    assert outcome.dry_run
    assert not cfg.week_report_dir.exists()


def test_run_summary_never_needs_a_live_llm(cfg):
    """夹具把 key 清空了，这里必须成功而不是抛异常。"""
    assert summary.run_summary(cfg, period="month", anchor=WEEK1).text


def test_run_summary_uses_the_llm_prose_when_available(cfg, paper):
    class FakeLLM:
        available = True

        def json(self, prompt, temperature=0.3, **kwargs):
            return {
                "headline": "本周只读完一篇",
                "did": ["读完一篇"],
                "learned": ["知道了 metastability"],
                "not_done": ["没碰闭环"],
                "drift": "有点漂",
                "next_focus": "回到 HMM",
                "artifacts": ["repo"],
            }

    seed_ledger(cfg, paper, 1, "2026-09-21", count=1, status=ledger.STATUS_READ)
    outcome = summary.run_summary(cfg, client=FakeLLM(), period="week", anchor=WEEK1, lang="zh")
    assert outcome.mode == "llm"
    assert "本周只读完一篇" in outcome.text
    assert "回到 HMM" in outcome.text


def test_periods_constant():
    assert summary.PERIODS == ("week", "month")
