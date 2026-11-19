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
from .bookkeeping import BookkeepingError, category_account, record_draft, void_transaction
from .ledger import Ledger

DB_PATH = os.environ.get("BOOKS_DB", Path(__file__).resolve().parent.parent / "data" / "books.db")
INITIAL_CASH = float(os.environ.get("BOOKS_INITIAL_CASH", "0"))

ledger = Ledger(DB_PATH, initial_capital=INITIAL_CASH)
_lock = threading.Lock()  # SQLite 单连接，写操作串行化

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
    return {
        "month": month,
        "income": pnl["income"],
        "expense": pnl["expense"],
        "net": pnl["net"],
        "categories": categories,
        "cash": ledger.balance("cash"),
        "last_month": {"month": last, "net": last_net},
        "mom_diff": round(pnl["net"] - last_net, 2),
    }


def _find_duplicate(date: str, draft: dict) -> dict | None:
    """同日同 type+category+amount 的最近一笔（未被作废的）→ 防重复入账。"""
    account = category_account(draft.get("type"), draft.get("category"))
    amount = round(float(draft["amount"]), 2)
    # expense 记在分类账户借方，income 记在贷方
    side = "p.debit" if draft["type"] == "expense" else "p.credit"
    row = ledger.conn.execute(
        f"""
        SELECT t.id, t.date, t.memo, t.draft FROM transactions t
        JOIN postings p ON p.txn_id = t.id
        WHERE t.date = ? AND p.account = ? AND {side} = ?
          AND NOT EXISTS (SELECT 1 FROM transactions v WHERE v.ref_id = 'void-' || t.id)
        ORDER BY t.id DESC LIMIT 1
        """,
        (date, account, amount),
    ).fetchone()
    if row is None:
        return None
    import json as _json

    return {"id": row["id"], "date": row["date"], "memo": row["memo"], "draft": _json.loads(row["draft"] or "{}")}


@app.post("/api/parse")
def parse(req: ParseReq):
    text = req.text.strip()
    if not text:
        raise HTTPException(422, "说句话试试，比如「午饭花了35」")
    try:
        draft = llm.chat_json(prompts.PARSE_SYSTEM, text)
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
    writer.writerow(["日期", "类型", "分类", "金额", "备注", "状态"])
    with _lock:
        for e in ledger.entries(month):
            if e["ref_id"].startswith("void-"):
                continue  # 反转分录不导出，原交易标「已作废」
            d = e["draft"]
            if d.get("type") in ("income", "expense"):
                writer.writerow([
                    e["date"], "收入" if d["type"] == "income" else "支出",
                    d.get("category", ""), d.get("amount", ""),
                    d.get("note") or e["memo"], "已作废" if e["voided"] else "正常",
                ])
            else:
                writer.writerow([e["date"], "", "", "", e["memo"], "已作废" if e["voided"] else "正常"])
    filename = f"books{'-' + month if month else ''}.csv"
    return Response(
        content="\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/")
def index():
    return FileResponse(Path(__file__).resolve().parent / "static" / "index.html")
