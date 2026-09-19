"""账本 ``进度.csv``：一行一篇，只为去重和统计而存在。

你**不需要看它**。它存在的唯一理由是：Zotero 的写入接口不会按 DOI 去重，
没有账本同一篇论文下周会被再推一次、再打一个标签，几周后库里全是重复条目。

两条规矩：

* ``status`` 列由脚本写成 ``待读``；你手改成 ``已读`` / ``跳过`` 之后，
  脚本**再也不会动它**（它只改写自己写下的 ``待读``）。
* ``evidence`` 列是脚本算的启发式值（有几篇笔记提到了这篇）。
  它是猜测，会错，所以脚本把猜的结果打印出来让你核对。
"""

from __future__ import annotations

import csv
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .models import clean_doi

LEDGER_FIELDS = [
    "week",
    "pushed_on",
    "paper_id",
    "title",
    "doi",
    "year",
    "venue",
    "url",
    "role",
    "status",
    "evidence",
]

STATUS_TODO = "待读"
STATUS_READ = "已读"
STATUS_SKIP = "跳过"


def _cell(value: Any) -> str:
    """CSV 单元格：换行/逗号/引号都要能安全落盘，否则 pandas 读不动。"""
    text = str(value if value is not None else "")
    if any(char in text for char in ',"\n\r'):
        return '"' + text.replace('"', '""') + '"'
    return text


def _key(paper_id: str = "", doi: str = "", title: str = "") -> str:
    """一篇论文的身份：DOI 优先，其次取 paperId，最后退到长标题。

    来源都会给 paperId，所以标题兜底几乎用不到；真用到时要求标题足够长，
    免得「Editorial」这类短标题把不相关的论文判成同一篇。

    DOI 一律小写：DOI 本身不区分大小写，但 OpenAlex / Crossref 返回的大小写
    不一致，不归一化就会把同一篇论文当成两篇。
    """
    cleaned = clean_doi(doi).lower()
    if cleaned:
        return f"doi:{cleaned}"
    if (paper_id or "").strip():
        return f"id:{paper_id.strip()}"
    slug = " ".join((title or "").lower().split())
    return f"title:{slug}" if len(slug) >= 20 else ""


def key_of_row(row: Dict[str, Any]) -> str:
    return _key(str(row.get("paper_id") or ""), str(row.get("doi") or ""), str(row.get("title") or ""))


def key_of_paper(paper: Dict[str, Any]) -> str:
    return _key(str(paper.get("paperId") or ""), str(paper.get("doi") or ""), str(paper.get("title") or ""))


def read_ledger(path: Path) -> List[Dict[str, str]]:
    """读账本。表头对不上当前 schema 时备份旧文件并当作空账本重新开始。"""
    path = Path(path)
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = reader.fieldnames or []
            if "paper_id" not in fields or "status" not in fields:
                if path.stat().st_size > 0:
                    backup = path.with_name(path.name + ".bak")
                    if not backup.exists():
                        shutil.copy2(path, backup)
                return []
            return [{field: (row.get(field) or "") for field in LEDGER_FIELDS} for row in reader]
    except OSError:
        return []


def write_ledger(path: Path, rows: Sequence[Dict[str, Any]]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [",".join(LEDGER_FIELDS)]
    for row in rows:
        lines.append(",".join(_cell(row.get(field, "")) for field in LEDGER_FIELDS))
    # newline="" 很重要：标题里偶尔会有换行，不能让 Windows 把它变成 \r\n，
    # 否则读写一圈下来，」已读「的列位会错，去重键也就对不上了。
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


def known_keys(rows: Iterable[Dict[str, Any]]) -> Set[str]:
    """已经推荐过的论文全集——用于「同一篇永远不推荐第二次」。"""
    return {key for key in (key_of_row(row) for row in rows) if key}


def filter_unseen(papers: Sequence[Dict[str, Any]], rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """滤掉推荐过的。算不出身份的论文宁可留下，也不冒丢掉候选的风险。"""
    seen = known_keys(rows)
    return [paper for paper in papers if not key_of_paper(paper) or key_of_paper(paper) not in seen]


def make_row(paper: Dict[str, Any], week: int, when: str, role: str) -> Dict[str, str]:
    return {
        "week": str(week),
        "pushed_on": when,
        "paper_id": str(paper.get("paperId") or ""),
        "title": str(paper.get("title") or ""),
        "doi": str(paper.get("doi") or ""),
        "year": str(paper.get("year") or ""),
        "venue": str(paper.get("venue") or ""),
        "url": str(paper.get("url") or paper.get("open_pdf") or ""),
        "role": role,
        "status": STATUS_TODO,
        "evidence": "0",
    }


def refresh(rows: Sequence[Dict[str, Any]], mentions) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    """更新 ``evidence``，并把脚本自己写的 ``待读`` 翻成 ``已读``。

    ``mentions`` 是 ``Callable[[str], bool]``——通常是 :class:`notes.Corpus`。
    你手写的 ``已读`` / ``跳过`` 一律原样保留。
    """
    out: List[Dict[str, str]] = []
    flipped = 0
    for row in rows:
        item = {field: str(row.get(field) or "") for field in LEDGER_FIELDS}
        hit = 1 if mentions(str(item.get("title") or "")) else 0
        item["evidence"] = str(hit)
        if hit and item.get("status") == STATUS_TODO:
            item["status"] = STATUS_READ
            flipped += 1
        out.append(item)
    return out, {"flipped": flipped, "with_notes": sum(1 for row in out if row["evidence"] == "1")}


def week_rows(rows: Sequence[Dict[str, Any]], week: int) -> List[Dict[str, str]]:
    return [dict(row) for row in rows if str(row.get("week") or "").strip() == str(week)]


def stats(rows: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """推荐 / 已读 / 跳过 / 未标记 的计数。"""
    todo = read = skip = 0
    for row in rows:
        status = (str(row.get("status") or "")).strip()
        if status == STATUS_SKIP:
            skip += 1
        elif status == STATUS_READ:
            read += 1
        else:
            todo += 1
    return {"pushed": len(rows), "read": read, "skipped": skip, "unmarked": todo}
