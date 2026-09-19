"""HTTP 层：4xx 的响应体必须带进异常里，正文类型不能喂错。

这个文件存在的理由是一次真实的调试事故：Zotero 一直回 ``400 Endpoint does
not support method``，但因为这里用的是 ``response.raise_for_status()``（它会把
响应体丢掉），看到的只有 ``400 Client Error: for url: ...``，于是一路往鉴权、
往参数上猜，绕了很远。异常的原文必须留下来。
"""

from __future__ import annotations

import pytest
import requests

from paperflow_core import http_client


class _FakeResponse:
    """够用的假响应：状态码、原因、url、正文。"""

    def __init__(self, status_code: int, text: str = "", reason: str = "Bad Request"):
        self.status_code = status_code
        self.reason = reason
        self.url = "http://localhost:23119/api/users/0/items"
        self.text = text
        self.headers: dict = {}


def test_error_message_carries_the_body():
    response = _FakeResponse(400, "Endpoint does not support method")

    message = http_client.error_message(response)

    assert message.startswith("400 Bad Request for url: http://localhost:23119/api/users/0/items")
    assert "Endpoint does not support method" in message


def test_error_message_flattens_and_caps_the_body():
    """服务端有时回一大坨 HTML，异常信息里要塞得下一行、不超过 400 字。"""
    response = _FakeResponse(500, "  boom\n\n   " * 200, reason="Internal Server Error")

    message = http_client.error_message(response)

    assert "\n" not in message
    assert len(message) < 600


def test_error_message_survives_a_binary_body():
    """解不出文本的响应（二进制）只报状态码，不能在这里再炸一次。"""

    class Binary:
        status_code = 404
        reason = "Not Found"
        url = "https://example.org/blob"
        headers: dict = {}

        @property
        def text(self) -> str:
            raise UnicodeDecodeError("utf-8", b"\x00", 0, 1, "invalid start byte")

    message = http_client.error_message(Binary())

    assert "404 Not Found" in message


def test_a_4xx_raises_with_the_response_body(monkeypatch: pytest.MonkeyPatch):
    """回归防护：这条断言就是当初让 Zotero 的 400 原文消失的那一句。"""
    monkeypatch.setattr(
        http_client.requests, "request", lambda *a, **k: _FakeResponse(400, "Endpoint does not support method")
    )

    with pytest.raises(requests.HTTPError) as excinfo:
        http_client.request("POST", "http://localhost:23119/api/users/0/items", headers={}, timeout=10)

    assert "Endpoint does not support method" in str(excinfo.value)


def test_api_post_json_sends_json_and_leaves_the_response_shape_to_the_caller(monkeypatch: pytest.MonkeyPatch):
    """连接器成功时回的是空正文或一小段纯文本，所以响应要能按文本读，不能强制解析 JSON。"""
    captured: dict = {}

    def fake_request(method, url, *, headers, timeout, **kwargs):
        captured["method"] = method
        captured["headers"] = headers
        captured["kwargs"] = kwargs
        return ""

    monkeypatch.setattr(http_client, "request", fake_request)

    result = http_client.api_post_json(
        "http://localhost:23119/connector/saveItems", {"sessionID": "s"}, parse="text", retry_status=()
    )

    assert result == ""
    assert captured["method"] == "POST"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["kwargs"]["json"] == {"sessionID": "s"}
    assert captured["kwargs"]["parse"] == "text"
    assert captured["kwargs"]["retry_status"] == ()


def test_api_post_json_never_carries_an_api_key(monkeypatch: pytest.MonkeyPatch):
    """这是发往本机 Zotero 的请求，不该顺手把 Semantic Scholar 的 key 一起捎上。"""
    monkeypatch.setenv("S2_API_KEY", "secret")
    captured: dict = {}

    def fake_request(method, url, *, headers, timeout, **kwargs):
        captured["headers"] = headers
        return {}

    monkeypatch.setattr(http_client, "request", fake_request)
    http_client.api_post_json("http://localhost:23119/connector/getSelectedCollection", {})

    assert "x-api-key" not in {key.lower() for key in captured["headers"]}


def test_api_post_bytes_sends_the_pdf_body_unchanged(monkeypatch: pytest.MonkeyPatch):
    """上传 PDF 必须原样发字节：任何编码或包装都会让 Zotero 存下一个打不开的文件。"""
    captured: dict = {}

    def fake_request(method, url, *, headers, timeout, **kwargs):
        captured["headers"] = headers
        captured["kwargs"] = kwargs
        return "Full Text PDF"

    monkeypatch.setattr(http_client, "request", fake_request)

    result = http_client.api_post_bytes(
        "http://localhost:23119/connector/saveAttachment?sessionID=s",
        b"%PDF-1.4\n",
        headers={"Content-Type": "application/pdf"},
    )

    assert result == "Full Text PDF"
    assert captured["kwargs"]["data"] == b"%PDF-1.4\n"
    assert captured["headers"]["Content-Type"] == "application/pdf"


def test_request_can_turn_off_retries_for_one_status(monkeypatch: pytest.MonkeyPatch):
    """回归防护：连接器用 500 表达"这篇没有开放获取副本"这种业务结果。

    默认逻辑把 5xx 当抖动，于是每篇闭源论文都要退避重试好几轮，白等半分钟。
    """
    calls: list = []

    def fake_request(method, url, **kwargs):
        calls.append(url)
        return _FakeResponse(500, "Internal Server Error", reason="Internal Server Error")

    monkeypatch.setattr(http_client.requests, "request", fake_request)

    with pytest.raises(requests.HTTPError):
        http_client.request(
            "POST",
            "http://localhost:23119/connector/saveAttachmentFromResolver",
            headers={},
            timeout=10,
            retry_status=(),
        )

    assert len(calls) == 1


def test_request_can_cap_the_number_of_attempts(monkeypatch: pytest.MonkeyPatch):
    """回归防护：``retry_status=()`` 只关得掉"状态码重试"，连接超时照样会再来四遍。

    本机 Zotero 连接器就是这么被白等十几分钟的：一次 60 秒超时要来 5 遍，
    然后再对下一个端点重复一遍。所以除了超时要能自己给，次数也要能自己限。
    """
    sent: list = []

    def fake_request(method, url, **kwargs):
        sent.append(kwargs)
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(http_client.requests, "request", fake_request)

    with pytest.raises(requests.ConnectionError):
        http_client.request(
            "POST",
            "http://localhost:23119/connector/saveItems",
            headers={},
            timeout=5,
            retries=1,
        )

    assert len(sent) == 1
    assert sent[0]["timeout"] == 5


def test_request_can_return_text(monkeypatch: pytest.MonkeyPatch):
    """连接器的 saveAttachment 成功时回的是附件标题（比如「全文」），不是 JSON。"""
    monkeypatch.setattr(
        http_client.requests, "request", lambda *a, **k: _FakeResponse(201, "全文", reason="Created")
    )

    assert http_client.request("POST", "http://localhost:23119/x", headers={}, timeout=10, parse="text") == "全文"
