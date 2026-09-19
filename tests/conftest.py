"""pytest 共用夹具。

这里的测试全部**离线**运行：不联网、不调用 LLM、不写仓库里的真实状态目录。
每个用例要用目录时都用 ``tmp_path``，避免污染 ``paperflow_state/``。
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path

import pytest

#: 仓库根目录。pytest 会把 tests/ 放进 sys.path，但不会把根目录放进去，
#: 所以这里手工补一下，让 `import paperflow_core` 生效。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 会被 autouse 夹具清空的变量 = 开发机 ``.env`` 里可能出现的所有 key。
#:
#: ``cli.main()`` 会加载仓库根目录的 ``.env``，所以测试**确实**会读到它；而
#: ``config.load_dotenv`` 只填「当前不存在」的变量，因此只要这里把它们设成空串，
#: ``.env`` 就进不来。空串不是随便选的：各处的写法（``config.default_vault``、
#: ``config.llm_settings``、``zotero.use_connector``）都是 ``(os.getenv(...) or "").strip()``，
#: 空串等同于「没配」。
#:
#: 代价与义务：往 ``.env`` 里加新 key 时，必须同时加到这个元组里，否则那条 key
#: 会悄悄影响测试结果（已经踩过一次：``PAPERFLOW_VAULT`` 让
#: ``test_init_without_a_vault_asks_for_one`` 读到真实库路径而失败）。
_ISOLATED_ENV_VARS = (
    # LLM：空 key 代表「没配模型」。现在没配模型时「周循环」会直接报错退出，
    # 所以测试必须显式注入假模型（见 FakeLLM / llm 夹具），测试本身不联网。
    "LLM_API_KEY",
    "OPENAI_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
    # 库/状态目录：指向开发机真实路径会让 init 意外成功、并可能写进真实库。
    "PAPERFLOW_VAULT",
    "PAPERFLOW_STATE_DIR",
    "PAPERFLOW_LANG",
    # Zotero：开着 ZOTERO_LOCAL 会让推送走连接器分支，改变被测行为的走向。
    "ZOTERO_LOCAL",
    "ZOTERO_API_URL",
    "ZOTERO_CONNECTOR_URL",
    "ZOTERO_USER_ID",
    "ZOTERO_API_KEY",
    # 检索：带上真实 key 会让请求真的打到 Semantic Scholar。
    "S2_API_KEY",
    "CONTACT_EMAIL",
    "OPENALEX_MAILTO",
    "NCBI_EMAIL",
    "NCBI_API_KEY",
)

#: 第 1 周的周一。固定值，否则「第几周」的断言会随真实日期漂移。
START = dt.date(2026, 9, 21)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清空开发机 ``.env`` 可能提供的所有配置，强制每个用例不联网。"""
    for name in _ISOLATED_ENV_VARS:
        monkeypatch.setenv(name, "")


class FakeLLM:
    """不联网的假模型。

    两种用法：

    * ``FakeLLM({...})`` —— 按给定 payload 回答，用来测「模型乱说」的各种情况；
    * ``FakeLLM()`` —— **照单全收**：从 prompt 里把候选的 ``paperId`` 读出来，
      按顺序挑前 ``picks`` 篇。周循环的用例靠它拿到一份「像 AI 挑的」清单。
    """

    def __init__(self, payload: object = None, picks: int = 12) -> None:
        self.payload = payload
        self.picks = picks
        self.prompts: list = []

    @property
    def available(self) -> bool:
        return True

    @staticmethod
    def paper_ids(prompt: str) -> list:
        """把 prompt 里 ``[i] paperId=xxx`` 的 id 按出现顺序抠出来。"""
        return re.findall(r"paperId=(\S+)", prompt or "")

    def json_ex(self, prompt: str, temperature: float = 0.1, system=None):
        self.prompts.append(prompt)
        if self.payload is not None:
            return self.payload, "", ""
        ids = self.paper_ids(prompt)[: self.picks]
        data = {
            "papers": [
                {
                    "paperId": paper_id,
                    "role": "deep" if index < 2 else "skim",
                    "track": "target" if index < 2 else "explore",
                    "why": f"命中长期目标（{paper_id}）",
                    "gain": "能在组会上讲清",
                }
                for index, paper_id in enumerate(ids)
            ],
            "convergence": "收敛到一个可复现的最小流程",
            "uncovered": "闭环调控",
            "open_questions": ["怎么选状态数 K"],
            "notes_digest": [],
        }
        return data, json.dumps(data, ensure_ascii=False), ""

    def json(self, prompt: str, temperature: float = 0.1, system=None):
        return self.json_ex(prompt, temperature)[0]


@pytest.fixture
def llm():
    """一个可用的假模型：它只会挑候选表里真实存在的论文。"""
    return FakeLLM()


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """空的 Obsidian 库根目录。"""
    path = tmp_path / "vault"
    path.mkdir()
    return path


@pytest.fixture
def goals(vault: Path) -> Path:
    """已写好的长期目标文件夹。"""
    folder = vault / "长期目标"
    folder.mkdir()
    (folder / "长期目标.md").write_text(
        "# 长期目标\n\n"
        "1. 能说清 metastability 和 criticality 的区别，并知道怎么从 EEG 里估计它们。\n"
        "2. 能复现一个静息态脑状态（微状态 / HMM）的分割流程，并解释每一步的假设。\n"
        "3. 能设计一个闭环刺激实验的最小可行方案。\n",
        encoding="utf-8",
    )
    return folder


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """一个空的 PaperFlow 状态目录。"""
    path = tmp_path / "PaperFlow"
    path.mkdir()
    return path


@pytest.fixture
def cfg(vault: Path, goals: Path, state_dir: Path):
    """一份绑定了状态目录的 VaultConfig。"""
    from paperflow_core import vault as vault_mod
    from paperflow_core.vault import VaultConfig

    config = VaultConfig(vault=vault, goals=goals, start_date=START, per_week=12)
    return vault_mod.bind(config, state_dir)


@pytest.fixture
def paper():
    """一篇结构完整的假论文，字段和 search.py 产出的保持一致。"""

    def _make(**overrides):
        base = {
            "paperId": "s2:abc123",
            "title": "Metastability in resting-state brain dynamics",
            "abstract": "We estimate metastability from resting-state EEG using a hidden Markov model.",
            "year": 2023,
            "venue": "NeuroImage",
            "doi": "10.1000/xyz",
            "url": "https://example.org/paper",
            "open_pdf": "https://example.org/paper.pdf",
            "citations": 42,
            "citationCount": 42,
            "influentialCitationCount": 3,
            "authors": ["A. Author", "B. Author"],
            "source": "openalex",
        }
        base.update(overrides)
        return base

    return _make


def write_week_note(cfg, week: int, name: str, text: str) -> Path:
    """在某一周文件夹里放一份笔记，返回路径。"""
    folder = cfg.ensure_week_dir(week)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


def write_report_block(cfg, week: int, **payload) -> Path:
    """手写一份带 ``<!-- PF:WEEK ... -->`` 块的周报，用来构造「历史」。

    必须是真块（不是裸 JSON），否则 ``notes.read_pf_blocks`` 读不到——
    这个格式就是全局回顾的唯一入口。
    """
    from paperflow_core.notes import render_pf_block
    from paperflow_core.vault import week_label

    start, _ = cfg.week_bounds(week)
    cfg.week_report_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.week_report_dir / f"{start.isoformat()}_{week_label(week)}.md"
    block = {
        "week": week,
        "window": "",
        "query": "",
        "themes": "",
        "titles": [],
        "uncovered": "",
        "mode": "llm",
    }
    block.update(payload)
    path.write_text("# 周报\n\n正文随便写点。\n\n" + render_pf_block(block) + "\n", encoding="utf-8")
    return path
