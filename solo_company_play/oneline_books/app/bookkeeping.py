"""draft → 复式分录 + FIFO 销货成本。

draft schema（来自 /api/parse）：
{type: buy|sell|receipt|payment|expense, counterparty, goods, qty, unit_price, amount, paid_amount, note}

分录规则：
- buy:  Dr inventory 总货款 / Cr cash 实付 + Cr ap:{供应商} 未付部分；有数量则开 FIFO 仓位
- sell: Dr cash 实收 + Dr ar:{客户} 未收部分 / Cr sales 总售价；
        Dr cogs 成本 / Cr inventory 成本（有品名+数量时 FIFO 取成本，否则 cogs=0）
- receipt: Dr cash / Cr ar:{客户}
- payment: Dr ap:{供应商} / Cr cash
- expense: Dr other_expense / Cr cash
"""

from __future__ import annotations

from typing import Any

from .ledger import Ledger, LedgerError

DRAFT_TYPES = ("buy", "sell", "receipt", "payment", "expense")


class BookkeepingError(ValueError):
    """draft 有问题，message 是直接给老板看的中文追问。"""


BASE_ACCOUNTS = [
    ("cash", "asset"),
    ("inventory", "asset"),
    ("sales", "income"),
    ("cogs", "expense"),
    ("other_expense", "expense"),
]


def ensure_base_accounts(ledger: Ledger) -> None:
    for name, type_ in BASE_ACCOUNTS:
        ledger.ensure_account(name, type_)


def _r2(x: float) -> float:
    return round(float(x), 2)


def _num(draft: dict, key: str) -> float | None:
    """draft 数字字段：None/空 → None；负数报错；0 视为 None（paid_amount=0 即没付/没收）。"""
    v = draft.get(key)
    if v is None or v == "":
        return None
    v = float(v)
    if v < 0:
        raise BookkeepingError(f"{key} 不能是负数")
    return v if v > 0 else None


def validate_draft(draft: dict) -> None:
    """校验 draft 完整性，缺信息抛 BookkeepingError（中文追问）。"""
    t = draft.get("type")
    if t not in DRAFT_TYPES:
        raise BookkeepingError("没看懂这是进钱还是出钱，换个说法试试，比如「进了500个纸箱单价2.8」")

    amount = _num(draft, "amount")
    if amount is None and draft.get("qty") is not None and draft.get("unit_price") is not None:
        amount = _num(draft, "qty") * _num(draft, "unit_price")

    if t in ("buy", "sell", "expense") and amount is None:
        raise BookkeepingError("这笔总价多少？（说总价，或数量×单价）")

    paid = _num(draft, "paid_amount")
    if t in ("buy", "sell") and amount is not None and paid is not None and paid > amount + 1e-9:
        raise BookkeepingError("付/收的比货款还多，核对一下？")

    if t in ("sell", "receipt", "payment") and not draft.get("counterparty"):
        who = "卖给哪家" if t == "sell" else "哪家"
        raise BookkeepingError(f"{who}？说下对方名字")

    if t == "buy" and draft.get("counterparty") is None:
        # parse 层 null = 暂时没付；未付部分要挂应付，必须有供应商
        paid_v = _num(draft, "paid_amount")
        total = amount
        if paid_v is None or total is None or paid_v < total - 1e-9:
            raise BookkeepingError("从哪家供应商进的？（没提付钱或没付全的，先按欠款记，需要供应商名字）")

    if t in ("receipt", "payment") and _num(draft, "amount") is None:
        raise BookkeepingError("金额多少？")


def record_draft(ledger: Ledger, draft: dict, date: str, ref_id: str) -> dict:
    """校验 + 生成分录 + FIFO + 入账（ref_id 幂等）。返回入账预览。"""
    validate_draft(draft)
    ensure_base_accounts(ledger)
    t = draft["type"]
    cp = (draft.get("counterparty") or "").strip() or None
    goods = (draft.get("goods") or "").strip() or None
    qty = _num(draft, "qty")
    unit_price = _num(draft, "unit_price")
    amount = _num(draft, "amount")
    if amount is None:
        amount = _r2(qty * unit_price)
    paid = _num(draft, "paid_amount")
    note = draft.get("note") or ""

    lines: list[tuple[str, float, float]] = []
    cogs = 0.0

    if t == "buy":
        ledger.ensure_account("inventory", "asset")
        paid_amt = _r2(paid or 0)  # parse 层 null = 暂时没付
        ap_amt = _r2(amount - paid_amt)
        lines.append(("inventory", amount, 0.0))
        if paid_amt > 0:
            lines.append(("cash", 0.0, paid_amt))
        if ap_amt > 0:
            if not cp:
                raise BookkeepingError("没付全款——从哪家供应商进的？说下对方名字")
            ap = f"ap:{cp}"
            ledger.ensure_account(ap, "liability")
            lines.append((ap, 0.0, ap_amt))
        if qty is not None and goods:
            ledger.open_position("goods", goods, qty, amount / qty, date)
        memo = note or f"进{' ' + goods if goods else '货'} {amount}元" + (f"（{cp}）" if cp else "")

    elif t == "sell":
        recv = _r2(paid or 0)
        ar_amt = _r2(amount - recv)
        ar = f"ar:{cp}"
        ledger.ensure_account(ar, "asset")
        if recv > 0:
            lines.append(("cash", recv, 0.0))
        if ar_amt > 0:
            lines.append((ar, ar_amt, 0.0))
        lines.append(("sales", 0.0, amount))
        if goods and qty is not None:
            try:
                cogs = ledger.reduce_positions_fifo("goods", goods, qty, date)
            except LedgerError:
                raise BookkeepingError(
                    f"「{goods}」库存数量不够（要出 {qty}）——是不是有进货没记数量，或没记账？"
                )
            if cogs > 0:
                lines.append(("cogs", cogs, 0.0))
                lines.append(("inventory", 0.0, cogs))
        memo = note or f"卖给{cp} {amount}元"

    elif t == "receipt":
        ar = f"ar:{cp}"
        ledger.ensure_account(ar, "asset")
        lines = [("cash", amount, 0.0), (ar, 0.0, amount)]
        memo = note or f"{cp}付款 {amount}元"

    elif t == "payment":
        ap = f"ap:{cp}"
        ledger.ensure_account(ap, "liability")
        lines = [(ap, amount, 0.0), ("cash", 0.0, amount)]
        memo = note or f"付{cp}货款 {amount}元"

    elif t == "expense":
        lines = [("other_expense", amount, 0.0), ("cash", 0.0, amount)]
        memo = note or f"杂项支出 {amount}元"

    txn_id = ledger.post(ref_id, date, memo, lines)
    return {
        "posted": txn_id is not None,
        "ref_id": ref_id,
        "date": date,
        "memo": memo,
        "lines": [{"account": a, "debit": _r2(d), "credit": _r2(c)} for a, d, c in lines],
        "cogs": _r2(cogs),
    }
