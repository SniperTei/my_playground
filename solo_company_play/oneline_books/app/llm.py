"""openai SDK → DeepSeek（或任何 OpenAI 兼容端点），JSON mode。

env 可切：BOOKS_LLM_BASE_URL / BOOKS_LLM_MODEL / BOOKS_LLM_API_KEY
默认读根目录 .env 的 DEEPSEEK_API_KEY。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

# .env 在项目根（oneline_books 的上级）；本地也兜底找一份
_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_ROOT / ".env")
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BASE_URL = os.environ.get("BOOKS_LLM_BASE_URL", "https://api.deepseek.com")
MODEL = os.environ.get("BOOKS_LLM_MODEL", "deepseek-chat")
API_KEY = os.environ.get("BOOKS_LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")

_client: OpenAI | None = None


def client() -> OpenAI:
    global _client
    if _client is None:
        if not API_KEY:
            raise RuntimeError("缺少 API key：请设置 DEEPSEEK_API_KEY 或 BOOKS_LLM_API_KEY")
        _client = OpenAI(base_url=BASE_URL, api_key=API_KEY)
    return _client


def chat_json(system: str, user: str) -> dict:
    """发送对话并要求 JSON 输出，返回解析后的 dict。"""
    resp = client().chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    content = resp.choices[0].message.content or "{}"
    return json.loads(content)


def chat_text(system: str, user: str) -> str:
    resp = client().chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.3,
    )
    return resp.choices[0].message.content or ""
