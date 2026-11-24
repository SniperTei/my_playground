"""agent_runtime 循环引擎 + reconcile proposal 校验 + run_reconcile_agent 编排测试（mock LLM）。"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent_runtime import run_agent
from app.bookkeeping import ensure_seed_account, record_draft
from app.ledger import Ledger
from app.reconcile import _build_tools, _validate_proposals, run_reconcile_agent


# ---------- agent_runtime：循环 / 分发 / 终态 ----------

def _llm_script(*steps):
    """按顺序吐出预设响应的 mock llm_call。"""
    calls = list(steps)

    def _call(messages, schemas):
        return calls.pop(0) if calls else {"content": "", "tool_calls": None}

    return _call


def _tc(name, args, tid="t1"):
    return {"id": tid, "name": name, "arguments": json.dumps(args, ensure_ascii=False)}


def test_runtime_terminal_short_circuit():
    tools = {"ping": lambda: {"pong": True}}
    schemas = [{"type": "function", "function": {"name": "submit", "parameters": {}}}]
    res = run_agent(_llm_script({"content": None, "tool_calls": [_tc("submit", {"items": [1, 2]})]}),
                    schemas, tools, [{"role": "user", "content": "go"}], terminal_tool="submit")
    assert res["ok"] and res["terminal"] == {"items": [1, 2]}
    assert res["iterations"] == 1


def test_runtime_tool_dispatch_and_loop():
    def add(x, y):
        return {"sum": x + y}

    schemas = [{"type": "function", "function": {"name": "add", "parameters": {}}}]
    script = [
        {"content": None, "tool_calls": [_tc("add", {"x": 2, "y": 3}, "a1")]},
        {"content": None, "tool_calls": [_tc("submit", {"done": True}, "a2")]},
    ]
    messages = [{"role": "user", "content": "go"}]
    res = run_agent(_llm_script(*script), schemas, {"add": add}, messages, terminal_tool="submit")
    assert res["terminal"] == {"done": True}
    # 消息流完整：user → assistant(a1) → tool(sum) → assistant(a2)
    assert messages[2] == {"role": "tool", "tool_call_id": "a1", "content": '{"sum": 5}'}


def test_runtime_unknown_tool_and_bad_json_fed_back():
    schemas = []
    script = [
        {"content": None, "tool_calls": [_tc("nope", {}, "u1"),
                                         {"id": "u2", "name": "submit", "arguments": "NOT-JSON"}]},
        {"content": None, "tool_calls": [_tc("submit", {"ok": 1}, "u3")]},
    ]
    res = run_agent(_llm_script(*script), schemas, {}, [], terminal_tool="submit")
    # 坏参数回传后 LLM 重试，最终成功
    assert res["terminal"] == {"ok": 1}


def test_runtime_max_iters():
    step = {"content": None, "tool_calls": [_tc("loop", {})]}
    res = run_agent(_llm_script(*[dict(step) for _ in range(5)]),
                    [], {"loop": lambda: {}}, [], max_iters=3)
    assert not res["ok"] and res["terminal"] is None


# ---------- 业务层：proposal 校验 ----------

@pytest.fixture()
def ledger(tmp_path):
    lg = Ledger(tmp_path / "t.db", initial_capital=0)
    ensure_seed_account(lg)
    yield lg
    lg.close()


def test_validate_add_rejects_bad_draft(ledger):
    cleaned, errors = _validate_proposals(ledger, [
        {"action": "add", "draft": {"type": "expense", "category": "不存在的分类", "amount": 5, "date": "2025-11-01"}},
        {"action": "add", "draft": {"type": "expense", "category": "餐饮", "amount": -3, "date": "2025-11-01"}},
    ])
    assert cleaned == [] and len(errors) == 2


def test_validate_void_checks_state(ledger):
    r = record_draft(ledger, {"type": "expense", "category": "餐饮", "amount": 35}, "2025-11-01", "v1")
    tid = r["txn_id"]
    cleaned, errors = _validate_proposals(ledger, [
        {"action": "void", "txn_id": 999},          # 不存在
        {"action": "void", "txn_id": tid},          # 合法
    ])
    assert len(cleaned) == 1 and cleaned[0]["txn_id"] == tid
    assert len(errors) == 1
    # 已作废后再提 void → 拒绝
    from app.bookkeeping import void_transaction
    void_transaction(ledger, tid, "test")
    _, errors2 = _validate_proposals(ledger, [{"action": "void", "txn_id": tid}])
    assert errors2


# ---------- run_reconcile_agent 端到端（mock，含修复轮） ----------

def test_run_agent_end_to_end(ledger):
    session = {"unmatched": [
        {"idx": 0, "date": "2025-11-01", "amount": 25.0, "direction": "out",
         "memo": "午饭外卖", "counterparty": "美团", "raw": {}},
        {"idx": 1, "date": "2025-11-01", "amount": 500.0, "direction": "neutral",
         "memo": "余额宝买入", "counterparty": "", "raw": {}},
    ], "dup_candidates": [], "matched": []}

    def llm_chat(messages, schemas, temperature=0):
        names = {s["function"]["name"] for s in schemas}
        assert "query_entries" in names and "get_meta" in names
        last = messages[-1]
        # 第一轮先拿 meta，第二轮直接 submit（一次带两个 proposal）
        if last["role"] == "user" and "校验失败" not in (last.get("content") or "") and len(messages) == 2:
            return {"content": None, "tool_calls": [{"id": "m1", "name": "get_meta", "arguments": "{}"}]}
        items = [
            {"action": "add", "reason": "账本无此消费，补记",
             "draft": {"type": "expense", "category": "餐饮", "amount": 25.0,
                       "account": None, "date": "2025-11-01", "note": "午饭外卖"}},
            {"action": "ignore", "reason": "理财买入，不计收支"},
        ]
        return {"content": None, "tool_calls": [{"id": "s1", "name": "submit_proposals",
                                                 "arguments": json.dumps({"items": items}, ensure_ascii=False)}]}

    res = run_reconcile_agent(ledger, session, llm_chat=llm_chat)
    assert res["error"] is None if "error" in res else True
    assert len(res["proposals"]) == 2
    assert [p["action"] for p in res["proposals"]] == ["add", "ignore"]
    assert [p["pid"] for p in res["proposals"]] == [1, 2]


def test_run_agent_repair_round(ledger):
    """第一轮提交非法草稿 → 错误回传 → 第二轮修正。"""
    session = {"unmatched": [{"idx": 0, "date": "2025-11-01", "amount": 10.0, "direction": "out",
                              "memo": "早餐", "counterparty": "", "raw": {}}],
               "dup_candidates": [], "matched": []}
    state = {"round": 0}

    def llm_chat(messages, schemas, temperature=0):
        state["round"] += 1
        if state["round"] == 1:
            bad = {"action": "add", "reason": "补记",
                   "draft": {"type": "expense", "category": "不存在的分类", "amount": 10.0, "date": "2025-11-01"}}
            return {"content": None, "tool_calls": [{"id": "s1", "name": "submit_proposals",
                                                     "arguments": json.dumps({"items": [bad]})}]}
        assert any("校验失败" in (m.get("content") or "") for m in messages if m["role"] == "user")
        good = {"action": "add", "reason": "补记",
                "draft": {"type": "expense", "category": "餐饮", "amount": 10.0, "date": "2025-11-01", "note": "早餐"}}
        return {"content": None, "tool_calls": [{"id": "s2", "name": "submit_proposals",
                                                 "arguments": json.dumps({"items": [good]})}]}

    res = run_reconcile_agent(ledger, session, llm_chat=llm_chat)
    assert len(res["proposals"]) == 1
    assert res["proposals"][0]["draft"]["category"] == "餐饮"


def test_run_agent_no_conclusion(ledger):
    session = {"unmatched": [], "dup_candidates": [], "matched": []}
    res = run_reconcile_agent(ledger, session, llm_chat=lambda m, s, temperature=0: {"content": "不会", "tool_calls": None})
    assert res["proposals"] == [] and "error" in res


# ---------- 领域工具 ----------

def test_build_tools_query_and_meta(ledger):
    record_draft(ledger, {"type": "expense", "category": "餐饮", "amount": 35}, "2025-11-01", "q1")
    tools, schemas = _build_tools(ledger)
    out = tools["query_entries"](month="2025-11")
    assert len(out["entries"]) == 1
    assert out["entries"][0]["category"] == "餐饮"
    meta = tools["get_meta"]()
    assert "餐饮" in meta["categories"]["expense"]
    assert "现金" in meta["accounts"]
