"""一句话账本（个人记账 v2）FastAPI 服务。

跑法：cd oneline_books && uv run uvicorn app.main:app --reload --host 0.0.0.0
"""

from __future__ import annotations

import csv
import io
import os
import threading
import uuid
from datetime import date as date_cls
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from . import llm, prompts
from .bookkeeping import (
    BookkeepingError,
    account_balances,
    adjust_account_balance,
    accounts_list,
    add_account,
    add_category,
    categories,
    category_account,
    default_account,
    migrate_cash_account,
    record_draft,
    remove_account,
    remove_category,
    set_default_account,
    void_transaction,
)
from .ledger import Ledger

DB_PATH = os.environ.get("BOOKS_DB", Path(__file__).resolve().parent.parent / "data" / "books.db")
INITIAL_CASH = float(os.environ.get("BOOKS_INITIAL_CASH", "0"))

ledger = Ledger(DB_PATH, initial_capital=INITIAL_CASH)
migrate_cash_account(ledger)  # v2 老库 cash → 现金；种默认账户
_lock = threading.RLock()  # SQLite 单连接，写操作串行化（可重入：端点内调用 get_accounts 等不再自锁）

app = FastAPI(title="一句话账本")


class ParseReq(BaseModel):
    text: str


class RecordReq(BaseModel):
    draft: dict
    date: str | None = None
    force: bool = False


class AskReq(BaseModel):
    question: str
    month: str | None = None
    history: list[dict] = []


def _month_or_now(month: str | None) -> str:
    if month:
        return month
    return date_cls.today().strftime("%Y-%m")


def _prev_month(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    if m == 1:
        return f"{y - 1}-12"
    return f"{y}-{m - 1:02d}"


def _report(month: str) -> dict:
    """月报数据：分类汇总 + 总收支 + 现金 + 上月环比。"""
    pnl = ledger.month_pnl(month)
    by_account = ledger.month_pnl_by_account(month)
    categories = {"expense": {}, "income": {}}
    for account, amount in by_account.items():
        t, _, cat = account.partition(":")
        categories[t][cat] = amount
    last = _prev_month(month)
    last_net = ledger.month_pnl(last)["net"]
    balances = account_balances(ledger)
    return {
        "month": month,
        "income": pnl["income"],
        "expense": pnl["expense"],
        "net": pnl["net"],
        "categories": categories,
        "accounts": {k: v for k, v in balances.items() if k != "__total__"},
        "total": balances.get("__total__", 0.0),
        "acct_default": default_account(ledger),
        "last_month": {"month": last, "net": last_net},
        "mom_diff": round(pnl["net"] - last_net, 2),
    }


def _find_duplicate(date: str, draft: dict) -> dict | None:
    """防重复：同日同 type+分类/转出转入+金额+账户 的最近一笔（未被作废的）。"""
    t = draft.get("type")
    amount = round(float(draft["amount"]), 2)
    if t not in ("income", "expense", "transfer"):
        return None
    rows = ledger.conn.execute(
        """
        SELECT t.id, t.date, t.memo, t.draft FROM transactions t
        WHERE t.date = ? AND t.ref_id NOT LIKE 'void-%'
          AND NOT EXISTS (SELECT 1 FROM transactions v WHERE v.ref_id = 'void-' || t.id)
        ORDER BY t.id DESC
        """,
        (date,),
    ).fetchall()
    import json as _json

    def norm_account(a: str | None) -> str:
        return (a or "").strip() or (default_account(ledger) or "")

    for r in rows:
        d = _json.loads(r["draft"] or "{}")
        if d.get("type") != t or round(float(d.get("amount") or 0), 2) != amount:
            continue
        if t == "transfer":
            if d.get("from_account") == draft.get("from_account") and d.get("to_account") == draft.get("to_account"):
                return {"id": r["id"], "date": r["date"], "memo": r["memo"], "draft": d}
        elif d.get("category") == draft.get("category") and norm_account(d.get("account")) == norm_account(draft.get("account")):
            return {"id": r["id"], "date": r["date"], "memo": r["memo"], "draft": d}
    return None


@app.post("/api/parse")
def parse(req: ParseReq):
    text = req.text.strip()
    if not text:
        raise HTTPException(422, "说句话试试，比如「午饭花了35」")
    try:
        with _lock:
            cats = categories(ledger)
            accts = accounts_list(ledger)
        draft = llm.chat_json(prompts.parse_system(cats=cats, accounts=accts), text)
    except RuntimeError as e:  # no API key
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(502, f"AI 没听懂，再试一次或换个说法（{e}）")
    return draft


@app.post("/api/record")
def record(req: RecordReq):
    date = req.date or date_cls.today().isoformat()
    try:
        with _lock:
            if not req.force:
                dup = _find_duplicate(date, req.draft)
                if dup:
                    raise HTTPException(
                        409,
                        detail={
                            "duplicate": True,
                            "message": f"同一天已记过一笔一样的：{dup['date']} {dup['memo']}。确认不是重复就再点一次「确认入账」。",
                            "existing": dup,
                        },
                    )
            ref_id = str(uuid.uuid4())
            result = record_draft(ledger, req.draft, date, ref_id)
    except BookkeepingError as e:
        raise HTTPException(422, str(e))
    return result


@app.get("/api/entries")
def entries(month: str | None = None):
    with _lock:
        return ledger.entries(month)


@app.get("/api/report")
def report(month: str | None = None):
    m = _month_or_now(month)
    with _lock:
        return _report(m)


@app.post("/api/ask")
def ask(req: AskReq):
    m = _month_or_now(req.month)
    with _lock:
        this_month = _report(m)
        last_month = _report(_prev_month(m))
        ents = ledger.entries()
    try:
        answer = llm.chat_text(
            prompts.ASK_SYSTEM,
            prompts.ask_user(req.question, this_month, last_month, ents),
            history=req.history,
        )
    except Exception as e:
        raise HTTPException(502, f"AI 没答上来，再问一次（{e}）")
    return {"answer": answer, "report": this_month}


@app.get("/api/accounts")
def get_accounts():
    with _lock:
        return {
            "accounts": accounts_list(ledger),
            "default": default_account(ledger),
            "balances": {k: v for k, v in account_balances(ledger).items() if k != "__total__"},
            "total": account_balances(ledger).get("__total__", 0.0),
        }


class AccountReq(BaseModel):
    name: str


@app.post("/api/accounts")
def post_account(req: AccountReq):
    try:
        with _lock:
            add_account(ledger, req.name)
            return get_accounts()
    except BookkeepingError as e:
        raise HTTPException(422, str(e))


@app.delete("/api/accounts/{name}")
def delete_account(name: str):
    try:
        with _lock:
            remove_account(ledger, name)
            return get_accounts()
    except BookkeepingError as e:
        raise HTTPException(422, str(e))


@app.post("/api/accounts/{name}/default")
def set_account_default(name: str):
    try:
        with _lock:
            set_default_account(ledger, name)
            return get_accounts()
    except BookkeepingError as e:
        raise HTTPException(422, str(e))


class BalanceReq(BaseModel):
    amount: float


@app.post("/api/accounts/{name}/balance")
def set_account_balance(name: str, req: BalanceReq):
    """设置账户余额：差额走「余额调整」权益账户，复式平衡、可作废。"""
    try:
        with _lock:
            adjust_account_balance(ledger, name, req.amount, date_cls.today().isoformat(), str(uuid.uuid4()))
            return get_accounts()
    except BookkeepingError as e:
        raise HTTPException(422, str(e))


@app.get("/api/categories")
def get_categories():
    with _lock:
        return categories(ledger)


class CategoryReq(BaseModel):
    type: str
    name: str


@app.post("/api/categories")
def post_category(req: CategoryReq):
    try:
        with _lock:
            add_category(ledger, req.type, req.name)
            return categories(ledger)
    except BookkeepingError as e:
        raise HTTPException(422, str(e))


@app.delete("/api/categories/{type_}/{name}")
def delete_category(type_: str, name: str):
    try:
        with _lock:
            remove_category(ledger, type_, name)
            return categories(ledger)
    except BookkeepingError as e:
        raise HTTPException(422, str(e))


@app.post("/api/void/{txn_id}")
def void(txn_id: int, date: str | None = None):
    d = date or date_cls.today().isoformat()
    try:
        with _lock:
            return void_transaction(ledger, txn_id, d)
    except BookkeepingError as e:
        raise HTTPException(422, str(e))


@app.get("/api/export")
def export(month: str | None = None):
    """CSV 导出（UTF-8 + BOM，Excel 打开中文不乱码）。"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["日期", "类型", "分类", "金额", "账户", "转出", "转入", "备注", "状态"])
    with _lock:
        for e in ledger.entries(month):
            if e["ref_id"].startswith("void-"):
                continue  # 反转分录不导出，原交易标「已作废」
            d = e["draft"]
            t = d.get("type")
            if t == "transfer":
                writer.writerow([e["date"], "转账", "", d.get("amount", ""), "",
                                 d.get("from_account", ""), d.get("to_account", ""),
                                 d.get("note") or e["memo"], "已作废" if e["voided"] else "正常"])
            elif t in ("income", "expense"):
                writer.writerow([
                    e["date"], "收入" if t == "income" else "支出",
                    d.get("category", ""), d.get("amount", ""), d.get("account", ""),
                    "", "", d.get("note") or e["memo"], "已作废" if e["voided"] else "正常",
                ])
            else:
                writer.writerow([e["date"], "", "", "", "", "", "", e["memo"], "已作废" if e["voided"] else "正常"])
    filename = f"books{'-' + month if month else ''}.csv"
    return Response(
        content="\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/")
def index():
    return FileResponse(
        Path(__file__).resolve().parent / "static" / "index.html",
        headers={"Cache-Control": "no-cache"},  # 无构建纯网页，避免手机浏览器缓存旧版
    )
