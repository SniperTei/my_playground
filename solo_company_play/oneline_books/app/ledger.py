"""SQLite double-entry ledger for oneline_books.

复制自 src/solo_company/finance/ledger.py 并扩展：
- `date TEXT`（ISO YYYY-MM-DD）替代模拟日 `day int`，支持按自然月切分
- month_pnl(month) / entries(month) / ar_ap_detail()
- positions 的 opened_day/closed_day 改为 opened_date/closed_date

post() 对 ref_id 幂等（unique 约束），重复确认不会重复入账。
Sign convention: postings carry debit/credit amounts (>= 0).
Asset & expense accounts are debit-natural; liability/equity/income
are credit-natural. balance() returns the raw debit-minus-credit sum.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

ACCOUNT_TYPES = ("asset", "liability", "equity", "income", "expense")
DEBIT_NATURAL = ("asset", "expense")

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    name TEXT PRIMARY KEY,
    type TEXT NOT NULL CHECK (type IN ('asset','liability','equity','income','expense'))
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref_id TEXT NOT NULL UNIQUE,
    date TEXT NOT NULL,
    memo TEXT NOT NULL,
    draft TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS postings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_id INTEGER NOT NULL REFERENCES transactions(id),
    account TEXT NOT NULL REFERENCES accounts(name),
    debit REAL NOT NULL DEFAULT 0 CHECK (debit >= 0),
    credit REAL NOT NULL DEFAULT 0 CHECK (credit >= 0)
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    qty REAL NOT NULL,
    unit_cost REAL NOT NULL,
    opened_date TEXT NOT NULL,
    closed_date TEXT,
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS names (
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    UNIQUE(kind, name)
);
"""

OPEN_CAPITAL_REF = "bootstrap:initial-capital"


class LedgerError(ValueError):
    pass


class Ledger:
    def __init__(self, path: str | Path, initial_capital: float = 0) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        if initial_capital > 0 and not self.has_ref(OPEN_CAPITAL_REF):
            self.ensure_account("cash", "asset")
            self.ensure_account("founder_equity", "equity")
            self.post(
                OPEN_CAPITAL_REF,
                date="2000-01-01",
                memo="期初现金",
                lines=[("cash", round(initial_capital, 2), 0.0), ("founder_equity", 0.0, round(initial_capital, 2))],
            )

    # ----- accounts -----
    def ensure_account(self, name: str, type_: str) -> None:
        if type_ not in ACCOUNT_TYPES:
            raise LedgerError(f"unknown account type: {type_}")
        self.conn.execute(
            "INSERT INTO accounts(name, type) VALUES(?, ?) ON CONFLICT(name) DO NOTHING", (name, type_)
        )
        self.conn.commit()

    def accounts(self) -> dict[str, str]:
        rows = self.conn.execute("SELECT name, type FROM accounts").fetchall()
        return {r["name"]: r["type"] for r in rows}

    # ----- transactions -----
    def has_ref(self, ref_id: str) -> bool:
        row = self.conn.execute("SELECT id FROM transactions WHERE ref_id=?", (ref_id,)).fetchone()
        return row is not None

    def post(self, ref_id: str, date: str, memo: str, lines: Iterable[Iterable[Any]], draft: dict | None = None) -> int | None:
        """Post a balanced transaction. Idempotent: existing ref_id is a no-op.

        lines: iterable of (account, debit, credit).
        draft: original AI draft JSON, kept for void/reports.
        """
        if self.has_ref(ref_id):
            return None
        norm: list[tuple[str, float, float]] = []
        for account, debit, credit in lines:
            debit, credit = round(float(debit), 2), round(float(credit), 2)
            if debit < 0 or credit < 0:
                raise LedgerError(f"negative amount in line {account}: {debit}/{credit}")
            if (debit > 0) == (credit > 0):
                raise LedgerError(f"line {account} must have exactly one positive side")
            if account not in self.accounts():
                raise LedgerError(f"unknown account: {account}")
            norm.append((account, debit, credit))
        if not norm:
            raise LedgerError("empty transaction")
        total_debit = round(sum(d for _, d, _ in norm), 2)
        total_credit = round(sum(c for _, _, c in norm), 2)
        if abs(total_debit - total_credit) > 1e-6:
            raise LedgerError(f"unbalanced transaction {ref_id}: debit {total_debit} != credit {total_credit}")
        cur = self.conn.execute(
            "INSERT INTO transactions(ref_id, date, memo, draft) VALUES(?, ?, ?, ?)",
            (ref_id, date, memo, json.dumps(draft or {}, ensure_ascii=False)),
        )
        txn_id = cur.lastrowid
        self.conn.executemany(
            "INSERT INTO postings(txn_id, account, debit, credit) VALUES(?, ?, ?, ?)",
            [(txn_id, a, d, c) for a, d, c in norm],
        )
        self.conn.commit()
        return txn_id

    # ----- queries -----
    def balance(self, account: str) -> float:
        """Raw debit-credit sum (positive for debit-natural accounts)."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(debit), 0) - COALESCE(SUM(credit), 0) AS bal "
            "FROM postings WHERE account=?",
            (account,),
        ).fetchone()
        return round(row["bal"], 2)

    def balances(self) -> dict[str, float]:
        rows = self.conn.execute(
            "SELECT account, SUM(debit) - SUM(credit) AS bal FROM postings GROUP BY account"
        ).fetchall()
        return {r["account"]: round(r["bal"], 2) for r in rows}

    def trial_balance(self) -> tuple[float, float]:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(debit),0) AS d, COALESCE(SUM(credit),0) AS c FROM postings"
        ).fetchone()
        return round(row["d"], 2), round(row["c"], 2)

    def month_pnl(self, month: str) -> dict[str, float]:
        """Natural-month P&L: income minus expense for YYYY-MM."""
        like = month + "-%"
        rows = self.conn.execute(
            "SELECT p.account, SUM(p.debit) - SUM(p.credit) AS bal "
            "FROM postings p JOIN transactions t ON p.txn_id = t.id "
            "WHERE t.date LIKE ? GROUP BY p.account",
            (like,),
        ).fetchall()
        types = self.accounts()
        result = {"income": 0.0, "expense": 0.0, "net": 0.0}
        for r in rows:
            type_ = types.get(r["account"], "")
            if type_ == "income":
                result["income"] += -r["bal"]
            elif type_ == "expense":
                result["expense"] += r["bal"]
        result["income"] = round(result["income"], 2)
        result["expense"] = round(result["expense"], 2)
        result["net"] = round(result["income"] - result["expense"], 2)
        return result

    def entries(self, month: str | None = None) -> list[dict]:
        """Transaction list (newest first), optionally filtered to YYYY-MM."""
        sql = (
            "SELECT t.id, t.ref_id, t.date, t.memo, p.account, p.debit, p.credit "
            "FROM transactions t JOIN postings p ON p.txn_id = t.id"
        )
        params: tuple = ()
        if month:
            sql += " WHERE t.date LIKE ?"
            params = (month + "-%",)
        sql += " ORDER BY t.id DESC, p.id"
        rows = self.conn.execute(sql, params).fetchall()
        by_txn: dict[int, dict] = {}
        for r in rows:
            txn = by_txn.setdefault(r["id"], {"id": r["id"], "ref_id": r["ref_id"], "date": r["date"], "memo": r["memo"], "lines": []})
            txn["lines"].append({"account": r["account"], "debit": r["debit"], "credit": r["credit"]})
        return list(by_txn.values())

    def ar_ap_detail(self) -> dict[str, dict[str, float]]:
        """Outstanding AR (who owes me) and AP (whom I owe), by counterparty."""
        detail: dict[str, dict[str, float]] = {"ar": {}, "ap": {}}
        for name, bal in self.balances().items():
            if name.startswith("ar:") and bal > 0:
                detail["ar"][name[3:]] = bal
            elif name.startswith("ap:") and -bal > 0:  # 负债贷方余额为正欠款
                detail["ap"][name[3:]] = round(-bal, 2)
        return detail

    # ----- positions (inventory lots) -----
    def open_position(self, kind: str, ref: str, qty: float, unit_cost: float, date: str, meta: dict | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO positions(kind, ref, qty, unit_cost, opened_date, meta) VALUES(?, ?, ?, ?, ?, ?)",
            (kind, ref, qty, unit_cost, date, json.dumps(meta or {}, ensure_ascii=False)),
        )
        self.conn.commit()
        return cur.lastrowid

    def open_positions(self, kind: str | None = None) -> list[sqlite3.Row]:
        return self._positions(kind, open_only=True)

    def all_positions(self, kind: str | None = None) -> list[sqlite3.Row]:
        return self._positions(kind, open_only=False)

    def _positions(self, kind: str | None, open_only: bool) -> list[sqlite3.Row]:
        sql = "SELECT * FROM positions"
        conds, params = [], []
        if open_only:
            conds.append("closed_date IS NULL")
        if kind:
            conds.append("kind=?")
            params.append(kind)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id"
        return self.conn.execute(sql, params).fetchall()

    def reduce_positions_fifo(self, kind: str, ref: str, qty: float, close_date: str) -> float:
        """Consume `qty` from open positions (FIFO). Returns cost basis of consumed qty."""
        remaining = qty
        basis = 0.0
        for row in self.open_positions(kind):
            if row["ref"] != ref or remaining <= 1e-9:
                continue
            take = min(row["qty"], remaining)
            basis += take * row["unit_cost"]
            new_qty = round(row["qty"] - take, 6)
            if new_qty <= 1e-9:
                self.conn.execute(
                    "UPDATE positions SET qty=0, closed_date=? WHERE id=?", (close_date, row["id"])
                )
            else:
                self.conn.execute("UPDATE positions SET qty=? WHERE id=?", (new_qty, row["id"]))
            remaining = round(remaining - take, 6)
        if remaining > 1e-9:
            raise LedgerError(f"insufficient open positions {kind}/{ref}: short by {remaining}")
        self.conn.commit()
        return round(basis, 2)

    def close(self) -> None:
        self.conn.close()
