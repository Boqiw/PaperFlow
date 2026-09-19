"""LLM 客户端：一次请求 + 多轮对话，支持注入以便离线测试。

设计要点
--------
* 客户端是**可注入**的：所有需要 AI 的函数都接收 ``client`` 参数。
  测试里用假客户端（不发网络请求），生产环境用 :func:`get_client`。
* 选论文这类「必须有 AI 才有意义」的步骤，拿不到结果时**上报错误**而不是自己编，
  见 :meth:`LLMClient.json_ex`。
* 输出语言由 prompt 末尾的 ``language_rule()`` 控制，不需要维护两份 prompt。
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

import requests

from .config import llm_settings
from .texts import language_rule

DEFAULT_SYSTEM = (
    "You are a cautious research mentor for a beginner entering computational neuroscience. "
    "You ground every claim in the provided context and never invent papers, DOIs, numbers, or facts. "
    "When information is missing you say so explicitly instead of guessing. "
    "You return valid JSON only when JSON is requested."
)


class LLMError(RuntimeError):
    """LLM 调用失败（网络、鉴权、响应格式）。调用方应该把它变成一次明确的报错。"""


def llm_failure_reason(error: str, raw: str) -> str:
    """把「AI 没给出可用 JSON」写成一句人能读的话，并附上模型回答的开头。"""
    preview = " ".join((raw or "").split())[:240]
    if error and preview:
        return f"{error}（模型回答开头：{preview}）"
    if error:
        return error
    return f"模型没有返回可解析的 JSON。模型回答开头：{preview or '（空回答）'}"


def strip_code_fence(content: str) -> str:
    """去掉模型习惯性包上的 ```json ... ``` 围栏。"""
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", (content or "").strip(), flags=re.I)


def parse_json_object(content: str) -> Optional[Dict[str, Any]]:
    """尽量从模型输出里取出**第一个** JSON 对象。

    模型有三个常见坏习惯：包 ``` 围栏、在 JSON 后面再追加一段解释、
    或者把两个对象直接拼在一起。严格 ``json.loads`` 碰到后两种会直接失败，
    所以这里退化成「从头找第一个能解析成功的 ``{``」，多出来的内容丢掉。
    """
    text = strip_code_fence(content)
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    index = text.find("{")
    while index != -1:
        try:
            data, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            index = text.find("{", index + 1)
            continue
        if isinstance(data, dict):
            return data
        index = text.find("{", index + 1)
    return None


class LLMClient:
    """OpenAI 兼容的 chat completions 客户端。"""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
        timeout: int = 120,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        json_mode: bool = False,
        system: Optional[str] = None,
    ) -> str:
        """发送一轮对话，返回 assistant 的文本内容。"""
        if not self.available:
            raise LLMError("LLM_API_KEY is not set")
        payload: Dict[str, Any] = {
            "model": self.model,
            "temperature": temperature,
            "messages": ([{"role": "system", "content": system or DEFAULT_SYSTEM}] if system != "" else []) + messages,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            response = requests.post(
                self.base_url + "/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"] or ""
        except requests.RequestException as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"Unexpected LLM response shape: {exc}") from exc

    def json_ex(
        self, prompt: str, temperature: float = 0.1, system: Optional[str] = None
    ) -> Tuple[Optional[Dict[str, Any]], str, str]:
        """请求 JSON，返回 ``(解析结果, 模型原始回答, 失败原因)``。

        成功时后两项没用；失败时解析结果是 None，调用方可以把失败原因原样写进报错，
        这样「AI 挂了」不是一句空话，而是能看到模型到底回了什么。
        """
        if not self.available:
            return None, "", "LLM_API_KEY is not set"
        try:
            content = self.chat(
                [{"role": "user", "content": prompt}],
                temperature=temperature,
                json_mode=True,
                system=system if system is not None else DEFAULT_SYSTEM + " Return valid JSON only.",
            )
        except LLMError as exc:
            return None, "", str(exc)
        data = parse_json_object(content)
        if data is None:
            return None, content or "", ""
        return data, content or "", ""

    def json(self, prompt: str, temperature: float = 0.1, system: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """请求 JSON 并解析；失败返回 None，并把原因打到标准错误。"""
        data, raw, error = self.json_ex(prompt, temperature=temperature, system=system)
        if data is None:
            print(f"Warning: {llm_failure_reason(error, raw)}", file=sys.stderr)
        return data

    def json_or(self, prompt: str, fallback: Dict[str, Any], temperature: float = 0.1) -> Dict[str, Any]:
        """请求 JSON，失败时返回 ``fallback`` —— 让调用方少写一层判断。"""
        result = self.json(prompt, temperature=temperature)
        return result if result is not None else fallback


def get_client() -> LLMClient:
    """按环境变量构造客户端（未配置 key 时 ``available`` 为 False）。"""
    key, base, model = llm_settings()
    return LLMClient(api_key=key, base_url=base, model=model)


def build_prompt(body: str, lang: str) -> str:
    """给 prompt 追加输出语言规则。"""
    return body.rstrip() + "\n\n" + language_rule(lang)
