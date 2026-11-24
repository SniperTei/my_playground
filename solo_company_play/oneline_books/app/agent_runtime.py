"""通用 agent 循环引擎（能力层）：LLM ↔ 工具循环，零业务知识。

工具是黑盒函数表：runtime 只负责消息追加、按 name 分发、终态工具短路。
llm_call 约定：f(messages, schemas) -> {"content": str|None, "tool_calls": [{"id","name","arguments"}]|None}
（app.llm.chat_completion 满足该约定；测试可注入 mock。）
"""

from __future__ import annotations

import json


def run_agent(
    llm_call,
    schemas: list[dict],
    tools: dict[str, callable],
    messages: list[dict],
    max_iters: int = 10,
    terminal_tool: str | None = None,
) -> dict:
    """执行循环直到 LLM 给出无工具调用的最终回复，或调用 terminal_tool，或达到 max_iters。

    返回 {ok, terminal, content, iterations}：
    - ok=True 且 terminal 不为 None：LLM 调了终态工具，terminal 是其参数 dict
    - ok=True 且 terminal 为 None：LLM 直接文字收尾（视为放弃/失败，content 带原因）
    - ok=False：达到 max_iters 仍无结论
    """
    iterations = 0
    for iterations in range(1, max_iters + 1):
        msg = llm_call(messages, schemas)
        messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": msg.get("tool_calls")})
        tcs = msg.get("tool_calls")
        if not tcs:
            return {"ok": True, "terminal": None, "content": msg.get("content") or "", "iterations": iterations}
        for tc in tcs:
            name = tc.get("name")
            raw = tc.get("arguments") or "{}"
            if name == terminal_tool:
                try:
                    return {"ok": True, "terminal": json.loads(raw), "content": msg.get("content") or "",
                            "iterations": iterations}
                except json.JSONDecodeError as e:
                    messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": json.dumps(
                        {"error": f"终态工具参数不是合法 JSON：{e}"}, ensure_ascii=False)})
                    continue
            fn = tools.get(name)
            if fn is None:
                result = {"error": f"未知工具：{name}"}
            else:
                try:
                    args = json.loads(raw)
                    if not isinstance(args, dict):
                        result = {"error": "工具参数必须是 JSON 对象"}
                    else:
                        result = fn(**args)
                except json.JSONDecodeError as e:
                    result = {"error": f"工具参数不是合法 JSON：{e}"}
                except Exception as e:  # 工具内部错误回传给 LLM 自行调整
                    result = {"error": str(e)}
            messages.append({"role": "tool", "tool_call_id": tc.get("id"),
                             "content": json.dumps(result, ensure_ascii=False)})
    return {"ok": False, "terminal": None, "content": "", "iterations": iterations}
