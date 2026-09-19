"""LLM 输出解析与失败路径测试。

模型有三个坏习惯：包 ``` 围栏、在 JSON 后面追加解释、把两个对象拼在一起。
严格 ``json.loads`` 在真实运行里直接抛过 ``JSONDecodeError``，所以这里把
:func:`parse_json_object` 的容错行为全部钉住。

另一半测的是「AI 挂了」怎么报：:meth:`LLMClient.json_ex` 返回的失败原因必须是
一句人能读的话，因为周循环会把它原样写进报错里（没有 AI 就不产出清单）。
"""

from __future__ import annotations

import pytest

from paperflow_core.llm import LLMClient, LLMError, llm_failure_reason, parse_json_object, strip_code_fence


def test_strip_code_fence_removes_json_fence():
    assert strip_code_fence('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_code_fence("```\n{}\n```") == "{}"
    assert strip_code_fence('{"a": 1}') == '{"a": 1}'
    assert strip_code_fence("") == ""


def test_parse_plain_object():
    assert parse_json_object('{"a": 1}') == {"a": 1}


def test_parse_fenced_object():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_object_with_trailing_prose():
    content = '{"recommendations": []}\n\nHope this helps! Let me know if you need more.'
    assert parse_json_object(content) == {"recommendations": []}


def test_parse_first_object_when_two_are_concatenated():
    content = '{"a": 1}{"b": 2}'
    assert parse_json_object(content) == {"a": 1}


def test_parse_object_after_leading_prose_and_stray_braces():
    content = 'Sure! Use {curly} braces like this: {"a": 1}'
    assert parse_json_object(content) == {"a": 1}


def test_parse_returns_none_for_non_object_json():
    assert parse_json_object("[1, 2, 3]") is None
    assert parse_json_object('"just a string"') is None
    assert parse_json_object("42") is None


def test_parse_returns_none_for_garbage():
    assert parse_json_object("") is None
    assert parse_json_object("   ") is None
    assert parse_json_object("no json here at all") is None
    # 有花括号但内容永远解析不出来。
    assert parse_json_object("{not json}") is None


def test_parse_handles_nested_and_unicode():
    content = '```json\n{"理由": "因为 \\"状态转移\\" 很关键", "ids": ["a", "b"]}\n```'
    parsed = parse_json_object(content)
    assert parsed is not None
    assert parsed["ids"] == ["a", "b"]
    assert "状态转移" in parsed["理由"]


# --------------------------------------------------------------- 失败路径


def test_json_ex_without_a_key_says_why():
    """没配 key 不是异常，是一个可解释的失败：三元组后两项都给出原因。"""
    client = LLMClient(api_key="")
    assert client.available is False

    data, raw, error = client.json_ex("hi")

    assert data is None
    assert raw == ""
    assert error == "LLM_API_KEY is not set"


def test_chat_without_a_key_raises(monkeypatch):
    client = LLMClient(api_key="")
    with pytest.raises(LLMError):
        client.chat([{"role": "user", "content": "hi"}])


def test_json_ex_reports_an_unparseable_answer(monkeypatch):
    """模型能连上、但回了一堆废话：raw 要留着，好让用户看见它到底回了什么。"""
    client = LLMClient(api_key="k")
    monkeypatch.setattr(client, "chat", lambda *a, **k: "I am not going to answer that.")

    data, raw, error = client.json_ex("hi")

    assert data is None
    assert raw == "I am not going to answer that."
    assert error == ""


def test_json_ex_reports_a_transport_failure(monkeypatch):
    client = LLMClient(api_key="k")

    def boom(*args, **kwargs):
        raise LLMError("LLM request failed: connection reset")

    monkeypatch.setattr(client, "chat", boom)

    data, raw, error = client.json_ex("hi")

    assert data is None
    assert "connection reset" in error


def test_json_ex_returns_the_parsed_object(monkeypatch):
    client = LLMClient(api_key="k")
    monkeypatch.setattr(client, "chat", lambda *a, **k: '```json\n{"papers": []}\n```')

    data, raw, error = client.json_ex("hi")

    assert data == {"papers": []}
    assert error == ""


def test_llm_failure_reason_quotes_the_model():
    reason = llm_failure_reason("", "  Sure!\n Here is the JSON:  ")
    assert "Sure! Here is the JSON:" in reason
    assert "\n" not in reason


def test_llm_failure_reason_keeps_the_transport_error():
    assert llm_failure_reason("LLM request failed: 429", "") == "LLM request failed: 429"
    assert "429" in llm_failure_reason("LLM request failed: 429", "oops")


def test_llm_failure_reason_handles_an_empty_answer():
    assert "空回答" in llm_failure_reason("", "")
    assert "空回答" in llm_failure_reason("", "   ")


def test_llm_failure_reason_truncates_a_huge_answer():
    assert len(llm_failure_reason("", "x" * 5000)) < 320
