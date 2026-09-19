"""库布局与周编号：把「第 n 周」翻译成真实文件夹。

所有路径都从 ``config.json`` 里的三个值推导：

* ``vault``      Obsidian 库根目录
* ``goals``      长期目标（文件或文件夹）。你自己维护，脚本只读、从不改写
* ``start_date`` 第 1 周的周一。定下来就别改，否则「第 n 周」全部错位

目录布局::

    D:\\Obsidian\\MyVault\\            <- vault
    ├── 长期目标\\                     <- goals（你自己维护）
    │   └── 长期目标.md
    ├── 2026-09-21_09-27\\             <- 第 1 周，装这一周的笔记
    │   ├── 阅读清单.md                <- 脚本写
    │   └── （你的笔记.md，随便放）
    └── PaperFlow\\                    <- state dir
        ├── config.json
        ├── 进度.csv                   <- 内部账本，只用来去重
        ├── 本周.md                    <- 可选，一句话说你现在卡在哪
        ├── 周报\\2026-09-21_第1周.md
        └── 月报\\2026-09.md

周文件夹名以**完整日期**开头（``2026-09-21_09-27``），所以按文件名排序
永远等于按时间排序——跨年也不会乱。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .ranking import CITATION_BASE

CONFIG_JSON = "config.json"
LEDGER_CSV = "进度.csv"
WEEK_REPORT_DIR = "周报"
MONTH_REPORT_DIR = "月报"
WEEK_NOTE = "本周.md"
QUEUE_NOTE = "阅读清单.md"

#: 每周推荐几篇。12 篇 = 5 篇精读 + 7 篇泛读（精读槽位见
#: :func:`paperflow_core.weekly.default_deep_slots`）。
DEFAULT_PER_WEEK = 12
DEFAULT_NOTES_GLOB = "*.md"


def week_label(week: int) -> str:
    """Zotero 标签与报告里统一用的周名：``第1周``（不加零填充）。"""
    return f"第{week}周"


@dataclass
class VaultConfig:
    """``config.json`` 的内存形式。"""

    vault: Path
    goals: Path
    start_date: dt.date
    per_week: int = DEFAULT_PER_WEEK
    last_run: Optional[dt.date] = None
    notes_glob: str = DEFAULT_NOTES_GLOB
    #: 引用门槛基准（见 :func:`paperflow_core.ranking.citation_floor`）。0 = 不设门槛。
    citation_base: int = CITATION_BASE

    # ------------------------------------------------------------- 序列化
    def to_dict(self) -> Dict[str, object]:
        return {
            "vault": str(self.vault),
            "goals": str(self.goals),
            "start_date": self.start_date.isoformat(),
            "per_week": self.per_week,
            "last_run": self.last_run.isoformat() if self.last_run else "",
            "notes_glob": self.notes_glob,
            "citation_base": self.citation_base,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, object]) -> "VaultConfig":
        vault = Path(str(payload["vault"])).expanduser()
        goals = Path(str(payload["goals"])).expanduser()
        if not goals.is_absolute():
            goals = vault / goals
        raw_last = str(payload.get("last_run") or "").strip()
        # 0 是合法值（表示关掉引用门槛），所以只能对「键不存在」用默认值。
        raw_floor = payload.get("citation_base")
        citation_base = CITATION_BASE if raw_floor in (None, "") else int(raw_floor)
        return cls(
            vault=vault,
            goals=goals,
            start_date=dt.date.fromisoformat(str(payload["start_date"])),
            per_week=int(payload.get("per_week") or DEFAULT_PER_WEEK),
            last_run=dt.date.fromisoformat(raw_last[:10]) if raw_last else None,
            notes_glob=str(payload.get("notes_glob") or DEFAULT_NOTES_GLOB),
            citation_base=citation_base,
        )

    # --------------------------------------------------------------- 周编号
    def week_of(self, when: dt.date) -> int:
        """``when`` 属于第几周。早于 ``start_date`` 返回 0（还没开始）。"""
        if when < self.start_date:
            return 0
        return (when - self.start_date).days // 7 + 1

    def week_bounds(self, week: int) -> Tuple[dt.date, dt.date]:
        start = self.start_date + dt.timedelta(days=(week - 1) * 7)
        return start, start + dt.timedelta(days=6)

    def folder_name(self, week: int) -> str:
        """``2026-09-21_09-27``——完整起始日期 + 结束月日。"""
        start, end = self.week_bounds(week)
        return f"{start.isoformat()}_{end.strftime('%m-%d')}"

    def week_dir(self, week: int) -> Path:
        return self.vault / self.folder_name(week)

    def ensure_week_dir(self, week: int) -> Path:
        path = self.week_dir(week)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def current_week(self, today: Optional[dt.date] = None) -> int:
        """今天的周号，但**不返回 0**：开始之前跑，就当作在准备第 1 周。"""
        return max(1, self.week_of(today or dt.date.today()))

    # ------------------------------------------------------------------ 目标
    def goal_files(self) -> List[Path]:
        """``goals`` 是文件夹就读里面所有 ``.md``，是文件就读那一个。"""
        if self.goals.is_dir():
            return sorted(path for path in self.goals.rglob("*.md") if path.is_file())
        return [self.goals] if self.goals.exists() else []

    def read_goals(self) -> str:
        chunks: List[str] = []
        for path in self.goal_files():
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                chunks.append(f"### {path.name}\n\n{text}")
        return "\n\n".join(chunks)

    # ------------------------------------------------------------------ 笔记
    def notes_in(self, week: int) -> List[Path]:
        """某一周文件夹里的笔记，排除脚本自己写的 ``阅读清单.md``。"""
        folder = self.week_dir(week)
        if not folder.is_dir():
            return []
        return sorted(
            path
            for path in folder.glob(self.notes_glob)
            if path.is_file() and path.name != QUEUE_NOTE
        )

    def notes_weeks(self, today: Optional[dt.date] = None, full: bool = False) -> List[int]:
        """要读哪些周的笔记。

        "自上次跑 ``week`` 以来" —— 跳了一周没跑，它自动把两周的笔记都吃掉，
        不会漏。``full=True`` 时无条件读全程。
        """
        last = self.current_week(today)
        if full or self.last_run is None:
            return list(range(1, last + 1))
        first = max(1, self.week_of(self.last_run))
        return list(range(first, last + 1))

    # ------------------------------------------------------------------ 路径
    @property
    def reports_dir(self) -> Path:
        return self.week_report_dir

    @property
    def week_report_dir(self) -> Path:
        return self._state / WEEK_REPORT_DIR

    @property
    def month_report_dir(self) -> Path:
        return self._state / MONTH_REPORT_DIR

    @property
    def ledger_path(self) -> Path:
        return self._state / LEDGER_CSV

    @property
    def week_note_path(self) -> Path:
        return self._state / WEEK_NOTE

    def report_path(self, week: int) -> Path:
        start, _ = self.week_bounds(week)
        return self.week_report_dir / f"{start.isoformat()}_{week_label(week)}.md"

    def month_report_path(self, label: str) -> Path:
        return self.month_report_dir / f"{label}.md"

    # ``_state`` / ``state_dir`` 由下面两个构造函数注入，不写进 JSON。
    _state: Path = field(default=Path("."), repr=False, compare=False)

    @property
    def state_dir(self) -> Path:
        return self._state


def bind(cfg: VaultConfig, state_dir: Path) -> VaultConfig:
    """把状态目录绑到配置上（``state_dir`` 不进 JSON，避免路径重复）。"""
    cfg._state = Path(state_dir)
    return cfg


def config_path(state_dir: Path) -> Path:
    return Path(state_dir) / CONFIG_JSON


def save_config(state_dir: Path, cfg: VaultConfig) -> Path:
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = config_path(state_dir)
    path.write_text(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_config(state_dir: Path) -> Optional[VaultConfig]:
    """读 ``config.json``；不存在或读不动时返回 ``None``（调用方提示跑 init）。"""
    path = config_path(state_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, KeyError, ValueError):
        return None
    if not isinstance(payload, dict) or "vault" not in payload or "start_date" not in payload:
        return None
    try:
        return bind(VaultConfig.from_dict(payload), state_dir)
    except (KeyError, ValueError):
        return None


def mark_run(state_dir: Path, cfg: VaultConfig, when: Optional[dt.date] = None) -> Path:
    """记下这次跑完的日期，下一次就知道该从哪一周开始读笔记。"""
    cfg.last_run = when or dt.date.today()
    return save_config(state_dir, cfg)
