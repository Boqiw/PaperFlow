"""语言工具测试：中英切换与归一化。

``normalise_lang`` 必须对不认识的输入回落默认语言而不是抛错：
``--lang`` 的值是直通进来的，抛错会把一次正常的每周运行变成崩溃。
"""

from __future__ import annotations

from paperflow_core import texts


def test_normalise_lang_accepts_prefixes_and_falls_back():
    assert texts.normalise_lang("zh") == "zh"
    assert texts.normalise_lang("zh-CN") == "zh"
    assert texts.normalise_lang("ZH") == "zh"
    assert texts.normalise_lang("en-US") == "en"
    assert texts.normalise_lang("EN") == "en"
    # 不认识的输入回落默认语言，而不是抛错。
    assert texts.normalise_lang("fr") == "zh"
    assert texts.normalise_lang(None) == "zh"
    assert texts.normalise_lang("   ") == "zh"


def test_pick_selects_by_language():
    assert texts.pick("zh", "中文", "english") == "中文"
    assert texts.pick("en", "中文", "english") == "english"
    # 归一化之后仍然生效。
    assert texts.pick("en-GB", "中文", "english") == "english"


def test_language_rule_forces_target_language():
    zh_rule = texts.language_rule("zh")
    assert "Simplified Chinese" in zh_rule
    assert "JSON keys" in zh_rule
    en_rule = texts.language_rule("en")
    assert "Simplified Chinese" not in en_rule
    assert "English" in en_rule
