"""每周循环：``week`` 命令的全部行为都在这一个文件里。

一次 ``week`` 做六件事：

1. 读你的长期目标 + 自上次运行以来的**全部**笔记 + 全部历史周报的摘要行；
2. 从这些内容里挑出本周该检索的几个方向（:mod:`paperflow_core.terms`）；
3. 检索 → 剔除引用量不够的、年份太旧的 → 让 AI 从剩下的候选里挑满本周配额并说明理由；
4. **先落盘**：账本 → 当周文件夹里的 ``阅读清单.md`` → 库里的 ``周报月报/`` 的周报 → ``config.json`` 的运行日期；
5. **最后**才推进 Zotero，只打一个 ``第n周`` 标签。

七条不可动摇的规矩
------------------

* **本地产出永远先写完，Zotero 排在最后。** 它是个跑在 ``localhost`` 上的服务，
  可能没开、可能正忙。以前它排在所有本地写入之前，于是一超时，这一周的清单、账本、
  周报就全都写不出来——现象是「阅读清单没有更新」，实际上一个文件都没写。
  它只是可选的加分项，不该对本周产出有否决权。

* **清单只能由 AI 产出。** 没有可用的模型（没配 key / 连不上 / 返回的不是 JSON）
  就直接报错退出，**不写清单、不写周报、不推 Zotero、不记账本**，所以下次会重跑同一周。
  任何形式的「离线排序」都不再存在——它给不出带依据的推荐，只会污染状态。
* **只有引用量够高的论文能进候选池。** 门槛按论文年龄分档
  （:func:`paperflow_core.ranking.citation_floor`），够不着的候选在打分之前就被剔除。
* **只推最近的工作。** 候选池优先只装近 :data:`paperflow_core.ranking.YEAR_WINDOW` 年的论文；
  这一档凑不满时，才用近 :data:`paperflow_core.ranking.YEAR_WINDOW_MAX` 年以内、更老的补足。
  实际用了哪一档会写在终端、清单和周报里（:func:`year_window_note`）。
* **AI 不许编造文献。** 它只能从脚本抓到的候选里**挑 paperId**；
  标题、DOI、引用数、链接全部由脚本回填；认不出的 paperId 一律丢弃。
* **检索层面的失败不冒充成功。** 网络断了、一篇都没选出，就写一份说清原因的空清单周报，
  并且**不覆盖**这一周已有的正本。
* **全局而不是局部。** 读笔记的范围是「自上次运行以来」，跳了一周没跑，
  它会把漏掉的那一周一起吞掉，所以不会因为漏跑而出现盲区。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import ledger, terms, zotero
from .llm import LLMClient, LLMError, build_prompt, llm_failure_reason
from .notes import PF_PATTERN, Corpus, global_history_block, read_notes, read_pf_blocks, render_pf_block
from .ranking import (
    CITATION_BASE,
    CITATION_BASE_YEARS,
    YEAR_WINDOW,
    YEAR_WINDOW_MAX,
    citation_floor,
    citation_signal,
    year_window_stats,
)
from .search import build_candidates
from .texts import pick
from .vault import DEFAULT_PER_WEEK, QUEUE_NOTE, REPORTS_DIR, VaultConfig, mark_run, week_label

ROLE_DEEP = "deep"
ROLE_SKIM = "skim"
ROLE_LABELS = {"deep": {"zh": "精读", "en": "read closely"}, "skim": {"zh": "泛读", "en": "skim"}}
TRACK_TARGET = "target"
TRACK_EXPLORE = "explore"
TRACK_LABELS = {"target": {"zh": "服务长期目标", "en": "target"}, "explore": {"zh": "发散探索", "en": "explore"}}

#: 清单由 AI 挑出来（唯一正常模式）。
MODE_LLM = "llm"
#: 本次没有候选，根本没调用 AI（不算失败，但也不会凭空给清单）。
MODE_NONE = "none"
MODE_LABELS = {
    MODE_LLM: {"zh": "AI 筛选", "en": "AI screening"},
    MODE_NONE: {"zh": "无候选，没有调用 AI", "en": "no candidates, AI not called"},
}

#: 候选池的下限。池子太小，要挑 12 篇就等于没得挑。
MAX_CANDIDATES = 40
#: 候选池相对「每周要几篇」的倍数：池子是每周篇数的这么多倍。
#: 取 :func:`max` 与 :data:`MAX_CANDIDATES` 是为了不缩小老配置（每周 6 篇 → 仍是 40 篇）。
CANDIDATE_POOL_PER_PICK = 5


def candidate_pool_size(per_week: int) -> int:
    """这一周要抓多少篇候选回来给 AI 挑。"""
    return max(MAX_CANDIDATES, int(per_week) * CANDIDATE_POOL_PER_PICK)


def role_label(role: str, lang: str = "zh") -> str:
    entry = ROLE_LABELS.get(role) or ROLE_LABELS[ROLE_SKIM]
    return pick(lang, entry["zh"], entry["en"])


def track_label(track: str, lang: str = "zh") -> str:
    entry = TRACK_LABELS.get(track) or TRACK_LABELS[TRACK_EXPLORE]
    return pick(lang, entry["zh"], entry["en"])


def mode_label(mode: str, lang: str = "zh") -> str:
    """给人看的筛选模式（报告与终端都用它，不再直接打印 llm/fallback 这种内部字符串）。"""
    entry = MODE_LABELS.get(mode)
    if entry is None:
        return mode or pick(lang, "未知", "unknown")
    return pick(lang, entry["zh"], entry["en"])


def citation_policy(base: int, lang: str = "zh") -> str:
    """把引用门槛写成一句人能看懂的话（周报和终端都会显示）。"""
    if base <= 0:
        return pick(lang, "未设引用门槛（--min-citations 0）", "no citation floor (--min-citations 0)")
    now = dt.date.today().year
    return pick(
        lang,
        f"发表满 {CITATION_BASE_YEARS} 年 ≥{citation_floor(now - CITATION_BASE_YEARS, base)} 次引用，"
        f"2–4 年 ≥{citation_floor(now - 3, base)} 次，近两年 ≥{citation_floor(now, base)} 次",
        f">={citation_floor(now - CITATION_BASE_YEARS, base)} citations if {CITATION_BASE_YEARS}+ years old, "
        f">={citation_floor(now - 3, base)} if 2-4 years, >={citation_floor(now, base)} if newer",
    )


def default_deep_slots(per_week: int) -> int:
    """精读约占每周篇数的 2/5，向上取整（12 篇 → 5 篇精读，其余泛读）。

    精读的意思是**要写一篇笔记**，所以它永远比泛读少。要改就直接用 ``--deep N``。
    """
    return max(1, (int(per_week) * 2 + 4) // 5)


def year_policy(years: int, max_years: int, lang: str = "zh", today: Optional[dt.date] = None) -> str:
    """把年份窗口写成一句人能看懂的话（周报和终端都会显示）。"""
    if int(years) <= 0:
        return pick(lang, "不限年份（--years 0）", "no year window (--years 0)")
    now = (today or dt.date.today()).year
    start = now - int(years)
    if int(max_years) <= int(years):
        return pick(lang, f"只收 {start} 年及以后", f"only papers from {start} or later")
    floor = now - int(max_years)
    return pick(
        lang,
        f"优先 {start} 年及以后；凑不满时最多放宽到 {floor} 年及以后",
        f"prefer {start} or later; widen to {floor} or later only when the pool is thin",
    )


def year_window_note(
    papers: Sequence[Dict[str, Any]],
    years: int = YEAR_WINDOW,
    max_years: int = YEAR_WINDOW_MAX,
    lang: str = "zh",
    today: Optional[dt.date] = None,
) -> str:
    """年份窗口这回实际起了什么作用（前半句是规则，后半句是候选的真实构成）。"""
    rule = year_policy(years, max_years, lang, today)
    stats = year_window_stats(papers, years, max_years, today)
    if not stats["total"]:
        return pick(lang, f"{rule}；这次没有候选", f"{rule}; no candidates this run")
    if int(years) <= 0:
        span = f"{stats['oldest']}–{stats['newest']}" if stats["oldest"] else "年份未知"
        return pick(lang, f"{rule}；候选 {span}", f"{rule}; candidates {span}")
    parts = [pick(lang, f"近 {int(years)} 年内 {stats['in_window']} 篇", f"{stats['in_window']} within {int(years)} years")]
    if stats["widened"]:
        parts.append(
            pick(
                lang,
                f"放宽补入 {stats['widened']} 篇",
                f"{stats['widened']} added by widening to {int(max_years)} years",
            )
        )
    if stats["unknown"]:
        parts.append(pick(lang, f"{stats['unknown']} 篇年份未知", f"{stats['unknown']} with an unknown year"))
    span = f"{stats['oldest']}–{stats['newest']}" if stats["oldest"] else "年份未知"
    return pick(lang, f"{rule}；候选 {span}：{'、'.join(parts)}", f"{rule}; candidates {span}: {', '.join(parts)}")


def link_for(paper: Dict[str, Any]) -> str:
    """优先给 PDF，其次 DOI，最后来源页——Zotero 抓不到全文时你还能点。"""
    pdf = (paper.get("open_pdf") or "").strip()
    if pdf:
        return pdf
    doi = (paper.get("doi") or "").strip()
    if doi:
        return f"https://doi.org/{doi}"
    return (paper.get("url") or "").strip()


# --------------------------------------------------------------------- 上下文


@dataclass
class Context:
    """一次 ``week`` 用到的全部本地事实。"""

    week: int
    window: str
    goals: str
    week_note: str
    corpus: Corpus
    history: List[Dict[str, Any]]
    rows: List[Dict[str, str]]
    stats_by_week: Dict[int, Dict[str, int]]
    query: str = ""
    prev_week: int = 0
    prev_stats: Dict[str, int] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    @property
    def prev_rows(self) -> List[Dict[str, str]]:
        return ledger.week_rows(self.rows, self.prev_week) if self.prev_week else []

    @property
    def context_text(self) -> str:
        """给启发式打分用的纯文本：长期目标 + 本周笔记 + 历史标题。"""
        chunks = [self.goals, self.week_note]
        chunks.extend(note.text for note in self.corpus.notes)
        for item in self.history:
            chunks.extend(str(title) for title in (item.get("titles") or []))
        return "\n".join(chunk for chunk in chunks if chunk)

    @property
    def empty(self) -> bool:
        return not (self.goals.strip() or self.corpus.notes or self.history)


def build_context(
    cfg: VaultConfig,
    week: int,
    today: dt.date,
    full: bool = False,
    query: str = "",
    min_queries: int = 3,
    lang: str = "zh",
) -> Context:
    """把长期目标、笔记、账本、历史周报读进来。"""
    weeks = cfg.notes_weeks(today, full=full)
    corpus = read_notes(cfg, weeks)
    rows, _ = ledger.refresh(ledger.read_ledger(cfg.ledger_path), corpus.mentions)
    stats_by_week = {
        number: ledger.stats(ledger.week_rows(rows, number))
        for number in sorted({int(row.get("week") or 0) for row in rows if row.get("week")})
    }
    goals = cfg.read_goals()
    week_note = ""
    if cfg.week_note_path.exists():
        week_note = cfg.week_note_path.read_text(encoding="utf-8", errors="replace").strip()
        if week_note.startswith("# "):
            week_note = week_note.split("\n", 1)[-1].strip()

    context = Context(
        week=week,
        window=_window(cfg, week),
        goals=goals,
        week_note=week_note,
        corpus=corpus,
        history=read_pf_blocks(cfg.week_report_dir),
        rows=rows,
        stats_by_week=stats_by_week,
        prev_week=week - 1 if week > 1 else 0,
    )
    context.prev_stats = stats_by_week.get(context.prev_week, {})
    context.query = query.strip() or build_query(context, min_queries)
    if not cfg.goal_files():
        context.warnings.append(
            pick(
                lang,
                f"没读到长期目标（{cfg.goals}）。推荐只能靠笔记和历史，方向会漂。"
                "请在长期目标文件里写几句你六个月后想做到什么。",
                f"No long-term goals found at {cfg.goals}; recommendations will drift.",
            )
        )
    return context


def _window(cfg: VaultConfig, week: int) -> str:
    start, end = cfg.week_bounds(week)
    return f"{start.isoformat()}..{end.isoformat()}"


def build_query(context: Context, limit: int = 3) -> str:
    """从长期目标里挑检索方向；目标里认不出领域词时退到笔记。"""
    found = terms.pick_queries(context.goals, limit=limit)
    if not found or found == list(terms.FALLBACK_QUERIES):
        from_notes = terms.pick_queries(
            "\n".join(note.text for note in context.corpus.notes) or context.goals, limit=limit
        )
        found = list(dict.fromkeys(list(found) + list(from_notes)))[:limit]
    return "|".join(found)


# ------------------------------------------------------------------- AI 筛选


def _candidate_brief(index: int, paper: Dict[str, Any]) -> str:
    abstract = (paper.get("abstract") or "")[:600]
    return "\n".join(
        [
            f"[{index}] paperId={paper.get('paperId')}",
            f"    title: {paper.get('title')}",
            f"    year={paper.get('year')} venue={paper.get('venue')} citations={citation_signal(paper)}",
            f"    heuristic_score={paper.get('heuristic_score')}",
            f"    abstract: {abstract}",
        ]
    )


def selection_prompt(context: Context, candidates: Sequence[Dict[str, Any]], lang: str, per_week: int, deep_slots: int) -> str:
    """构造本周的筛选 prompt。只让模型挑 paperId 并说理由，不给它编造事实的机会。"""
    notes_view = context.corpus.prompt_block(lang)
    if len(notes_view) > 12000:
        notes_view = notes_view[:12000] + "\n……（笔记过长，已截断）"
    history_view = global_history_block(context.history, context.stats_by_week, lang)
    last_week = ""
    if context.prev_week:
        rows = context.prev_rows
        if rows:
            detail = "\n".join(
                f"  - [{row.get('status') or '未标记'}] {row.get('title')}" for row in rows
            )
        else:
            detail = "  （上周没有推荐记录）"
        last_week = f"第{context.prev_week}周清单及你的标记：\n{detail}"
    else:
        last_week = "（这是第一周，还没有上一周）"

    body = f"""You are screening papers for a beginner in computational neuroscience who is
studying how brain states change over time, how neural circuits produce them, how to model
them from EEG / fMRI / behaviour, and how that connects to psychiatric disorders and closed-loop control.

The student reads DIVERGENTLY but must accumulate CONVERGENT understanding.
Their biggest fear is reading a lot and retaining nothing, so every recommendation must
be tied to something they already wrote down.

## Their long-term goals (verbatim, they wrote these themselves)
{context.goals[:6000] or '(they have not written any yet — say so in your output)'}

## What they wrote this week ("本周.md")
{context.week_note[:1500] or '(nothing)'}

## NEW notes since the last run (their actual reading / coding notes, full text)
{notes_view}

## Global history — every week so far, one block each
{history_view}

{last_week}

## Candidates (deduplicated, already past a citation gate and a recency window; abstracts truncated)
{chr(10).join(_candidate_brief(i, p) for i, p in enumerate(candidates))}

## Quality rule (hard requirement — read this before you pick)
Every candidate above passed a citation gate, so the citation numbers are real
(from OpenAlex / Semantic Scholar) and are the only objective quality signal you have.
The pool is also recency-filtered: recent work is taken first, and older work only enters
the pool when the recent window cannot fill it. So the candidates you see are already the
recent, well-cited part of the literature — your job is to pick the best of THEM.

- Prefer the foundational, heavily-cited work. A 300-citation review beats a fresh 40-citation
  preprint unless you can say concretely what the preprint gives the student that nothing else does.
- When two candidates do the same job, prefer the newer one and say so explicitly.
- Never justify a pick with "it is recent" or "it is novel" alone: recency is a filter the
  script already applied, not a reason.
- In the "why" field, cite the actual number (e.g. "被引 1200 次，是这条线最常被引的综述") for at
  least half of your picks, so the student can see the evidence behind your choice.

## Task
Select exactly {per_week} papers, no more, all of them from the candidate list above.
At most {deep_slots} of them may be role "deep".

For each paper you must justify it with EVIDENCE FROM THE STUDENT'S OWN WRITING:

- "why" MUST name the specific week number and/or the specific sentence in their
  long-term goals that this paper serves. Example: "第3周你写到自己分不清 metastability 和
  criticality，这篇正好把它们的关系讲清楚。" A justification that only restates the
  paper's abstract is a failure — reject the paper and pick another.
- role "deep": worth reading end to end and writing a note about. role "skim": abstract + figures only.
- track "target": serves a long-term goal or a gap they wrote down. track "explore":
  reaches outside, but say what unexpected link it could create.
- difficulty for a real beginner: "intro" (undergraduate maths), "mid" (linear algebra /
  differential equations), "hard" (research-level maths or heavy engineering).

Also required:

- "uncovered": quote ONE long-term goal that NO week so far has touched. Use the
  global history above to check. If everything is covered, write "none".
- "convergence": one short paragraph on how this set keeps reading divergent while
  making the accumulation convergent. Be blunt if you think the student is scattering.
- "notes_digest": 3-5 bullets summarising what the student ACTUALLY did this week,
  based only on their notes. If the notes are unrelated to their goals, say that.

Return JSON with exactly this shape. Use only paperIds that appear above. Never invent a paper.
{{
  "notes_digest": ["...", "..."],
  "papers": [
    {{
      "paperId": "...",
      "role": "deep|skim",
      "track": "target|explore",
      "why": "...",
      "gain": "what specific thing they will be able to do or explain afterwards",
      "difficulty": "intro|mid|hard"
    }}
  ],
  "convergence": "...",
  "uncovered": "...",
  "open_questions": ["..."],
  "engineering": {{
    "task": "one small concrete thing to implement this week that supports this reading (no code here)",
    "skills": ["python", "pandas", "..."],
    "done_criteria": "how they know it is done",
    "hours": 3
  }}
}}"""
    return build_prompt(body, lang)


def _enrich(entry: Dict[str, Any], by_id: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """把 AI 的选择与本地真实论文数据合并；认不出的 paperId 直接丢掉。"""
    paper = by_id.get(str(entry.get("paperId") or ""))
    if not paper:
        return None
    role = entry.get("role") if entry.get("role") in (ROLE_DEEP, ROLE_SKIM) else ROLE_SKIM
    track = entry.get("track") if entry.get("track") in (TRACK_TARGET, TRACK_EXPLORE) else TRACK_TARGET
    return {
        "paperId": paper["paperId"],
        "title": paper.get("title") or "",
        "year": paper.get("year"),
        "venue": paper.get("venue") or "",
        "author_text": paper.get("author_text") or "",
        "doi": paper.get("doi") or "",
        "url": paper.get("url") or "",
        "open_pdf": paper.get("open_pdf") or "",
        "citationCount": paper.get("citationCount") or 0,
        "source": paper.get("source") or "",
        "heuristic_score": paper.get("heuristic_score"),
        "role": role,
        "track": track,
        "why": str(entry.get("why") or "").strip(),
        "gain": str(entry.get("gain") or "").strip(),
        "difficulty": entry.get("difficulty") if entry.get("difficulty") in ("intro", "mid", "hard") else "mid",
        "themes": terms.theme_line([paper.get("title") or ""], limit=4),
        "matched": terms.count_terms(f"{paper.get('title') or ''} {paper.get('abstract') or ''}")[:4],
    }


def _trim(papers: List[Dict[str, Any]], per_week: int, deep_slots: int) -> List[Dict[str, Any]]:
    """裁到 per_week 篇，精读不超过 deep_slots（超了的降级成泛读，不丢）。"""
    seen: set = set()
    ordered: List[Dict[str, Any]] = []
    for paper in papers:
        key = ledger.key_of_paper(paper) or paper["paperId"]
        if key in seen:
            continue
        seen.add(key)
        ordered.append(paper)
    deep_used = 0
    for paper in ordered:
        if paper["role"] == ROLE_DEEP:
            if deep_used >= deep_slots:
                paper["role"] = ROLE_SKIM
            else:
                deep_used += 1
    return ordered[:per_week]


def select(
    client: Optional[LLMClient],
    context: Context,
    candidates: Sequence[Dict[str, Any]],
    lang: str = "zh",
    per_week: int = DEFAULT_PER_WEEK,
    deep_slots: Optional[int] = None,
) -> Dict[str, Any]:
    """让 AI 从候选里挑论文。**没有可用的 AI 就抛 :class:`LLMError`**，不再有离线排序。

    ``mode`` 只有两种取值：``llm``（AI 挑的）和 ``none``（根本没有候选，没调用 AI）。
    抛错的三种情况都不会写任何文件，所以下次会重跑同一周：

    * 没配模型（``client`` 为 None 或 ``available`` 为假）；
    * 模型连不上，或者连试两次都没给出可解析的 JSON；
    * 模型给的所有 ``paperId`` 都不在候选表里（它在编造文献）。
    """
    slots = deep_slots if deep_slots is not None else default_deep_slots(per_week)
    if not candidates:
        return {
            "notes_digest": [],
            "papers": [],
            "convergence": "",
            "uncovered": "",
            "open_questions": [],
            "engineering": {},
            "mode": MODE_NONE,
            "warnings": [],
        }
    if client is None or not client.available:
        raise LLMError(
            pick(
                lang,
                "没有可用的 AI，本次不产出清单。请检查 .env 里的 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL，"
                "然后重跑本周（还没有写任何文件）。",
                "No LLM is available, so no list was produced. Check LLM_API_KEY / LLM_BASE_URL / "
                "LLM_MODEL in .env and re-run this week (nothing was written).",
            )
        )

    by_id = {paper["paperId"]: paper for paper in candidates}
    prompt = selection_prompt(context, candidates, lang, per_week, slots)
    payload: Optional[Dict[str, Any]] = None
    failure = ""
    # 模型偶尔返回一段解释、或者 JSON 被截断。换个温度再试一次；还是不行就明确报错，
    # 而不是「自己编一份清单」——那正是这一版要根除的行为。
    for temperature in (0.2, 0.0):
        data, raw, error = client.json_ex(prompt, temperature=temperature)
        if data is not None and isinstance(data.get("papers"), list):
            payload = data
            break
        if data is not None:
            failure = pick(lang, "模型返回的 JSON 里没有 papers 数组", 'the model\'s JSON had no "papers" array')
        else:
            failure = llm_failure_reason(error, raw)
    if payload is None:
        raise LLMError(
            pick(
                lang,
                f"AI 没有返回可用的选择：{failure}。本次不产出清单，下次会重跑第 {context.week} 周。",
                f"The AI did not return a usable selection: {failure}. No list was produced; "
                f"week {context.week} will be retried.",
            )
        )

    enriched = [_enrich(item, by_id) for item in payload["papers"]]
    rejected = sum(1 for item in enriched if item is None)
    papers = _trim([item for item in enriched if item], per_week, slots)
    if not papers:
        raise LLMError(
            pick(
                lang,
                f"AI 选了 {len(payload['papers'])} 篇，但没有一篇的 paperId 在候选表里——它多半在编造文献。"
                "本次不产出清单，下次会重跑本周。",
                f"The AI picked {len(payload['papers'])} papers but none of their paperIds existed — "
                "it is probably inventing references. No list was produced; this week will be retried.",
            )
        )

    warnings: List[str] = []
    if rejected:
        warnings.append(
            pick(
                lang,
                f"AI 给出的选择里有 {rejected} 条 paperId 不在候选表里，已丢弃（它不能凭空造文献）。",
                f"{rejected} of the AI's picks referenced unknown paperIds and were dropped.",
            )
        )
    return {
        "notes_digest": [str(x) for x in (payload.get("notes_digest") or [])],
        "papers": papers,
        "convergence": str(payload.get("convergence") or "").strip(),
        "uncovered": str(payload.get("uncovered") or "").strip(),
        "open_questions": [str(x) for x in (payload.get("open_questions") or [])],
        "engineering": payload.get("engineering") or {},
        "mode": MODE_LLM,
        "warnings": warnings,
    }


# --------------------------------------------------------------------- 渲染


def render_queue(
    context: Context,
    papers: Sequence[Dict[str, Any]],
    review: Dict[str, Any],
    lang: str = "zh",
    year_window: str = "",
) -> str:
    """写进当周文件夹的 ``阅读清单.md``——你实际照它做事的文件。"""
    lines = [f"# {week_label(context.week)} 阅读清单", ""]
    lines.append(f"- 区间：{context.window}")
    lines.append(f"- Zotero 标签：`{week_label(context.week)}`（在 Zotero 标签面板点它，就是这周要读的）")
    lines.append(f"- 筛选：{mode_label(str(review.get('mode') or ''), lang)}")
    if year_window:
        lines.append(f"- 年份窗口：{year_window}")
    lines.append("")

    for role in (ROLE_DEEP, ROLE_SKIM):
        group = [paper for paper in papers if paper["role"] == role]
        if not group:
            continue
        lines.append(f"## {role_label(role, lang)}（{len(group)} 篇）")
        lines.append("")
        for index, paper in enumerate(group, start=1):
            lines.append(f"### {index}. {paper['title']}")
            meta = " · ".join(
                str(bit)
                for bit in (
                    paper.get("year") or "",
                    paper.get("venue") or "",
                    f"{paper.get('citationCount') or 0} 引用",
                    f"难度 {paper.get('difficulty')}",
                    track_label(paper.get("track") or "", lang),
                )
                if bit
            )
            lines.append(f"- {meta}")
            link = link_for(paper)
            if link:
                lines.append(f"- 链接：{link}")
            if paper.get("themes"):
                lines.append(f"- 主题：{paper['themes']}")
            if paper.get("why"):
                lines.append(f"- **为什么读这篇**：{paper['why']}")
            if paper.get("gain"):
                lines.append(f"- **读完你会**：{paper['gain']}")
            lines.append("")

    if not papers:
        lines.append(pick(lang, "## 这一周没有新论文可选", "## Nothing new this week"))
        lines.append("")
        lines.append(
            pick(
                lang,
                "要么是检索没连上，要么是候选都被之前几周推过了，要么是引用门槛把候选全刷掉了。周报里有详细说明。",
                "Either the search failed, everything was already pushed in previous weeks, or every "
                "candidate fell below the citation floor. See the report.",
            )
        )
        lines.append("")

    if review.get("uncovered"):
        lines.append("## 长期目标里还没被碰过的一条")
        lines.append("")
        lines.append(f"> {review['uncovered']}")
        lines.append("")
    if review.get("convergence"):
        lines.append("## 这一周的收敛判断")
        lines.append("")
        lines.append(review["convergence"])
        lines.append("")
    if review.get("open_questions"):
        lines.append("## 还悬着的问题")
        lines.append("")
        lines.extend(f"- {item}" for item in review["open_questions"])
        lines.append("")
    lines.append(f"> 本周工程建议见 [`{_report_name(context)}`](../{REPORTS_DIR}/{_report_name(context)})。")
    return "\n".join(lines).rstrip() + "\n"


def _report_name(context: Context) -> str:
    return f"{context.window.split('..')[0]}_{week_label(context.week)}.md"


def render_report(
    context: Context,
    papers: Sequence[Dict[str, Any]],
    review: Dict[str, Any],
    extras: Dict[str, Any],
    lang: str = "zh",
    citation_base: int = CITATION_BASE,
) -> Tuple[str, Dict[str, Any]]:
    """写周报正文，同时算出要嵌进文件的 ``PF:WEEK`` 块。"""
    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# {week_label(context.week)} 周报", ""]
    lines.append(f"- 区间：{context.window}")
    lines.append(f"- 生成时间：{generated}")
    lines.append(f"- 检索式：`{context.query}`")
    lines.append(f"- 筛选模式：{mode_label(str(review.get('mode') or ''), lang)}")
    lines.append(f"- 引用门槛：{citation_policy(citation_base, lang)}")
    year_window = str(extras.get("year_window") or "").strip()
    if year_window:
        lines.append(f"- 年份窗口：{year_window}")
    lines.append("")

    # 1. 上周执行
    lines.append(f"## 1. 上周（第{context.prev_week}周）执行情况" if context.prev_week else "## 1. 上周执行情况")
    lines.append("")
    if context.prev_week and context.prev_rows:
        counts = context.prev_stats
        lines.append(
            f"- Zotero 推了 {counts.get('pushed', len(context.prev_rows))} 篇："
            f"已读 {counts.get('read', 0)}、跳过 {counts.get('skipped', 0)}、未标记 {counts.get('unmarked', 0)}"
        )
        with_notes = [row.get("title") for row in context.prev_rows if row.get("evidence") == "1"]
        without = [row.get("title") for row in context.prev_rows if row.get("evidence") != "1"]
        if with_notes:
            lines.append(f"- 笔记里提到过的（我按标题前几个词猜的，会误判）：{'；'.join(str(t) for t in with_notes)}")
        if without:
            lines.append(f"- 没有任何笔记提到、我当作**没读**的：{'；'.join(str(t) for t in without)}")
        lines.append("")
        lines.append(
            "> 猜错了就直接改 `PaperFlow/进度.csv` 里的 `status` 列（`已读` / `跳过`）。"
            "脚本只改自己写的 `待读`，你改过的它永远不动。"
        )
    else:
        lines.append(pick(lang, "- （没有上一周的记录）", "- (no previous week on record)"))
    lines.append("")

    # 2. 这周笔记
    lines.append("## 2. 你自己写的东西")
    lines.append("")
    if context.corpus.empty:
        lines.append(pick(lang, "- 自上次运行以来没有任何笔记。", "- No new notes since the last run."))
    else:
        weeks = "、".join(week_label(w) for w in context.corpus.weeks)
        lines.append(f"- 读了 {len(context.corpus.notes)} 份笔记（{weeks}）")
        for note in context.corpus.notes:
            lines.append(f"  - `{note.path.name}`（{len(note.text)} 字）")
        if context.corpus.trimmed:
            lines.append("- （有笔记超出长度预算，AI 只看到前半部分）")
        if context.week_note:
            lines.append(f"- `本周.md`：{context.week_note}")
        for item in review.get("notes_digest") or []:
            lines.append(f"- {item}")
    lines.append("")

    # 3. 本周清单
    lines.append("## 3. 本周清单")
    lines.append("")
    if papers:
        lines.append(f"- {len(papers)} 篇 → `{extras.get('queue_path', '')}`")
        for index, paper in enumerate(papers, start=1):
            lines.append(
                f"  {index}. [{role_label(paper['role'], lang)}] {paper['title']}"
                f"（{paper.get('year') or '?'}，{paper.get('venue') or '未知期刊'}）"
            )
        zotero_message = str(extras.get("zotero_message") or "").strip()
        if zotero_message:
            # 消息本身已经按 lang 本地化过了，这里不用再翻一次。
            lines.append(f"- Zotero：{zotero_message}")
    else:
        lines.append(pick(lang, "- 本次没有选出任何新论文。", "- Nothing selected this run."))
    lines.append("")

    # 4. 收敛判断
    if review.get("convergence"):
        lines.append("## 4. 为什么这样选不算散")
        lines.append("")
        lines.append(review["convergence"])
        lines.append("")
    if review.get("uncovered"):
        lines.append("## 5. 长期目标覆盖情况")
        lines.append("")
        lines.append(f"- 从未被任何一周碰过的一条：{review['uncovered']}")
        lines.append("")
    if review.get("open_questions"):
        lines.append("## 6. 还悬着的问题")
        lines.append("")
        lines.extend(f"- {item}" for item in review["open_questions"])
        lines.append("")

    # 工程建议
    engineering = review.get("engineering") or {}
    lines.append("## 7. 本周工程建议")
    lines.append("")
    if engineering.get("task"):
        lines.append(f"- 任务：{engineering['task']}")
        if engineering.get("skills"):
            skills = engineering["skills"]
            lines.append(f"- 需要：{', '.join(str(s) for s in skills) if isinstance(skills, list) else skills}")
        if engineering.get("done_criteria"):
            lines.append(f"- 完成标准：{engineering['done_criteria']}")
        hours = engineering.get("hours") or engineering.get("estimated_hours")
        if hours:
            lines.append(f"- 预估：{hours} 小时")
    else:
        lines.append(
            pick(
                lang,
                "- 这次没能给建议（AI 不可用）。读完之后告诉我你卡在哪，下周会补上。",
                "- No recommendation this run (AI unavailable).",
            )
        )
    lines.append("")
    lines.append("> 这里只给建议。脚本不碰 git，也不替你写代码。")
    lines.append("")

    if context.warnings or extras.get("warnings"):
        lines.append("## 8. 需要你知道的")
        lines.append("")
        lines.extend(f"- {item}" for item in list(context.warnings) + list(extras.get("warnings") or []))
        lines.append("")

    block = {
        "week": context.week,
        "window": context.window,
        "query": context.query,
        "themes": terms.theme_line([paper["title"] for paper in papers]),
        "titles": [paper["title"] for paper in papers],
        "uncovered": review.get("uncovered") or "",
        "mode": review.get("mode"),
    }
    lines.append(render_pf_block(block))
    return "\n".join(lines).rstrip() + "\n", block


# --------------------------------------------------------------------- 主流程


@dataclass
class WeekOutcome:
    week: int
    window: str
    query: str
    mode: str
    papers: List[Dict[str, Any]]
    report_block: Dict[str, Any] = field(default_factory=dict)
    queue_path: Optional[Path] = None
    report_path: Optional[Path] = None
    zotero: Dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False
    candidates: int = 0
    notes_used: int = 0
    year_window: str = ""
    warnings: List[str] = field(default_factory=list)


def run_week(
    cfg: VaultConfig,
    client: Optional[LLMClient] = None,
    lang: str = "zh",
    dry_run: bool = False,
    full: bool = False,
    per_week: Optional[int] = None,
    deep_slots: Optional[int] = None,
    query: str = "",
    min_year: Optional[int] = None,
    sources: Optional[str] = None,
    seeds: Optional[Sequence[str]] = None,
    zotero_on: bool = True,
    today: Optional[dt.date] = None,
    min_citations: Optional[int] = None,
    years: Optional[int] = None,
    max_years: Optional[int] = None,
) -> WeekOutcome:
    """跑一周。``dry_run=True`` 时不写任何文件、不碰 Zotero。

    没有可用的 AI 时 :func:`select` 会抛 :class:`~paperflow_core.llm.LLMError`：
    这种情况下**一个文件都不会写**（清单、周报、账本、Zotero 全不动），
    所以同一周下次会被重跑，不会留下半成品。

    ``years`` / ``max_years`` 是年份窗口（默认近 5 年，凑不够放宽到近 10 年），
    ``years=0`` 表示不限年份。
    """
    anchor = today or dt.date.today()
    size = int(per_week or cfg.per_week or DEFAULT_PER_WEEK)
    slots = deep_slots if deep_slots is not None else default_deep_slots(size)
    pool = candidate_pool_size(size)
    base = int(cfg.citation_base if min_citations is None else min_citations)
    window_years = YEAR_WINDOW if years is None else int(years)
    window_max = YEAR_WINDOW_MAX if max_years is None else int(max_years)
    week = cfg.current_week(anchor)
    context = build_context(cfg, week, anchor, full=full, query=query, lang=lang)
    warnings: List[str] = []

    candidates: List[Dict[str, Any]] = []
    search_error = ""
    try:
        candidates = build_candidates(
            context.query,
            list(seeds or []),
            min_year,
            pool,
            context.context_text,
            sources,
            min_citations=base,
            years=window_years,
            max_years=window_max,
        )
    except Exception as exc:  # 网络断了也要出周报
        search_error = f"{type(exc).__name__}: {exc}"
        warnings.append(
            pick(
                lang,
                f"检索失败（{search_error}）。这一周没有候选，所以清单是空的。",
                f"Search failed ({search_error}); there are no candidates this week, so the list is empty.",
            )
        )

    fresh = ledger.filter_unseen(candidates, context.rows)
    if candidates and not fresh:
        warnings.append(
            pick(
                lang,
                f"检索到 {len(candidates)} 篇候选，但全部在之前几周推过了。"
                "想让候选池更深，可以加 `--full` 或换 `--topic`。",
                f"{len(candidates)} candidates found but all were already pushed before.",
            )
        )
    if not candidates and not search_error:
        warnings.append(
            pick(
                lang,
                "这次一篇候选都没找到。可以换 `--topic`，或者放宽 `--min-year` / `--min-citations`。",
                "No candidates at all this run; try --topic, or relax --min-year / --min-citations.",
            )
        )

    review = select(client, context, fresh, lang=lang, per_week=size, deep_slots=slots)
    warnings.extend(str(item) for item in (review.get("warnings") or []))
    papers = list(review.get("papers") or [])

    outcome = WeekOutcome(
        week=week,
        window=context.window,
        query=context.query,
        mode=str(review.get("mode") or ""),
        papers=papers,
        dry_run=dry_run,
        candidates=len(candidates),
        notes_used=len(context.corpus.notes),
        year_window=year_window_note(candidates, window_years, window_max, lang, anchor),
        warnings=warnings,
    )
    if dry_run:
        outcome.report_block = {"week": week, "window": context.window, "titles": [p["title"] for p in papers]}
        return outcome

    # ---- 以下开始落盘 ----
    # 顺序是刻意的：**本地产出全部写完，最后才碰 Zotero**。
    # Zotero 是本机服务：推送时它可能没开、端口被占、或者主线程正忙，请求会一直等到超时。
    # 以前把它排在最前面，于是它一卡住，这一周的清单、账本、周报就全都写不出来，
    # 用户看到的现象是「阅读清单没有更新」，实际上一个文件都没写。
    # 它只是可选的加分项，不该对本周产出有否决权。
    folder = cfg.ensure_week_dir(week)
    outcome.queue_path = folder / QUEUE_NOTE

    if papers:
        rows = list(context.rows)
        rows.extend(ledger.make_row(paper, week, anchor.isoformat(), paper.get("role") or "") for paper in papers)
        ledger.write_ledger(cfg.ledger_path, rows)

    # 先把这些新行读回来，周报里才能显示「第 n 周推了几篇」。
    context.rows = ledger.read_ledger(cfg.ledger_path)
    context.stats_by_week[week] = ledger.stats(ledger.week_rows(context.rows, week))

    queue_text = render_queue(context, papers, review, lang, outcome.year_window)
    # 空清单不覆盖已有的清单：跟下面周报的处理保持一致，不动正本。
    if papers or not outcome.queue_path.exists():
        outcome.queue_path.write_text(queue_text, encoding="utf-8")

    # 本地已经全部落盘，现在才轮到 Zotero；它的成败只体现为周报里的一句话。
    if papers and zotero_on:
        outcome.zotero = zotero.push_papers(papers, week, lang=lang)
        if not outcome.zotero.get("ok"):
            warnings.append(str(outcome.zotero.get("message") or "Zotero push failed"))
    elif papers and not zotero_on:
        outcome.zotero = {"ok": True, "count": 0, "message": pick(lang, "按你的要求跳过了 Zotero。", "Zotero skipped on request.")}

    extras = {
        "queue_path": _relative(cfg, outcome.queue_path),
        "warnings": warnings,
        "year_window": outcome.year_window,
        # 推送成功时终端里那句「已导入 Zotero n 条」一关窗口就没了，周报是留下来的那份，
        # 所以成败都要写进去（失败的还会额外进第 8 节「需要你知道的」）。
        "zotero_message": str((outcome.zotero or {}).get("message") or ""),
    }
    report_text, block = render_report(context, papers, review, extras, lang, citation_base=base)
    outcome.report_block = block
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    outcome.report_path = cfg.report_path(week)
    if not papers and outcome.report_path.exists():
        # 这一周已经有周报了，而这次没产出清单（检索挂了 / 候选全推过 / 全被引用门槛刷掉）。
        # 一份「什么都没有」的周报不该覆盖掉正本，所以另存一份带时间戳的。
        notice = pick(
            lang,
            "> ⚠ **空跑副本**：这次没有产出清单（检索失败、候选全推过，或者全被引用门槛刷掉了）。"
            "当周的正本是同一目录里文件名不带「空跑」字样的那一份，这份没有覆盖它。",
            "> ⚠ **Empty-run copy**: no list was produced this run (the search failed, every candidate "
            "had already been pushed, or all of them were below the citation floor). The canonical "
            'report for this week is the file in this folder whose name has no "empty run" suffix.',
        )
        # PF:WEEK 块必须去掉：notes.read_pf_blocks 会把同一周的两份都读进来，
        # 全局历史里就会出现两条重复的「第 n 周」。
        report_text = notice + "\n\n" + PF_PATTERN.sub("", report_text).strip() + "\n"
        outcome.report_path = outcome.report_path.with_name(
            f"{outcome.report_path.stem}_空跑-{dt.datetime.now().strftime('%Y%m%d-%H%M')}"
            f"{outcome.report_path.suffix}"
        )
        warnings.append(
            pick(
                lang,
                f"这一周已经有周报了，而这次没有产出清单，所以没有覆盖原来那份，另存为「{outcome.report_path.name}」。",
                f"This week already had a report, and this run produced no list, so nothing was "
                f"overwritten: the result went to \"{outcome.report_path.name}\".",
            )
        )
    outcome.report_path.write_text(report_text, encoding="utf-8")

    if papers:
        mark_run(cfg.state_dir, cfg, anchor)
    else:
        warnings.append(
            pick(
                lang,
                "因为这一周没有产出清单，我没有记录运行时间，下次会重跑同一周（不会漏掉你写的笔记）。",
                "No list was produced, so the run date was not recorded; the next run retries the same week.",
            )
        )
    return outcome


def _relative(cfg: VaultConfig, path: Path) -> str:
    try:
        return str(path.relative_to(cfg.vault))
    except ValueError:
        return str(path)
