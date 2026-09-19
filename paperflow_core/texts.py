"""语言工具：所有用户可见文本都通过 ``pick()`` 选择中英文。

设计取舍
--------
LLM prompt 的"骨架"统一用英文书写（模型对英文指令的理解最稳定），
只在 prompt 末尾追加一行 ``language_rule()`` 强制输出语言。
这样避免为每种语言维护两份 prompt，同时保证产物是中文。
"""

from __future__ import annotations

DEFAULT_LANG = "zh"
SUPPORTED_LANGS = ("zh", "en")


def normalise_lang(value: str | None) -> str:
    """把任意输入归一化成受支持的语言代码。"""
    code = (value or "").strip().lower()
    if code.startswith("zh"):
        return "zh"
    if code.startswith("en"):
        return "en"
    return DEFAULT_LANG


def pick(lang: str, zh: str, en: str) -> str:
    """按语言选择脚手架文本（标题、说明、表头等）。"""
    return zh if normalise_lang(lang) == "zh" else en


def language_rule(lang: str) -> str:
    """追加到 prompt 末尾的输出语言强制规则。"""
    if normalise_lang(lang) == "zh":
        return (
            "IMPORTANT: Write every human-readable string value in the JSON (and any prose) "
            "in Simplified Chinese (简体中文). Keep JSON keys, paper IDs, DOIs, URLs, and "
            "concept ids in their original ASCII form."
        )
    return "IMPORTANT: Write all human-readable string values in English. Keep JSON keys, paper IDs, DOIs, URLs, and concept ids in their original ASCII form."
