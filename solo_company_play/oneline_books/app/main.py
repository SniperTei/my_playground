"""一句话账本 FastAPI 服务。

跑法：cd oneline_books && uv run uvicorn app.main:app --reload
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import date as date_cls
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import llm, prompts
from .bookkeeping import BookkeepingError, record_draft
from .ledger import Ledger

DB_PATH = os.environ.get("BOOKS_DB", Path(__file__).resolve().parent.parent / "data" / "books.db")
INITIAL_CASH = float(os.environ.get("BOOKS_INITIAL_CASH", "20000"))

ledger = Ledger(DB_PATH, initial_capital=INITIAL_CASH)
_lock = threading.Lock()  # SQLite 单连接，写操作串行化

app = FastAPI(title="一句话账本")


class ParseReq(BaseModel):
    text: str


class RecordReq(BaseModel):
    draft: dict
    date: str | None = None


class AskReq(BaseModel):
    question: str
    month: str | None = None


def _month_or_now(month: str | None) -> str:
    if month:
        return month
    return date_cls.today().strftime("%Y-%m")


@app.post("/api/parse")
def parse(req: ParseReq):
    text = req.text.strip()
    if not text:
        raise HTTPException(422, "说句话试试，比如「进了500个纸箱，单价2.8，付了全款」")
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
    ref_id = str(uuid.uuid4())
    try:
        with _lock:
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
        return {
            "month": m,
            "pnl": ledger.month_pnl(m),
            "cash": ledger.balance("cash"),
            "ar_ap": ledger.ar_ap_detail(),
        }


@app.post("/api/ask")
def ask(req: AskReq):
    m = _month_or_now(req.month)
    with _lock:
        rep = {
            "month": m,
            "pnl": ledger.month_pnl(m),
            "cash": ledger.balance("cash"),
            "ar_ap": ledger.ar_ap_detail(),
        }
        ents = ledger.entries()
    try:
        answer = llm.chat_text(prompts.ASK_SYSTEM, prompts.ask_user(req.question, rep, rep["ar_ap"], ents))
    except Exception as e:
        raise HTTPException(502, f"AI 没答上来，再问一次（{e}）")
    return {"answer": answer, "report": rep}


@app.get("/")
def index():
    return FileResponse(Path(__file__).resolve().parent / "static" / "index.html")
