"""配置：环境变量、路径默认值、联系邮箱与 User-Agent。

这里集中所有"读环境"的逻辑，其它模块只调用函数，不直接碰 ``os.environ``。
好处是测试时只需要改环境变量，无需打补丁。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from .texts import DEFAULT_LANG, normalise_lang

DEFAULT_STATE_DIR = "paperflow_state"

ENV_LANG = "PAPERFLOW_LANG"
ENV_STATE_DIR = "PAPERFLOW_STATE_DIR"
ENV_VAULT = "PAPERFLOW_VAULT"


def default_state_dir() -> str:
    """状态目录的默认值：``PAPERFLOW_STATE_DIR`` > ``paperflow_state``。

    把这一行写进 ``.env``（例如 ``PAPERFLOW_STATE_DIR=D:\\Obsidian\\MyVault\\PaperFlow``）
    之后，每条命令都不用再带 ``--state-dir``。
    """
    return (os.getenv(ENV_STATE_DIR) or "").strip() or DEFAULT_STATE_DIR


def default_vault() -> str:
    """Obsidian 库根目录的默认值：``PAPERFLOW_VAULT`` > 空。

    ``init`` 会用它当 ``--vault`` 的默认值；没设就必须显式传 ``--vault``。
    """
    return (os.getenv(ENV_VAULT) or "").strip()


def load_dotenv(paths: Iterable[Path]) -> None:
    """加载简单的 ``KEY=VALUE`` 文件，避免引入 python-dotenv 依赖。

    已存在的环境变量优先级更高，不会被文件覆盖。
    """
    for path in paths:
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def default_dotenv_paths() -> list[Path]:
    """按优先级返回要加载的 .env 位置：当前目录优先，其次脚本目录。"""
    return [Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"]


def contact_email() -> str:
    """联系邮箱让请求进入 OpenAlex / NCBI 的 "polite pool"，限流更宽松。"""
    for name in ("CONTACT_EMAIL", "OPENALEX_MAILTO", "NCBI_EMAIL"):
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def user_agent() -> str:
    email = contact_email()
    return f"paperflow/0.2 (mailto:{email})" if email else "paperflow/0.2"


def resolve_lang(flag_value: str | None = None) -> str:
    """语言解析优先级：命令行参数 > 环境变量 > 默认中文。"""
    return normalise_lang(flag_value or os.getenv(ENV_LANG) or DEFAULT_LANG)


def llm_settings() -> tuple[str, str, str]:
    """返回 ``(api_key, base_url, model)``。"""
    key = (os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
    base = (os.getenv("LLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    model = os.getenv("LLM_MODEL") or "gpt-4o-mini"
    return key, base, model
