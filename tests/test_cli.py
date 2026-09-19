"""命令行：只有 init / week / summary 三条命令。

这一组测试全部通过 ``cli.main(argv)`` 走真实入口，所以也顺带覆盖了
参数解析、退出码和「配置文件缺失时要提示跑 init」这些行为。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from paperflow_core import cli, ledger, weekly
from tests.conftest import FakeLLM


def run(argv):
    return cli.main(argv)


def init_argv(vault, state_dir, *extra):
    return ["init", "--vault", str(vault), "--start", "2026-09-21", "--state-dir", str(state_dir), *extra]


# ---------------------------------------------------------------------- init


def test_init_creates_the_whole_layout(vault, state_dir, capsys):
    assert run(init_argv(vault, state_dir)) == 0

    assert (state_dir / "config.json").exists()
    assert (state_dir / "进度.csv").read_text(encoding="utf-8").startswith("week,")
    assert (state_dir / "周报").is_dir()
    assert (state_dir / "月报").is_dir()
    assert (state_dir / "本周.md").exists()
    assert (vault / "2026-09-21_09-27").is_dir()  # 第 1 周的文件夹
    assert (vault / "长期目标" / "长期目标.md").exists()

    out = capsys.readouterr().out
    assert "初始化完成" in out
    assert "长期目标" in out
    assert "--dry-run" in out  # 告诉用户下一步干什么


def test_init_writes_a_readable_config(vault, state_dir):
    run(init_argv(vault, state_dir))
    payload = json.loads((state_dir / "config.json").read_text(encoding="utf-8"))
    assert payload["start_date"] == "2026-09-21"
    assert Path(payload["vault"]) == vault.resolve()
    assert payload["per_week"] == 12
    assert payload["last_run"] == ""


def test_init_is_idempotent_and_never_clobbers_your_files(vault, state_dir):
    run(init_argv(vault, state_dir))
    note = state_dir / "本周.md"
    note.write_text("# 本周\n\n我自己写的，别动它\n", encoding="utf-8")

    config = state_dir / "config.json"
    payload = json.loads(config.read_text(encoding="utf-8"))
    payload["per_week"] = 9
    config.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    assert run(init_argv(vault, state_dir)) == 0
    assert "别动它" in note.read_text(encoding="utf-8")
    assert json.loads(config.read_text(encoding="utf-8"))["per_week"] == 9


def test_init_force_rebuilds_the_config(vault, state_dir):
    run(init_argv(vault, state_dir))
    assert run(init_argv(vault, state_dir, "--force", "--per-week", "4")) == 0
    assert json.loads((state_dir / "config.json").read_text(encoding="utf-8"))["per_week"] == 4


def test_init_does_not_touch_an_existing_goals_folder(vault, state_dir):
    goals = vault / "长期目标"
    goals.mkdir()
    (goals / "长期目标.md").write_text("# 我的目标\n\n1. 复现一个 HMM 分割流程\n", encoding="utf-8")

    run(init_argv(vault, state_dir))
    assert "复现一个 HMM" in (goals / "长期目标.md").read_text(encoding="utf-8")


def test_init_accepts_an_explicit_goals_file(vault, state_dir, tmp_path):
    target = tmp_path / "目标.md"
    target.write_text("做完闭环刺激的最小方案", encoding="utf-8")
    run(init_argv(vault, state_dir, "--goals", str(target)))
    payload = json.loads((state_dir / "config.json").read_text(encoding="utf-8"))
    assert payload["goals"] == str(target)
    assert not (vault / "长期目标").exists()


def test_init_without_a_vault_asks_for_one(state_dir, capsys):
    """没有 --vault 也没设 PAPERFLOW_VAULT 时，要说清楚该干什么。"""
    assert run(["init", "--state-dir", str(state_dir)]) == 2
    assert "--vault" in capsys.readouterr().err


def test_init_reads_the_vault_from_the_environment(state_dir, vault, monkeypatch, capsys):
    monkeypatch.setenv("PAPERFLOW_VAULT", str(vault))
    assert run(["init", "--state-dir", str(state_dir), "--start", "2026-09-21"]) == 0
    assert (vault / "2026-09-21_09-27").is_dir()
    assert capsys.readouterr().out


def test_init_rejects_a_bad_date(vault, state_dir):
    with pytest.raises(SystemExit):
        run(init_argv(vault, state_dir, "--start", "21/09/2026"))


def test_a_bad_date_exits_2_like_any_other_typo(tmp_path, capsys):
    """日期写错是参数问题，退出码必须是 2，不能和「模型不可用」的 1 混在一起。

    也没有 init 过：参数错误要先报出来，不能被「你还没初始化」盖过去。
    """
    for argv in (
        ["week", "--state-dir", str(tmp_path / "空的"), "--date", "2026-13-45"],
        ["summary", "--state-dir", str(tmp_path / "空的"), "--date", "2026-13-45"],
    ):
        with pytest.raises(SystemExit) as excinfo:
            run(argv)
        assert excinfo.value.code == 2
        assert "YYYY-MM-DD" in capsys.readouterr().err


def test_init_defaults_to_the_next_monday(vault, state_dir):
    """今天是周日就让第 1 周从明天（周一）开始——用户不用手算日期。"""
    run(["init", "--vault", str(vault), "--state-dir", str(state_dir)])
    payload = json.loads((state_dir / "config.json").read_text(encoding="utf-8"))
    start = dt.date.fromisoformat(payload["start_date"])
    assert start.weekday() == 0
    assert start >= dt.date.today()


def test_init_warns_when_the_goals_file_is_still_a_placeholder(vault, state_dir, capsys):
    run(init_argv(vault, state_dir))
    out = capsys.readouterr().out
    assert "把长期目标写清楚" in out


def test_init_in_english(vault, state_dir, capsys):
    assert run(init_argv(vault, state_dir, "--lang", "en")) == 0
    assert "Initialised" in capsys.readouterr().out


# ---------------------------------------------------------------------- week


def test_week_without_a_config_tells_you_to_run_init(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        run(["week", "--state-dir", str(tmp_path / "空的")])
    assert excinfo.value.code == 1
    assert "init" in capsys.readouterr().err


def test_week_dry_run_writes_nothing(vault, state_dir, paper, monkeypatch, capsys):
    run(init_argv(vault, state_dir))
    monkeypatch.setattr(cli, "get_client", lambda: FakeLLM())
    monkeypatch.setattr(weekly, "build_candidates", lambda *a, **k: [])

    assert run(["week", "--dry-run", "--state-dir", str(state_dir), "--date", "2026-09-24"]) == 0

    out = capsys.readouterr().out
    assert "第1周" in out
    assert "试跑" in out
    assert not (state_dir / "周报").exists() or not list((state_dir / "周报").iterdir())
    assert not (state_dir / "进度.csv").exists() or not ledger.read_ledger(state_dir / "进度.csv")
    assert not (vault / "2026-09-21_09-27" / "阅读清单.md").exists()


def test_week_end_to_end(vault, state_dir, paper, monkeypatch, capsys):
    """有模型、不推 Zotero：写出清单 + 周报 + 账本，并说清是谁挑的。"""
    run(init_argv(vault, state_dir))
    pool = []
    for index in range(4):
        item = paper(paperId=f"s2:{index}", doi=f"10.1000/{index}", title=f"Brain state study {index}")
        item["heuristic_score"] = 10 - index
        pool.append(item)
    monkeypatch.setattr(cli, "get_client", lambda: FakeLLM())
    monkeypatch.setattr(weekly, "build_candidates", lambda *a, **k: pool)

    assert run(["week", "--no-zotero", "--state-dir", str(state_dir), "--date", "2026-09-24"]) == 0

    assert (state_dir / "周报" / "2026-09-21_第1周.md").exists()
    assert (vault / "2026-09-21_09-27" / "阅读清单.md").exists()
    assert len(ledger.read_ledger(state_dir / "进度.csv")) == 4

    out = capsys.readouterr().out
    assert "Brain state study 0" in out
    assert "周报" in out
    assert "AI 筛选" in out
    assert "引用门槛" in out


def test_week_without_a_model_exits_1_and_writes_nothing(vault, state_dir, paper, monkeypatch, capsys):
    """没配模型：报错退出（1），而且不写清单、不写周报、不记账本、不推 Zotero。"""
    run(init_argv(vault, state_dir))
    item = paper(doi="10.1000/only")
    item["heuristic_score"] = 5
    monkeypatch.setattr(weekly, "build_candidates", lambda *a, **k: [item])

    assert run(["week", "--state-dir", str(state_dir), "--date", "2026-09-24"]) == 1

    err = capsys.readouterr().err
    assert "没有可用的 AI" in err
    assert not (state_dir / "周报").exists() or not list((state_dir / "周报").iterdir())
    assert not (state_dir / "进度.csv").exists() or not ledger.read_ledger(state_dir / "进度.csv")
    assert not (vault / "2026-09-21_09-27" / "阅读清单.md").exists()


def test_week_survives_a_search_failure(vault, state_dir, monkeypatch, capsys):
    run(init_argv(vault, state_dir))

    def boom(*args, **kwargs):
        raise ConnectionResetError("connection reset by peer")

    monkeypatch.setattr(weekly, "build_candidates", boom)
    assert run(["week", "--no-zotero", "--state-dir", str(state_dir), "--date", "2026-09-24"]) == 0
    assert "检索失败" in capsys.readouterr().out


def test_week_says_zotero_was_skipped_on_request(vault, state_dir, paper, monkeypatch, capsys):
    run(init_argv(vault, state_dir))
    item = paper(doi="10.1000/only")
    item["heuristic_score"] = 5
    monkeypatch.setattr(cli, "get_client", lambda: FakeLLM())
    monkeypatch.setattr(weekly, "build_candidates", lambda *a, **k: [item])

    run(["week", "--no-zotero", "--state-dir", str(state_dir), "--date", "2026-09-24"])
    assert "跳过" in capsys.readouterr().out


def test_week_flags_reach_the_orchestrator(vault, state_dir, monkeypatch):
    run(init_argv(vault, state_dir))
    seen = {}

    def fake(cfg, **kwargs):
        seen.update(kwargs)
        return weekly.WeekOutcome(week=1, window="a..b", query="q", mode=weekly.MODE_NONE, papers=[])

    monkeypatch.setattr(weekly, "run_week", fake)
    run(
        [
            "week",
            "--full",
            "--per-week",
            "3",
            "--deep",
            "1",
            "--topic",
            "eeg|fmri",
            "--min-year",
            "2018",
            "--min-citations",
            "80",
            "--years",
            "6",
            "--max-years",
            "12",
            "--no-zotero",
            "--date",
            "2026-09-24",
            "--state-dir",
            str(state_dir),
        ]
    )
    assert seen["full"] is True
    assert seen["per_week"] == 3
    assert seen["deep_slots"] == 1
    assert seen["query"] == "eeg|fmri"
    assert seen["min_year"] == 2018
    assert seen["zotero_on"] is False
    assert seen["today"] == dt.date(2026, 9, 24)
    assert seen["min_citations"] == 80
    assert seen["years"] == 6
    assert seen["max_years"] == 12


def test_week_always_passes_a_client(vault, state_dir, monkeypatch):
    """周循环不提供「不用 AI」的开关：命令行必须把客户端传下去。"""
    run(init_argv(vault, state_dir))
    sentinel = object()
    monkeypatch.setattr(cli, "get_client", lambda: sentinel)
    seen = {}

    def fake(cfg, **kwargs):
        seen.update(kwargs)
        return weekly.WeekOutcome(week=1, window="a..b", query="q", mode=weekly.MODE_NONE, papers=[])

    monkeypatch.setattr(weekly, "run_week", fake)
    run(["week", "--state-dir", str(state_dir)])
    assert seen["client"] is sentinel

    parser = cli.make_parser()
    with pytest.raises(SystemExit):  # --no-llm 已经不存在了
        parser.parse_args(["week", "--no-llm"])


# ------------------------------------------------------------------- summary


def test_summary_dry_run_prints_and_writes_nothing(vault, state_dir, capsys):
    run(init_argv(vault, state_dir))
    assert run(["summary", "--period", "week", "--date", "2026-09-24", "--state-dir", str(state_dir)]) == 0
    out = capsys.readouterr().out
    assert "周总结" in out
    assert "试跑" in out


def test_summary_writes_a_month_report(vault, state_dir, capsys):
    run(init_argv(vault, state_dir))
    assert run(["summary", "--period", "month", "--date", "2026-10-15", "--state-dir", str(state_dir)]) == 0
    path = state_dir / "月报" / "2026-10.md"
    assert path.exists()
    assert "2026-10.md" in capsys.readouterr().out


def test_summary_week_defaults_are_documented_in_help(capsys):
    with pytest.raises(SystemExit):
        run(["summary", "--help"])
    assert "--period" in capsys.readouterr().out


# --------------------------------------------------------------- 通用行为


def test_unknown_command_exits_with_2():
    with pytest.raises(SystemExit) as excinfo:
        run(["recommend", "--topic", "x"])
    assert excinfo.value.code == 2


def test_no_command_exits_with_2():
    with pytest.raises(SystemExit) as excinfo:
        run([])
    assert excinfo.value.code == 2


def test_help_lists_exactly_three_commands(capsys):
    with pytest.raises(SystemExit):
        run(["--help"])
    out = capsys.readouterr().out
    assert "init" in out and "week" in out and "summary" in out
    for gone in ("recommend", "mentor", "roadmap", "graph", "card", "fieldmap", "metrics"):
        assert gone not in out


def test_state_dir_can_be_passed_before_the_subcommand(vault, state_dir):
    assert run(["--state-dir", str(state_dir), "init", "--vault", str(vault), "--start", "2026-09-21"]) == 0
    assert (state_dir / "config.json").exists()


def test_state_dir_accepts_an_environment_default(vault, state_dir, monkeypatch):
    monkeypatch.setenv("PAPERFLOW_STATE_DIR", str(state_dir))
    assert run(["init", "--vault", str(vault), "--start", "2026-09-21"]) == 0
    assert (state_dir / "config.json").exists()


def test_language_can_be_passed_before_the_subcommand(vault, state_dir, capsys):
    run(
        [
            "--lang",
            "en",
            "init",
            "--vault",
            str(vault),
            "--start",
            "2026-09-21",
            "--state-dir",
            str(state_dir),
        ]
    )
    assert "Initialised" in capsys.readouterr().out


def test_llm_error_exits_with_1(vault, state_dir, monkeypatch, capsys):
    """LLM 挂了退出码是 1（业务失败），不是崩溃。"""
    from paperflow_core.llm import LLMError

    run(init_argv(vault, state_dir))

    def boom(cfg, **kwargs):
        raise LLMError("boom")

    monkeypatch.setattr(weekly, "run_week", boom)
    assert run(["week", "--state-dir", str(state_dir)]) == 1
    assert "LLM 调用失败" in capsys.readouterr().err


def test_cli_never_imports_the_deleted_modules():
    """回归保护：这三条命令不该再把旧模块拖回来。"""
    import paperflow_core.cli as cli_mod

    source = open(cli_mod.__file__, encoding="utf-8").read()
    for gone in ("cards", "consolidate", "feedback", "fieldmap", "graph", "roadmap", "report", "state"):
        assert f"from .{gone}" not in source
        assert f"from . import {gone}" not in source
