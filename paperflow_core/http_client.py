"""HTTP 层：统一的请求、重试与限流处理。

文件名刻意不叫 ``http.py``：那会遮蔽标准库的 ``http`` 包，是个常见且难排查的坑。
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlparse

import requests

from .config import user_agent

MAX_RETRIES = 5
RETRY_STATUS = {429, 500, 502, 503, 504}


def retry_after_seconds(response: Any, attempt: int) -> float:
    """优先遵守服务端 ``Retry-After``，否则指数退避（上限 30 秒）。"""
    header = (getattr(response, "headers", {}) or {}).get("Retry-After") if response is not None else None
    if header:
        try:
            return min(float(header), 60.0)
        except (TypeError, ValueError):
            pass
    return min(2.0 * (2 ** (attempt - 1)), 30.0)


def error_message(response: Any) -> str:
    """把响应体带进异常信息里。

    踩过的坑：以前这里直接用 ``response.raise_for_status()``，而它会把响应体丢掉，
    于是 Zotero 回的 ``400 Endpoint does not support method`` 一个字都看不到，
    只能对着 "400 Client Error" 猜。4xx 的原文必须留下来。
    """
    reason = str(getattr(response, "reason", "") or "").strip()
    status = f"{response.status_code} {reason}".strip()
    message = f"{status} for url: {response.url}"
    try:
        detail = " ".join((response.text or "").split())
    except Exception:  # 二进制响应解不出文本，那就只报状态码
        detail = ""
    if detail:
        message += f" | {detail[:400]}"
    return message


def request(
    method: str,
    url: str,
    *,
    headers: Dict[str, str],
    timeout: int,
    parse: str = "json",
    retry_status: Optional[Iterable[int]] = None,
    retries: Optional[int] = None,
    **kwargs: Any,
) -> Any:
    """执行请求，对 429/5xx 与网络抖动做退避重试。

    ``parse`` 取 ``json`` / ``text`` / ``bytes``——别的一律当 json 解析。
    ``retry_status`` 覆盖默认的重试状态码集合：Zotero 连接器用 ``500`` 表示
    "这个条目没有开放获取副本"，那不是抖动，重试只会白等半分钟，所以传空集合关掉。
    ``retries`` 覆盖总的尝试次数（默认 :data:`MAX_RETRIES`）。它管的是**所有**重试，
    超时也算：``retry_status=()`` 只关得掉"状态码重试"，连接超时照样会再来四遍。
    本机 Zotero 连接器就是"几秒不回等于没在工作"的服务，那边一律传 ``1``。
    """
    statuses = RETRY_STATUS if retry_status is None else set(retry_status)
    attempts = MAX_RETRIES if retries is None else max(1, int(retries))
    last_exc: Optional[Exception] = None
    host = urlparse(url).netloc or url
    for attempt in range(1, attempts + 1):
        try:
            response = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            # 网络抖动同样值得重试。
            last_exc = exc
            if attempt == attempts:
                raise
            delay = retry_after_seconds(None, attempt)
            print(f"Warning: {exc}. Retrying in {delay:.0f}s ({attempt}/{attempts - 1}).", file=sys.stderr)
            time.sleep(delay)
            continue
        if response.status_code in statuses:
            delay = retry_after_seconds(response, attempt)
            last_exc = requests.HTTPError(
                f"{response.status_code} Client Error: for url: {response.url}", response=response
            )
            if attempt == attempts:
                raise last_exc
            print(
                f"Warning: HTTP {response.status_code} from {host}. Retrying in {delay:.0f}s "
                f"({attempt}/{attempts - 1}).",
                file=sys.stderr,
            )
            time.sleep(delay)
            continue
        if response.status_code >= 400:
            raise requests.HTTPError(error_message(response), response=response)
        if parse == "bytes":
            return response.content
        if parse == "text":
            return response.text
        return response.json()
    raise last_exc if last_exc else RuntimeError("request failed without a response")


def api_get(url: str, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> Any:
    """GET JSON；自动附带 Semantic Scholar 的 API key（若已配置）。"""
    h = {"User-Agent": user_agent()}
    if os.getenv("S2_API_KEY"):
        h["x-api-key"] = os.environ["S2_API_KEY"]
    if headers:
        h.update(headers)
    return request("GET", url, headers=h, timeout=45, params=params)


def api_get_bytes(url: str, params: Optional[Dict[str, Any]] = None) -> bytes:
    """获取原始 XML；arXiv 与 NCBI E-utilities 返回 XML 而非 JSON。"""
    return request(
        "GET",
        url,
        headers={"User-Agent": user_agent(), "Accept": "*/*"},
        timeout=45,
        parse="bytes",
        params=params,
    )


def api_post(
    url: str,
    payload: Any,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 60,
    retries: Optional[int] = None,
) -> Any:
    h = {"User-Agent": user_agent(), "Content-Type": "application/json"}
    if os.getenv("S2_API_KEY"):
        h["x-api-key"] = os.environ["S2_API_KEY"]
    if headers:
        h.update(headers)
    return request("POST", url, headers=h, timeout=timeout, params=params, json=payload, retries=retries)


def api_post_json(
    url: str,
    payload: Any,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    parse: str = "json",
    retry_status: Optional[Iterable[int]] = None,
    timeout: int = 60,
    retries: Optional[int] = None,
) -> Any:
    """POST 一段 JSON，**不附带任何 API key**，而且响应可以按文本读。

    给 Zotero 的连接器接口用。两处和上面的 :func:`api_post` 不一样，都是踩出来的：

    * 它成功时回的是**空正文**或一小段纯文本（不是 JSON），用 ``parse="json"``
      会直接抛 ``JSONDecodeError``；
    * 它用 ``500`` 表达"这个条目找不到开放获取副本"这种**业务结果**，默认的
      "500 就退避重试"只会白等半分钟，所以要把 ``retry_status`` 传成空集合。
    """
    h = {"User-Agent": user_agent(), "Content-Type": "application/json"}
    if headers:
        h.update(headers)
    return request(
        "POST",
        url,
        headers=h,
        timeout=timeout,
        params=params,
        json=payload,
        parse=parse,
        retry_status=retry_status,
        retries=retries,
    )


def api_post_bytes(
    url: str,
    data: bytes,
    headers: Optional[Dict[str, str]] = None,
    parse: str = "text",
    timeout: int = 300,
    retries: Optional[int] = None,
) -> Any:
    """POST 原始字节（这里是上传 PDF 全文），响应按文本读。

    给连接器的 ``saveAttachment`` 用：那边要的是 PDF 二进制本体，成功时回
    ``201`` + 一小段纯文本（附件标题）。默认超时给得很宽，因为几 MB 的文件加上
    Zotero 那边落盘可能要几十秒；但它同样是本机连接器，所以实际调用点会收紧。
    """
    h = {"User-Agent": user_agent(), "Content-Type": "application/octet-stream"}
    if headers:
        h.update(headers)
    return request("POST", url, headers=h, timeout=timeout, data=data, parse=parse, retries=retries)
