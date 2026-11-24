"""对账业务层：确定性匹配 + agent 编排 + proposal 服务端校验。

流程：账单行（statement.py 归一化）→ match_statement 确定性匹配 →
未匹配行交给 agent（agent_runtime 循环 + 领域工具）→ proposals → 人确认后
走 main.py 现有的 /api/record、/api/void（agent 永不直接写账）。
"""

from __future__ import annotations

import json

from . import llm, prompts
from .agent_runtime import run_agent
from .bookkeeping import DRAFT_TYPES, BookkeepingError, accounts_list, categories, default_account, people, validate_draft
from .ledger import Ledger

DAY_WINDOW = 3  # 账单行与账本分录的最大日期差


# ----- 确定性匹配 -----

def _entry_direction(entry: dict) -> str | None:
    """账本条目的资金方向（资金账户侧进/出）。None = 方向不明（如转账），可与任意方向配。"""
    d = entry.get("draft") or {}
    t = d.get("type")
    if t in ("expense", "receivable"):
        return "out"
    if t == "income":
        return "in"
    if t == "settle":
        return "in" if d.get("kind") == "ar" else "out"
    if t == "payable":
        return "in" if not d.get("category") else None  # 垫付不走自己账户，方向不定
    return None  # transfer / 其他


def match_statement(rows: list[dict], entries: list[dict]) -> dict:
    """确定性匹配：金额精确相等 + 日期±3天 + 方向一致，贪心按日期最近唯一配对。

    entries 为 ledger.entries() 全量流水。返回：
    {matched: [{row, entry}], dup_candidates: [{txn_id, ...}], unmatched: [row]}
    dup_candidates：同额同日账本记了两笔以上而账单只有一行 → 多余的那几笔疑似重复入账。
    """
    avail = []
    for e in entries:
        if e["voided"] or e["ref_id"].startswith("void-"):
            continue
        d = e.get("draft") or {}
        if d.get("type") not in DRAFT_TYPES:
            continue  # adjust/人工调整不参与账单匹配
        try:
            amt = round(float(d.get("amount") or 0), 2)
        except (TypeError, ValueError):
            continue
        if amt <= 0:
            continue
        avail.append({"id": e["id"], "date": e["date"], "amount": amt, "memo": e["memo"],
                      "type": d.get("type"), "direction": _entry_direction(e)})

    from datetime import date as date_cls

    def _d(s: str) -> date_cls:
        return date_cls.fromisoformat(s)

    matched: list[dict] = []
    unmatched: list[dict] = []
    matched_counts: dict[tuple, int] = {}  # (date, amount) → 已被账单行配走的账本条数

    for row in sorted(rows, key=lambda r: r["date"]):
        amt, rdate = round(float(row["amount"]), 2), _d(row["date"])
        cands = [e for e in avail
                 if abs(e["amount"] - amt) < 0.005
                 and abs((_d(e["date"]) - rdate).days) <= DAY_WINDOW
                 and (e["direction"] is None or row["direction"] == "neutral" or e["direction"] == row["direction"])]
        if not cands:
            unmatched.append(row)
            continue
        best = min(cands, key=lambda e: (abs((_d(e["date"]) - rdate).days), e["id"]))
        avail.remove(best)
        matched.append({"row": row, "entry": best})
        key = (best["date"], best["amount"])
        matched_counts[key] = matched_counts.get(key, 0) + 1

    # 疑似重复入账：账本同额同日多条，但被账单行配走的不够多
    dup_candidates: list[dict] = []
    groups: dict[tuple, list[dict]] = {}
    all_entries = [e for e in entries
                   if not e["voided"] and not e["ref_id"].startswith("void-")
                   and (e.get("draft") or {}).get("type") in DRAFT_TYPES]
    for e in all_entries:
        d = e.get("draft") or {}
        try:
            amt = round(float(d.get("amount") or 0), 2)
        except (TypeError, ValueError):
            continue
        if amt <= 0:
            continue
        groups.setdefault((e["date"], amt), []).append(e)
    for (gdate, gamt), group in groups.items():
        n_stmt = matched_counts.get((gdate, gamt), 0)
        if len(group) > n_stmt:  # 账本比账单多出的 → 疑似重复
            for e in sorted(group, key=lambda x: x["id"])[n_stmt:]:
                dup_candidates.append({
                    "txn_id": e["id"], "date": e["date"], "amount": gamt, "memo": e["memo"],
                    "peer_ids": [x["id"] for x in sorted(group, key=lambda x: x["id"])[:n_stmt]],
                })
    return {"matched": matched, "dup_candidates": dup_candidates, "unmatched": unmatched}


# ----- agent 编排（领域工具 + 循环 + 校验）-----

def _build_tools(ledger: Ledger, lock=None) -> tuple[dict[str, callable], list[dict]]:
    def _locked(fn):
        def wrapper(**kwargs):
            if lock is not None:
                with lock:
                    return fn(**kwargs)
            return fn(**kwargs)
        return wrapper

    def query_entries(month: str) -> dict:
        out = []
        for e in ledger.entries(month or None):
            if e["ref_id"].startswith("void-"):
                continue
            d = e.get("draft") or {}
            out.append({"id": e["id"], "date": e["date"], "type": d.get("type"),
                        "category": d.get("category"), "person": d.get("person"),
                        "kind": d.get("kind"), "amount": d.get("amount"),
                        "account": d.get("account"), "note": d.get("note"), "voided": e["voided"]})
        return {"month": month, "entries": out}

    def get_meta() -> dict:
        return {"categories": categories(ledger), "accounts": accounts_list(ledger),
                "people": people(ledger), "default_account": default_account(ledger)}

    schemas = [
        {"type": "function", "function": {
            "name": "query_entries",
            "description": "查询账本某个月（YYYY-MM）的全部流水明细，含金额/分类/对方/状态",
            "parameters": {"type": "object", "properties": {
                "month": {"type": "string", "description": "月份，如 2025-11"}},
                "required": ["month"]}}},
        {"type": "function", "function": {
            "name": "get_meta",
            "description": "获取账本元数据：可用分类表、资金账户列表、往来人名、默认账户",
            "parameters": {"type": "object", "properties": {}}},
    },
    ]
    return {"query_entries": _locked(query_entries), "get_meta": _locked(get_meta)}, schemas


def _validate_proposals(ledger: Ledger, items: list) -> tuple[list[dict], list[str]]:
    """proposal 服务端校验（干跑，不入库）。返回 (清洗后的 proposals, 错误列表)。"""
    cleaned, errors = [], []
    if not isinstance(items, list):
        return [], ["items 必须是数组"]
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            errors.append(f"第 {i + 1} 条不是对象")
            continue
        action = it.get("action")
        reason = (it.get("reason") or "").strip()
        if action == "add":
            draft = it.get("draft") or {}
            try:
                validate_draft(ledger, draft)
                cleaned.append({"action": "add", "draft": draft, "reason": reason})
            except BookkeepingError as e:
                errors.append(f"第 {i + 1} 条(add)草稿不合法：{e}")
        elif action == "void":
            txn_id = it.get("txn_id")
            orig = ledger.transaction(int(txn_id)) if txn_id is not None else None
            if orig is None:
                errors.append(f"第 {i + 1} 条(void)：交易 #{txn_id} 不存在")
            elif orig["ref_id"].startswith("void-") or ledger.has_ref(f"void-{txn_id}"):
                errors.append(f"第 {i + 1} 条(void)：交易 #{txn_id} 已作废或不可作废")
            else:
                cleaned.append({"action": "void", "txn_id": int(txn_id), "reason": reason,
                                "memo": orig["memo"], "amount": (orig.get("draft") or {}).get("amount")})
        elif action == "ignore":
            cleaned.append({"action": "ignore", "reason": reason})
        else:
            errors.append(f"第 {i + 1} 条：未知 action「{action}」")
    return cleaned, errors


def run_reconcile_agent(ledger: Ledger, session: dict, lock=None, llm_chat=None, max_iters: int = 10) -> dict:
    """对账 agent 主入口：未匹配行 → 工具循环 → 校验通过的 proposals。

    llm_chat 可注入 mock（测试用）；LLM 调用发生在锁外（工具回调按需拿锁）。
    """
    llm_chat = llm_chat or llm.chat_completion
    tools, schemas = _build_tools(ledger, lock)
    payload = json.dumps({
        "unmatched_rows": session.get("unmatched", []),
        "duplicate_bookings": session.get("dup_candidates", []),
        "matched_count": len(session.get("matched", [])),
    }, ensure_ascii=False)
    messages: list[dict] = [
        {"role": "system", "content": prompts.RECONCILE_SYSTEM},
        {"role": "user", "content": payload + "\n\n请分析以上未匹配账单行并 submit_proposals 提交结论。"},
    ]
    for _ in range(3):  # 最多 2 轮修复
        res = run_agent(llm_chat, schemas, tools, messages, max_iters=max_iters, terminal_tool="submit_proposals")
        if not res["ok"] or res["terminal"] is None:
            return {"proposals": [], "error": f"agent 未给出结论（{res.get('content') or '轮次耗尽'}）"}
        cleaned, errors = _validate_proposals(ledger, res["terminal"].get("items") or [])
        if not errors:
            for i, p in enumerate(cleaned, 1):
                p["pid"] = i
            return {"proposals": cleaned, "iterations": res["iterations"]}
        messages.append({"role": "user", "content": "以下 proposal 校验失败，请修正后重新 submit_proposals：\n"
                         + json.dumps(errors, ensure_ascii=False)})
    return {"proposals": [], "error": "多次修正仍未通过校验"}
