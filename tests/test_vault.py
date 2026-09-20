"""库布局与周编号：整套东西的坐标系统。"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from paperflow_core import vault as vault_mod
from paperflow_core.vault import VaultConfig, week_label


def test_week_label_has_no_zero_padding():
    assert week_label(1) == "第1周"
    assert week_label(12) == "第12周"


def test_week_of_counts_from_start_date(cfg):
    assert cfg.week_of(dt.date(2026, 9, 20)) == 0  # 开始前一天
    assert cfg.week_of(dt.date(2026, 9, 21)) == 1
    assert cfg.week_of(dt.date(2026, 9, 27)) == 1  # 第 1 周的最后一天
    assert cfg.week_of(dt.date(2026, 9, 28)) == 2


def test_current_week_never_returns_zero(cfg):
    """开始日之前跑，要当成在准备第 1 周，而不是第 0 周。"""
    assert cfg.current_week(dt.date(2026, 9, 1)) == 1
    assert cfg.current_week(dt.date(2026, 9, 21)) == 1
    assert cfg.current_week(dt.date(2026, 10, 5)) == 3


def test_folder_name_sorts_chronologically():
    """文件夹名前缀是完整日期，所以字符串排序 == 时间排序，跨年也不会乱。"""
    cfg = VaultConfig(vault=Path("."), goals=Path("."), start_date=dt.date(2026, 12, 28))
    assert cfg.folder_name(1) == "2026-12-28_01-03"
    assert cfg.folder_name(2) == "2027-01-04_01-10"
    assert cfg.folder_name(1) < cfg.folder_name(2)


def test_week_bounds_are_seven_days(cfg):
    start, end = cfg.week_bounds(3)
    assert start == dt.date(2026, 10, 5)
    assert end == dt.date(2026, 10, 11)
    assert (end - start).days == 6


def test_week_dir_uses_folder_name(cfg):
    assert cfg.week_dir(1).name == "2026-09-21_09-27"
    assert cfg.ensure_week_dir(1).is_dir()
    assert cfg.week_dir(1).is_dir()


def test_report_path_is_sortable(cfg):
    path = cfg.report_path(1)
    assert path.name == "2026-09-21_第1周.md"
    assert path.parent.name == "周报月报"
    # 报告写在库里（Obsidian 能直接点开），不在 state dir。
    assert path.parent.parent == cfg.vault
    assert cfg.month_report_dir == cfg.report_path(1).parent
    assert cfg.week_note_path == cfg.vault / "本周.md"


def test_reports_and_the_week_note_live_in_the_vault_root(cfg):
    """报告和 ``本周.md`` 是给人看的，必须落在库里；机器用的才放 state dir。"""
    assert cfg.reports_dir == cfg.vault / "周报月报"
    assert cfg.week_report_dir == cfg.reports_dir
    assert cfg.month_report_dir == cfg.reports_dir
    assert cfg.week_note_path == cfg.vault / "本周.md"
    assert cfg.ledger_path.parent == cfg.state_dir
    assert cfg.reports_dir.parent == cfg.vault


def test_reports_in_the_vault_are_not_read_back_as_notes_or_goals(cfg):
    """报告、``本周.md`` 都是脚本自己写的，绝不能被当成这周的笔记或长期目标喂回去。

    搬到库里之后它们离笔记住得很近，这个边界得用测试守着：
    一旦被当成「笔记」读回去，AI 就会把自己上周写的话当成你的想法。
    """
    cfg.reports_dir.mkdir(parents=True)
    (cfg.reports_dir / "2026-09-21_第1周.md").write_text("# 周报\n\n正文\n", encoding="utf-8")
    (cfg.reports_dir / "2026-09.md").write_text("# 月度总结\n", encoding="utf-8")
    cfg.week_note_path.write_text("# 本周\n\n我自己写的卡点\n", encoding="utf-8")

    folder = cfg.ensure_week_dir(1)
    (folder / "我的笔记.md").write_text("真笔记", encoding="utf-8")

    assert [path.name for path in cfg.notes_in(1)] == ["我的笔记.md"]
    goals = cfg.read_goals()
    assert "周报" not in goals
    assert "月度总结" not in goals
    assert "卡点" not in goals


def test_notes_weeks_full_vs_since_last_run(cfg):
    cfg.last_run = None
    assert cfg.notes_weeks(dt.date(2026, 10, 5), full=False) == [1, 2, 3]

    cfg.last_run = dt.date(2026, 9, 28)  # 第 2 周
    assert cfg.notes_weeks(dt.date(2026, 10, 5), full=False) == [2, 3]
    assert cfg.notes_weeks(dt.date(2026, 10, 5), full=True) == [1, 2, 3]


def test_notes_weeks_never_drops_a_skipped_week(cfg):
    """跳了一周没跑，下一次要把漏掉的那一周的笔记也读进来。"""
    cfg.last_run = dt.date(2026, 9, 21)  # 第 1 周
    assert cfg.notes_weeks(dt.date(2026, 10, 12), full=False) == [1, 2, 3, 4]


def test_notes_in_excludes_the_generated_queue(cfg):
    folder = cfg.ensure_week_dir(1)
    (folder / "我的笔记.md").write_text("读了 metastability 那篇", encoding="utf-8")
    (folder / "阅读清单.md").write_text("脚本写的，不算笔记", encoding="utf-8")
    names = [path.name for path in cfg.notes_in(1)]
    assert names == ["我的笔记.md"]


def test_goals_can_be_a_file_or_a_folder(cfg, vault):
    assert cfg.read_goals().startswith("### 长期目标.md")

    single = vault / "单文件目标.md"
    single.write_text("只想做闭环刺激", encoding="utf-8")
    cfg.goals = single
    assert "只想做闭环刺激" in cfg.read_goals()


def test_read_goals_ignores_missing_goals(cfg, vault):
    cfg.goals = vault / "不存在"
    assert cfg.read_goals() == ""


def test_config_round_trip(cfg, state_dir):
    vault_mod.save_config(state_dir, cfg)
    loaded = vault_mod.load_config(state_dir)
    assert loaded is not None
    assert loaded.vault == cfg.vault
    assert loaded.goals == cfg.goals
    assert loaded.start_date == cfg.start_date
    assert loaded.per_week == cfg.per_week
    assert loaded.state_dir == state_dir


def test_relative_goals_resolve_against_vault(vault, state_dir):
    """config.json 里的相对目标路径要按 vault 解析，否则换台机器就读不到了。"""
    (state_dir / "config.json").write_text(
        json.dumps({"vault": str(vault), "goals": "长期目标", "start_date": "2026-09-21"}, ensure_ascii=False),
        encoding="utf-8",
    )
    loaded = vault_mod.load_config(state_dir)
    assert loaded is not None
    assert loaded.goals.is_absolute()
    assert loaded.goals == vault / "长期目标"


def test_load_config_returns_none_on_garbage(state_dir):
    assert vault_mod.load_config(state_dir) is None
    (state_dir / "config.json").write_text("{ this is not json", encoding="utf-8")
    assert vault_mod.load_config(state_dir) is None
    (state_dir / "config.json").write_text('{"start_date": "2026-09-21"}', encoding="utf-8")
    assert vault_mod.load_config(state_dir) is None  # 缺 vault


def test_mark_run_records_the_date(cfg, state_dir):
    vault_mod.save_config(state_dir, cfg)
    vault_mod.mark_run(state_dir, cfg, dt.date(2026, 9, 24))
    assert vault_mod.load_config(state_dir).last_run == dt.date(2026, 9, 24)
