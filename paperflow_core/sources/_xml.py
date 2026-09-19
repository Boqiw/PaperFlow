"""共享的 XML 工具：arXiv 与 PubMed 都用 XML 返回，需要把嵌套节点压平成文本。"""

from __future__ import annotations

from typing import Any, Optional


def xml_text(element: Optional[Any]) -> str:
    """把 XML 元素展平成去掉多余空白的文本（标题/摘要常被换行拆散）。"""
    if element is None:
        return ""
    return " ".join("".join(element.itertext()).split())
