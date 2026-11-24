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


def chat_completion(messages: list[dict], tools: list[dict] | None = None, temperature: float = 0) -> dict:
    """通用工具调用接口（能力层原语，agent_runtime 用）。

    返回 SDK 无关的扁平 dict：
    {"content": str|None, "tool_calls": [{"id", "name", "arguments": str}]|None}
    """
    def _norm(m: dict) -> dict:
        # agent_runtime 往 messages 里塞的是扁平 tool_calls（{"id","name","arguments"}），
        # 发给 OpenAI 兼容端点前还原成 {"id","type","function":{name,arguments}}
        tcs = m.get("tool_calls")
        if m.get("role") == "assistant" and tcs and "function" not in (tcs[0] or {}):
            m = {k: v for k, v in m.items() if k != "tool_calls"}
            m["tool_calls"] = [{"id": t.get("id"), "type": "function",
                                "function": {"name": t.get("name"), "arguments": t.get("arguments") or "{}"}}
                               for t in tcs]
        return m

    resp = client().chat.completions.create(
        model=MODEL,
        messages=[_norm(m) for m in messages],
        tools=tools or None,
        temperature=temperature,
    )
    m = resp.choices[0].message
    tcs = [{"id": t.id, "name": t.function.name, "arguments": t.function.arguments}
           for t in (m.tool_calls or [])]
    return {"content": m.content, "tool_calls": tcs or None}


def chat_text(system: str, user: str, history: list[dict] | None = None) -> str:
    """多轮对话：history 为 [{"role": "user"|"assistant", "content": str}, ...]。"""
    messages: list[dict] = [{"role": "system", "content": system}]
    for m in history or []:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": user})
    resp = client().chat.completions.create(
        model=MODEL,
        messages=messages,
        temperature=0.3,
    )
    return resp.choices[0].message.content or ""
