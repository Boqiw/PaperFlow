"""每周循环：全局回顾 → 检索筛选 → 落盘。

两条底线：

* **清单只能来自 AI。** 没有可用的模型就必须报错退出，而不是自己排个序充数；
* **失败不许把已经有的东西写没。** 空跑不能覆盖当周的正本。
"""

from __future__ import annotations

import datetime as dt

import pytest

from paperflow_core import ledger, weekly
from paperflow_core.llm import LLMError
from tests.conftest import FakeLLM, write_report_block, write_week_note

WEEK1 = dt.date(2026, 9, 24)
WEEK2 = dt.date(2026, 9, 30)


def candidates(paper, count=4):
    """一组互不相同的候选论文（DOI / paperId / 标题都不同，就像真实检索）。"""
    made = []
    for index in range(count):
        item = paper(
            paperId=f"s2:{index}",
            doi=f"10.1000/{index}",
            title=f"Brain state dynamics study {index}",
        )
        item["heuristic_score"] = 10 - index
        made.append(item)
    return made


def patch_search(monkeypatch, papers, calls=None):
    """把检索换掉。签名跟着 build_candidates 走（包括引用门槛与年份窗口）。"""

    def fake(
        topic, seeds, min_year, max_candidates, context, sources=None, min_citations=None, **extra
    ):
        if calls is not None:
            calls.append(
                {
                    "topic": topic,
                    "context": context,
                    "min_citations": min_citations,
                    "max_candidates": max_candidates,
                    "years": extra.get("years"),
                    "max_years": extra.get("max_years"),
                }
            )
        return list(papers)

    monkeypatch.setattr(weekly, "build_candidates", fake)


def review_of(papers, mode=weekly.MODE_LLM, **extra):
    """一份 review 字典。渲染类用例不关心它怎么来的，所以直接拼。"""
    base = {
        "notes_digest": [],
        "papers": list(papers),
        "convergence": "收敛",
        "uncovered": "闭环调控",
        "open_questions": [],
        "engineering": {},
        "mode": mode,
        "warnings": [],
    }
    base.update(extra)
    return base


# --------------------------------------------------------------- build_context


def test_build_context_reads_goals_and_note(cfg):
    write_week_note(cfg, 1, "卡点.md", "metastability 估计不出来")
    cfg.week_note_path.write_text("# 本周\n\n我想先搞清 HMM\n", encoding="utf-8")

    context = weekly.build_context(cfg, 1, WEEK1)
    assert "metastability" in context.goals
    assert "HMM" in context.week_note  # 去掉了标题行
    assert context.corpus.weeks == [1]
    assert context.window == "2026-09-21..2026-09-27"
    assert context.prev_week == 0


def test_build_context_warns_when_goals_are_missing(cfg, vault):
    cfg.goals = vault / "不存在"
    context = weekly.build_context(cfg, 1, WEEK1)
    assert any("长期目标" in item for item in context.warnings)


def test_build_context_builds_a_query_from_the_goals(cfg):
    context = weekly.build_context(cfg, 1, WEEK1)
    assert context.query
    assert "|" in context.query or context.query in weekly.terms.FALLBACK_QUERIES


def test_build_context_keeps_an_explicit_query(cfg):
    assert weekly.build_context(cfg, 1, WEEK1, query="my query").query == "my query"


def test_build_context_falls_back_to_notes_when_goals_are_vague(cfg, vault):
    (vault / "长期目标" / "长期目标.md").write_text("我想变强", encoding="utf-8")
    write_week_note(cfg, 1, "笔记.md", "这篇讲 kuramoto 模型的 metastability")
    context = weekly.build_context(cfg, 1, WEEK1)
    assert "metastability" in context.query or "kuramoto" in context.query


def test_build_context_reads_the_global_history(cfg):
    write_report_block(cfg, 1, titles=["第一周推的"])
    context = weekly.build_context(cfg, 2, WEEK2)
    assert [block["week"] for block in context.history] == [1]


def test_build_context_refreshes_the_ledger_from_notes(cfg, paper):
    """笔记里写了标题，就应该在账本里被翻成「已读」。"""
    write_week_note(cfg, 1, "笔记.md", "今天读了 Metastability in resting-state brain dynamics。")
    ledger.write_ledger(cfg.ledger_path, [ledger.make_row(paper(), 1, "2026-09-21", "deep")])

    context = weekly.build_context(cfg, 1, WEEK1)
    assert context.rows[0]["status"] == ledger.STATUS_READ
    assert context.stats_by_week[1]["read"] == 1


def test_build_context_full_reads_every_week(cfg):
    write_week_note(cfg, 1, "一.md", "第一周")
    write_week_note(cfg, 2, "二.md", "第二周")
    cfg.last_run = dt.date(2026, 9, 28)
    assert weekly.build_context(cfg, 2, WEEK2).corpus.weeks == [2]
    assert weekly.build_context(cfg, 2, WEEK2, full=True).corpus.weeks == [1, 2]


# ------------------------------------------------------------------ selection


def test_select_without_a_client_refuses_to_invent_a_list(cfg, paper):
    """没配模型就不许出清单。

    宁可报错退出、下次重跑同一周，也不给一份「按分数排个序」的假推荐——
    那种东西会写进账本、推给 Zotero，比什么都不做更糟。
    """
    context = weekly.build_context(cfg, 1, WEEK1)
    with pytest.raises(LLMError):
        weekly.select(None, context, candidates(paper), per_week=3, deep_slots=2)


def test_select_refuses_when_the_model_is_unavailable(cfg, paper):
    class Dead(FakeLLM):
        available = False

    context = weekly.build_context(cfg, 1, WEEK1)
    with pytest.raises(LLMError):
        weekly.select(Dead(), context, candidates(paper), per_week=3, deep_slots=2)


def test_select_caps_deep_papers(cfg, paper):
    context = weekly.build_context(cfg, 1, WEEK1)
    review = weekly.select(FakeLLM(picks=5), context, candidates(paper, 5), per_week=5, deep_slots=1)
    assert sum(1 for item in review["papers"] if item["role"] == weekly.ROLE_DEEP) == 1


def test_select_trims_to_the_week_quota(cfg, paper):
    context = weekly.build_context(cfg, 1, WEEK1)
    review = weekly.select(FakeLLM(picks=9), context, candidates(paper, 9), per_week=3, deep_slots=1)
    assert len(review["papers"]) == 3


def test_select_uses_the_llm_when_available(cfg, paper):
    context = weekly.build_context(cfg, 1, WEEK1)
    pool = candidates(paper)
    client = FakeLLM(
        {
            "papers": [
                {"paperId": "s2:0", "role": "deep", "track": "target", "why": "命中第 1 周目标", "gain": "能讲清"},
                {"paperId": "s2:1", "role": "skim", "track": "explore", "why": "发散"},
            ],
            "convergence": "收敛到 HMM",
            "uncovered": "闭环刺激",
            "open_questions": ["怎么选 K"],
            "notes_digest": ["你卡在 HMM"],
        }
    )
    review = weekly.select(client, context, pool, per_week=6, deep_slots=3)
    assert review["mode"] == "llm"
    assert [item["paperId"] for item in review["papers"]] == ["s2:0", "s2:1"]
    assert review["papers"][0]["title"] == "Brain state dynamics study 0"  # 标题由脚本回填
    assert review["uncovered"] == "闭环刺激"
    assert client.prompts and "s2:0" in client.prompts[0]


def test_select_drops_hallucinated_paper_ids_and_warns(cfg, paper):
    """AI 不许编造文献：不在候选表里的 paperId 一律丢掉并报警告。"""
    context = weekly.build_context(cfg, 1, WEEK1)
    client = FakeLLM({"papers": [{"paperId": "s2:0"}, {"paperId": "编造的"}]})
    review = weekly.select(client, context, candidates(paper), per_week=6, deep_slots=3)
    assert [item["paperId"] for item in review["papers"]] == ["s2:0"]
    assert any("不在候选表里" in item for item in review["warnings"])


def test_select_raises_when_every_pick_is_hallucinated(cfg, paper):
    """全部都是编造的 paperId 时不能「退到启发式」，只能报错。"""
    context = weekly.build_context(cfg, 1, WEEK1)
    client = FakeLLM({"papers": [{"paperId": "编造的"}]})
    with pytest.raises(LLMError) as excinfo:
        weekly.select(client, context, candidates(paper), per_week=2, deep_slots=1)
    assert "编造" in str(excinfo.value)


def test_select_raises_on_a_malformed_payload(cfg, paper):
    context = weekly.build_context(cfg, 1, WEEK1)
    client = FakeLLM({"nonsense": True})
    with pytest.raises(LLMError):
        weekly.select(client, context, candidates(paper), per_week=2, deep_slots=1)
    assert len(client.prompts) == 2  # 换了个温度又试了一次，然后才放弃


def test_select_raises_when_the_model_returns_nothing(cfg, paper):
    context = weekly.build_context(cfg, 1, WEEK1)
    client = FakeLLM({})
    with pytest.raises(LLMError):
        weekly.select(client, context, candidates(paper), per_week=2, deep_slots=1)


def test_select_with_no_candidates_does_not_call_the_llm(cfg, paper):
    context = weekly.build_context(cfg, 1, WEEK1)
    client = FakeLLM({"papers": [{"paperId": "s2:0"}]})
    review = weekly.select(client, context, [], per_week=6, deep_slots=3)
    assert review["papers"] == []
    assert client.prompts == []


def test_trim_demotes_excess_deep_papers_instead_of_dropping_them(cfg, paper):
    pool = [
        dict(item, role=weekly.ROLE_DEEP if index < 3 else weekly.ROLE_SKIM)
        for index, item in enumerate(candidates(paper, 4))
    ]
    trimmed = weekly._trim(pool, per_week=4, deep_slots=1)
    assert [item["role"] for item in trimmed] == [
        weekly.ROLE_DEEP,
        weekly.ROLE_SKIM,
        weekly.ROLE_SKIM,
        weekly.ROLE_SKIM,
    ]


def test_trim_dedupes_and_truncates(cfg, paper):
    pool = [dict(item, role=weekly.ROLE_SKIM) for item in candidates(paper, 5)]
    pool.append(dict(pool[0]))
    trimmed = weekly._trim(pool, per_week=2, deep_slots=1)
    assert len(trimmed) == 2


def test_trim_keeps_everything_when_the_limit_is_generous(cfg, paper):
    pool = [dict(item, role=weekly.ROLE_SKIM) for item in candidates(paper, 3)]
    assert len(weekly._trim(pool, per_week=6, deep_slots=3)) == 3


# --------------------------------------------------------------------- 渲染


def with_roles(pool, deep=1):
    """给候选标上 role：前 ``deep`` 篇精读，其余泛读。"""
    return [
        dict(item, role=weekly.ROLE_DEEP if index < deep else weekly.ROLE_SKIM)
        for index, item in enumerate(pool)
    ]


def test_render_queue_names_the_week_and_the_zotero_tag(cfg, paper):
    context = weekly.build_context(cfg, 1, WEEK1)
    papers = with_roles(candidates(paper, 3))
    text = weekly.render_queue(context, papers, review_of(papers), "zh")
    assert "# 第1周 阅读清单" in text
    assert "`第1周`" in text
    assert "精读" in text and "泛读" in text
    assert all(item["title"] in text for item in papers)


def test_render_queue_says_who_screened_the_papers(cfg, paper):
    """清单上要写清楚「这是 AI 挑的」——不再有「没有 AI，纯启发式排序」那套警告。"""
    context = weekly.build_context(cfg, 1, WEEK1)
    papers = with_roles(candidates(paper, 1))
    text = weekly.render_queue(context, papers, review_of(papers), "zh")
    assert "AI 筛选" in text
    assert "启发式" not in text


def test_render_queue_handles_an_empty_week(cfg):
    context = weekly.build_context(cfg, 1, WEEK1)
    text = weekly.render_queue(context, [], review_of([], mode=weekly.MODE_NONE), "zh")
    assert "没有新论文" in text


def test_render_report_embeds_a_machine_readable_block(cfg, paper):
    from paperflow_core import notes as notes_mod

    context = weekly.build_context(cfg, 1, WEEK1)
    papers = with_roles(candidates(paper, 2))
    text, block = weekly.render_report(context, papers, review_of(papers), {}, "zh")
    assert block["week"] == 1
    assert len(block["titles"]) == 2
    assert notes_mod.parse_pf_blocks(text) == [block]
    assert "本周工程建议" in text


def test_render_report_states_the_mode_and_the_citation_floor(cfg, paper):
    context = weekly.build_context(cfg, 1, WEEK1)
    papers = with_roles(candidates(paper, 1))
    text, _ = weekly.render_report(context, papers, review_of(papers), {}, "zh", citation_base=50)
    assert "AI 筛选" in text
    assert "引用门槛" in text and "≥50" in text
    assert "离线" not in text


def test_render_report_says_the_script_never_touches_git(cfg):
    context = weekly.build_context(cfg, 1, WEEK1)
    text, _ = weekly.render_report(context, [], review_of([], mode=weekly.MODE_NONE), {}, "zh")
    assert "git" in text


# ------------------------------------------------------------------ 年份窗口


def test_year_policy_says_five_years_then_ten():
    assert weekly.year_policy(5, 10, "zh", WEEK1) == "优先 2021 年及以后；凑不满时最多放宽到 2016 年及以后"
    assert weekly.year_policy(0, 10, "zh", WEEK1) == "不限年份（--years 0）"
    assert weekly.year_policy(5, 5, "zh", WEEK1) == "只收 2021 年及以后"


def test_year_window_note_reports_the_real_composition():
    pool = [
        {"paperId": "a", "year": 2025},
        {"paperId": "b", "year": 2020},
        {"paperId": "c", "year": 2019},
        {"paperId": "d", "year": None},
    ]
    note = weekly.year_window_note(pool, 5, 10, "zh", WEEK1)
    assert note.startswith("优先 2021 年及以后")
    assert "候选 2019–2025" in note
    assert "近 5 年内 1 篇" in note
    assert "放宽补入 2 篇" in note
    assert "1 篇年份未知" in note


def test_year_window_note_handles_an_empty_pool():
    note = weekly.year_window_note([], 5, 10, "zh", WEEK1)
    assert "优先 2021 年及以后" in note
    assert "没有候选" in note


def test_render_queue_and_report_carry_the_year_window(cfg, paper):
    context = weekly.build_context(cfg, 1, WEEK1)
    papers = with_roles(candidates(paper, 1))
    note = weekly.year_window_note(papers, 5, 10, "zh", WEEK1)
    queue = weekly.render_queue(context, papers, review_of(papers), "zh", note)
    assert "- 年份窗口：" in queue
    report, _ = weekly.render_report(
        context, papers, review_of(papers), {"year_window": note}, "zh"
    )
    assert "- 年份窗口：" in report


def test_render_queue_without_a_window_note_still_renders(cfg, paper):
    """渲染函数不能因为少了个可选参数就崩（空跑、旧调用点都会这么调）。"""
    context = weekly.build_context(cfg, 1, WEEK1)
    papers = with_roles(candidates(paper, 1))
    text = weekly.render_queue(context, papers, review_of(papers), "zh")
    assert "年份窗口" not in text


# ----------------------------------------------------------------- run_week


def test_run_week_dry_run_writes_nothing(cfg, paper, monkeypatch, llm):
    patch_search(monkeypatch, candidates(paper))
    outcome = weekly.run_week(cfg, client=llm, dry_run=True, today=WEEK1)

    assert outcome.dry_run
    assert outcome.papers
    assert outcome.queue_path is None
    assert outcome.report_path is None
    assert not cfg.week_report_dir.exists()
    assert not cfg.ledger_path.exists()
    assert not cfg.week_dir(1).exists()


def test_run_week_writes_the_queue_report_and_ledger(cfg, paper, monkeypatch, llm):
    patch_search(monkeypatch, candidates(paper))
    outcome = weekly.run_week(cfg, client=llm, zotero_on=False, today=WEEK1)

    assert outcome.queue_path and outcome.queue_path.exists()
    assert outcome.report_path and outcome.report_path.name == "2026-09-21_第1周.md"
    assert outcome.report_path.parent.name == "周报月报"
    assert outcome.report_path.parent.parent == cfg.vault
    assert cfg.week_dir(1).name == "2026-09-21_09-27"
    assert len(ledger.read_ledger(cfg.ledger_path)) == len(outcome.papers)
    assert outcome.zotero["ok"] is True


def test_the_queue_link_to_the_report_really_resolves(cfg, paper, monkeypatch, llm):
    """清单在 ``<库>/<周文件夹>/``，报告在 ``<库>/周报月报/``，所以链接必须以 ``../`` 开头。

    以前报告藏在 state dir 里，这个链接是死的；搬到库里之后它必须真能点开，
    否则用户在 Obsidian 里看到的就是一个点不动的灰色文字。
    """
    patch_search(monkeypatch, candidates(paper))
    outcome = weekly.run_week(cfg, client=llm, zotero_on=False, today=WEEK1)

    relative = f"../周报月报/{outcome.report_path.name}"
    assert relative in outcome.queue_path.read_text(encoding="utf-8")
    assert (outcome.queue_path.parent / relative).resolve() == outcome.report_path.resolve()


def test_run_week_records_the_run_date(cfg, paper, monkeypatch, llm):
    """记下运行日期，下一次才知道该从哪一周开始读笔记。"""
    from paperflow_core import vault as vault_mod

    patch_search(monkeypatch, candidates(paper))
    weekly.run_week(cfg, client=llm, zotero_on=False, today=WEEK1)
    assert vault_mod.load_config(cfg.state_dir).last_run == WEEK1


def test_run_week_without_an_llm_raises_and_writes_nothing(cfg, paper, monkeypatch):
    """没配模型时：报错退出，而且一个文件、一行账本都不能写。

    这样同一周下次会被重跑；如果拿一份启发式清单充数，账本会把这些论文
    永久标成「已推」，真正的 AI 版本反而再也选不到它们了。
    """
    patch_search(monkeypatch, candidates(paper))
    with pytest.raises(LLMError):
        weekly.run_week(cfg, client=None, zotero_on=False, today=WEEK1)

    assert not cfg.ledger_path.exists()
    assert not cfg.week_dir(1).exists()
    assert not cfg.week_report_dir.exists()


def test_run_week_survives_a_search_failure(cfg, paper, monkeypatch):
    """断网也不能把这一周搞崩：写一份说清楚情况的空周报（不需要 AI）。"""
    write_report_block(cfg, 1, titles=["上一次推的那篇"])

    def boom(*args, **kwargs):
        raise ConnectionResetError("connection reset by peer")

    monkeypatch.setattr(weekly, "build_candidates", boom)
    outcome = weekly.run_week(cfg, zotero_on=False, today=WEEK2)

    assert outcome.candidates == 0
    assert outcome.papers == []
    assert outcome.mode == weekly.MODE_NONE
    assert any("检索失败" in item for item in outcome.warnings)
    assert outcome.report_path.exists()
    assert outcome.queue_path.exists()


def test_run_week_survives_a_zotero_failure(cfg, paper, monkeypatch, llm):
    patch_search(monkeypatch, candidates(paper))
    monkeypatch.setenv("ZOTERO_USER_ID", "12345")
    monkeypatch.setattr(
        weekly.zotero, "zotero_create", lambda *a, **k: (_ for _ in ()).throw(OSError("no zotero"))
    )
    outcome = weekly.run_week(cfg, client=llm, today=WEEK1)
    assert outcome.zotero["ok"] is False
    assert outcome.report_path.exists()
    assert outcome.queue_path.exists()


def test_run_week_writes_the_ledger_and_queue_before_touching_zotero(cfg, paper, monkeypatch, llm):
    """回归防护：Zotero 排在最后，它没有否决权。

    线上真的遇到过：本机连接器读超时，而推送排在**所有**本地写入之前，于是这一周的
    账本、阅读清单、周报一个都没写——用户看到的现象是「阅读清单没有更新」。
    现在推送被调用时，账本和阅读清单必须已经躺在磁盘上了。

    （周报是唯一排在推送之后的产出，因为它要把推送结果写进「需要你知道的」那一节；
    连接器那边的超时已经被压到几十秒，所以这里不会再有长时间阻塞。）
    """
    from paperflow_core import vault as vault_mod

    patch_search(monkeypatch, candidates(paper))
    seen = {}

    def slow_push(papers, week, lang="zh"):
        seen["ledger"] = len(ledger.read_ledger(cfg.ledger_path))
        seen["queue"] = (cfg.week_dir(week) / vault_mod.QUEUE_NOTE).exists()
        seen["titles"] = [item["title"] for item in papers]
        return {"ok": False, "count": 0, "message": "连不上本机 Zotero（测试）"}

    monkeypatch.setenv("ZOTERO_LOCAL", "1")
    monkeypatch.delenv("ZOTERO_USER_ID", raising=False)
    monkeypatch.setattr(weekly.zotero, "push_papers", slow_push)

    outcome = weekly.run_week(cfg, client=llm, today=WEEK1)

    assert seen["ledger"] == len(outcome.papers)  # 推送时账本已经写好了
    assert seen["queue"] is True
    # 推送失败只是周报里的一句话，清单和周报本身照出
    assert outcome.zotero["ok"] is False
    assert any("连不上本机 Zotero（测试）" in item for item in outcome.warnings)
    assert outcome.queue_path.exists()
    assert "连不上本机 Zotero（测试）" in outcome.report_path.read_text(encoding="utf-8")
    # 本地写完了就说明这一周跑过了，必须记下运行日期，否则下周会重跑同一周
    assert vault_mod.load_config(cfg.state_dir).last_run == WEEK1


def test_run_week_records_a_successful_push_in_the_report(cfg, paper, monkeypatch, llm):
    """推送成功也要写进周报：终端那句话关掉窗口就没了，周报才是留下来的那份。"""
    patch_search(monkeypatch, candidates(paper))
    monkeypatch.setenv("ZOTERO_LOCAL", "1")
    monkeypatch.delenv("ZOTERO_USER_ID", raising=False)
    monkeypatch.setattr(
        weekly.zotero,
        "push_papers",
        lambda papers, week, lang="zh": {"ok": True, "count": len(papers), "message": "已导入 Zotero 4 条（测试）"},
    )

    outcome = weekly.run_week(cfg, client=llm, today=WEEK1)

    assert outcome.zotero["ok"] is True
    assert outcome.warnings == []  # 成功不该产生警告
    assert "已导入 Zotero 4 条（测试）" in outcome.report_path.read_text(encoding="utf-8")


def test_run_week_filters_out_papers_already_pushed(cfg, paper, monkeypatch, llm):
    rows = [ledger.make_row(candidates(paper)[0], 1, "2026-09-21", "deep")]
    ledger.write_ledger(cfg.ledger_path, rows)
    patch_search(monkeypatch, candidates(paper))
    outcome = weekly.run_week(cfg, client=llm, zotero_on=False, today=WEEK1)
    assert "Brain state dynamics study 0" not in [item["title"] for item in outcome.papers]


def test_run_week_warns_when_everything_was_already_pushed(cfg, paper, monkeypatch):
    pool = candidates(paper)
    ledger.write_ledger(cfg.ledger_path, [ledger.make_row(item, 1, "2026-09-21", "skim") for item in pool])
    patch_search(monkeypatch, pool)
    outcome = weekly.run_week(cfg, zotero_on=False, today=WEEK2)
    assert any("全部在之前几周推过了" in item for item in outcome.warnings)
    assert outcome.mode == weekly.MODE_NONE
    assert outcome.papers == []


def test_run_week_passes_goals_and_notes_into_the_search_context(cfg, paper, monkeypatch, llm):
    calls = []
    patch_search(monkeypatch, candidates(paper), calls)
    write_week_note(cfg, 1, "笔记.md", "我卡在 metastability 的估计上")
    weekly.run_week(cfg, client=llm, zotero_on=False, today=WEEK1)

    assert calls
    assert "metastability 的估计" in calls[0]["context"]  # 笔记进了打分上下文
    assert "长期目标" in calls[0]["context"]


def test_run_week_passes_the_citation_floor_to_the_search(cfg, paper, monkeypatch, llm):
    """引用门槛要有单一来源：默认取 config，命令行给了就用命令行的。"""
    calls = []
    patch_search(monkeypatch, candidates(paper), calls)
    weekly.run_week(cfg, client=llm, zotero_on=False, today=WEEK1)
    assert calls[0]["min_citations"] == cfg.citation_base

    weekly.run_week(cfg, client=llm, zotero_on=False, today=WEEK1, min_citations=0)
    assert calls[-1]["min_citations"] == 0


def test_run_week_passes_the_year_window_to_the_search(cfg, paper, monkeypatch, llm):
    """默认就是「近 5 年，最多放宽到近 10 年」，而且能用参数改。"""
    calls = []
    patch_search(monkeypatch, candidates(paper), calls)

    weekly.run_week(cfg, client=llm, zotero_on=False, today=WEEK1)
    assert (calls[-1]["years"], calls[-1]["max_years"]) == (5, 10)

    weekly.run_week(cfg, client=llm, zotero_on=False, today=WEEK1, years=0, max_years=0)
    assert (calls[-1]["years"], calls[-1]["max_years"]) == (0, 0)


def test_run_week_reports_the_year_window(cfg, paper, monkeypatch, llm):
    patch_search(monkeypatch, candidates(paper, 2))
    outcome = weekly.run_week(cfg, client=llm, zotero_on=False, today=WEEK1)
    assert outcome.year_window.startswith("优先 2021 年及以后")
    assert "年份窗口" in outcome.queue_path.read_text(encoding="utf-8")
    assert "年份窗口" in outcome.report_path.read_text(encoding="utf-8")


def test_run_week_respects_per_week(cfg, paper, monkeypatch, llm):
    patch_search(monkeypatch, candidates(paper, 8))
    outcome = weekly.run_week(cfg, client=llm, zotero_on=False, per_week=2, today=WEEK1)
    assert len(outcome.papers) == 2


def test_run_week_runs_twice_without_crashing(cfg, paper, monkeypatch, llm):
    # 候选要明显多过一周的份量，否则第一周就把池子用光，第二周没得挑。
    patch_search(monkeypatch, candidates(paper, 40))
    first = weekly.run_week(cfg, client=llm, zotero_on=False, today=WEEK1)
    second = weekly.run_week(cfg, client=llm, zotero_on=False, today=WEEK2)
    assert first.papers and second.papers
    assert len(first.papers) == 12
    assert len(ledger.read_ledger(cfg.ledger_path)) >= len(second.papers)


def test_an_empty_run_does_not_overwrite_the_canonical_report(cfg, paper, monkeypatch, llm):
    """空跑不许覆盖正本。

    同一周再跑一次时，候选都被自己推过了——如果不加区分地把一份「什么都没有」
    的周报写上去，带 AI 理由的正本会被悄悄换掉，而且没有任何痕迹。
    规则：这一周已经有周报，而我这次没产出清单，就另存一份带时间戳的。
    """
    patch_search(monkeypatch, candidates(paper))
    first = weekly.run_week(cfg, client=llm, zotero_on=False, today=WEEK1)
    canonical = first.report_path
    before = canonical.read_text(encoding="utf-8")
    queue_before = first.queue_path.read_text(encoding="utf-8")

    second = weekly.run_week(cfg, client=llm, zotero_on=False, today=WEEK1)

    assert canonical.read_text(encoding="utf-8") == before  # 正本一字未动
    assert first.queue_path.read_text(encoding="utf-8") == queue_before  # 清单也没被清空
    assert second.report_path != canonical
    assert second.report_path.exists()
    assert second.report_path.name.startswith("2026-09-21_第1周_空跑-")
    assert second.report_path.suffix == ".md"
    assert any(second.report_path.name in item for item in second.warnings)


def test_an_empty_run_copy_carries_no_machine_readable_block(cfg, paper, monkeypatch, llm):
    """两份同周周报都带 ``PF:WEEK`` 块的话，全局历史里就会出现两条重复的「第 n 周」。

    ``notes.read_pf_blocks`` 是「读所有周报」的唯一入口，它按目录里所有 .md 找块，
    不看文件名——所以空跑副本里的块必须删掉。
    """
    from paperflow_core import notes as notes_mod

    patch_search(monkeypatch, candidates(paper))
    first = weekly.run_week(cfg, client=llm, zotero_on=False, today=WEEK1)
    assert notes_mod.parse_pf_blocks(first.report_path.read_text(encoding="utf-8"))  # 正本当然有

    second = weekly.run_week(cfg, client=llm, zotero_on=False, today=WEEK1)
    empty_copy = second.report_path.read_text(encoding="utf-8")

    assert notes_mod.parse_pf_blocks(empty_copy) == []
    assert "空跑副本" in empty_copy
    assert [block["week"] for block in notes_mod.read_pf_blocks(cfg.week_report_dir)] == [1]


def test_a_normal_run_updates_the_report(cfg, paper, monkeypatch, llm):
    """另存只针对空跑：真有清单时应该更新正本，而不是每跑一次多堆一个文件。"""
    patch_search(monkeypatch, candidates(paper))
    weekly.run_week(cfg, client=llm, zotero_on=False, today=WEEK1)
    canonical = cfg.week_report_dir / "2026-09-21_第1周.md"
    ledger.write_ledger(cfg.ledger_path, [])  # 清空账本，让候选重新变成「没推过的」

    outcome = weekly.run_week(cfg, client=llm, zotero_on=False, today=WEEK1)

    assert outcome.mode == weekly.MODE_LLM
    assert outcome.report_path == canonical
    assert list(cfg.week_report_dir.glob("*空跑*")) == []


def test_default_deep_slots_is_two_fifths_of_the_week():
    """精读永远比泛读少：12 篇 → 5 篇精读 + 7 篇泛读。"""
    assert weekly.default_deep_slots(12) == 5
    assert weekly.default_deep_slots(6) == 3
    assert weekly.default_deep_slots(1) == 1
    assert weekly.default_deep_slots(0) == 1


def test_default_per_week_is_twelve_papers():
    from paperflow_core import vault as vault_mod

    assert vault_mod.DEFAULT_PER_WEEK == 12
    assert weekly.default_deep_slots(vault_mod.DEFAULT_PER_WEEK) == 5


def test_candidate_pool_grows_with_per_week():
    """池子是每周篇数的 5 倍，但不小于 40（老配置 6 篇 → 还是 40 篇）。"""
    assert weekly.candidate_pool_size(6) == 40
    assert weekly.candidate_pool_size(12) == 60
    assert weekly.candidate_pool_size(1) == 40


def test_run_week_fetches_a_pool_wide_enough_for_twelve(cfg, paper, monkeypatch, llm):
    calls = []
    patch_search(monkeypatch, candidates(paper, 3), calls)

    weekly.run_week(cfg, client=llm, zotero_on=False, today=WEEK1)
    assert calls[-1]["max_candidates"] == 60

    weekly.run_week(cfg, client=llm, zotero_on=False, today=WEEK1, per_week=6)
    assert calls[-1]["max_candidates"] == 40


def test_link_for_prefers_the_open_pdf(paper):
    assert weekly.link_for(paper()).endswith("paper.pdf")
    assert weekly.link_for(paper(open_pdf="", doi="10.1/a", url="https://x")) == "https://doi.org/10.1/a"
    assert weekly.link_for(paper(open_pdf="", doi="")) == "https://example.org/paper"
    assert weekly.link_for({}) == ""


@pytest.mark.parametrize("lang,expected", [("zh", "精读"), ("en", "read closely")])
def test_role_labels_are_localised(lang, expected):
    assert weekly.role_label(weekly.ROLE_DEEP, lang) == expected


def test_unknown_role_degrades_gracefully():
    assert weekly.role_label("nonsense", "zh")
    assert weekly.track_label("nonsense", "zh")
