"""draft → 复式分录（个人记账 v2：纯收支两类）。

draft schema（来自 /api/parse）：
{type: income|expense, category, amount, note}，date 由前端带。

分类 = 复式账户：`expense:餐饮`、`income:工资`……每分类一账户，
月度分类汇总走 ledger.month_pnl_by_account()。

分录规则：
- expense: Dr expense:{分类} / Cr cash
- income:  Dr cash / Cr income:{分类}

纠错 = 作废重记：void_transaction 读原分录→逐行反转→post ref=`void-{id}`（幂等留痕）。
"""

from __future__ import annotations

from .ledger import Ledger

DRAFT_TYPES = ("income", "expense")

# 内置分类（与 prompts.py 的 few-shot 同步维护）
EXPENSE_CATEGORIES = ("餐饮", "交通", "购物", "日用", "居住", "娱乐", "医疗", "人情", "教育", "通讯", "其他")
INCOME_CATEGORIES = ("工资", "副业", "理财", "报销", "红包", "其他")


class BookkeepingError(ValueError):
    """draft 有问题，message 是直接给用户看的中文提示。"""


def _r2(x: float) -> float:
    return round(float(x), 2)


def category_account(type_: str, category: str) -> str:
    if type_ not in DRAFT_TYPES:
        raise BookkeepingError("没看懂这是收入还是支出，换个说法试试，比如「午饭花了35」")
    allowed = EXPENSE_CATEGORIES if type_ == "expense" else INCOME_CATEGORIES
    if category not in allowed:
        raise BookkeepingError(f"分类「{category}」不在{type_}的内置分类里")
    return f"{type_}:{category}"


def validate_draft(draft: dict) -> None:
    """校验 draft 完整性，缺信息抛 BookkeepingError（中文追问）。"""
    t = draft.get("type")
    if t not in DRAFT_TYPES:
        raise BookkeepingError("没看懂这是收入还是支出，换个说法试试，比如「午饭花了35」")

    amount = draft.get("amount")
    if amount is None or amount == "" or float(amount) <= 0:
        raise BookkeepingError("这笔多少钱？")

    category = draft.get("category")
    if not category:
        raise BookkeepingError("这笔算哪个分类？（拿不准就填「其他」）")
    category_account(t, category)  # 落在内置表内


def record_draft(ledger: Ledger, draft: dict, date: str, ref_id: str) -> dict:
    """校验 + 生成分录 + 入账（ref_id 幂等）。返回入账预览。"""
    validate_draft(draft)
    t = draft["type"]
    amount = _r2(draft["amount"])
    account = category_account(t, draft["category"])
    ledger.ensure_account("cash", "asset")
    ledger.ensure_account(account, t)
    note = (draft.get("note") or "").strip() or draft["category"]

    if t == "expense":
        lines = [(account, amount, 0.0), ("cash", 0.0, amount)]
    else:
        lines = [("cash", amount, 0.0), (account, 0.0, amount)]

    memo = note
    txn_id = ledger.post(ref_id, date, memo, lines, draft=draft)
    return {
        "posted": txn_id is not None,
        "txn_id": txn_id,
        "ref_id": ref_id,
        "date": date,
        "memo": memo,
        "lines": [{"account": a, "debit": _r2(d), "credit": _r2(c)} for a, d, c in lines],
    }


def void_transaction(ledger: Ledger, txn_id: int, date: str) -> dict:
    """作废一笔交易：读原分录→逐行反转→post ref=`void-{id}`（幂等）。

    复式留痕：不删历史，反转分录让账目回到入账前。无库存/应收纠缠，纯反转即可。
    """
    ref_id = f"void-{txn_id}"
    if ledger.has_ref(ref_id):
        orig = ledger.transaction(txn_id)
        return {
            "posted": False,
            "voided_ref": ref_id,
            "memo": f"作废 #{txn_id}（已作废过）",
            "date": (orig or {}).get("date", date),
        }
    orig = ledger.transaction(txn_id)
    if orig is None:
        raise BookkeepingError(f"没找到交易 #{txn_id}")
    if orig["ref_id"].startswith("void-"):
        raise BookkeepingError("作废分录不能再作废")

    reversed_lines = [(l["account"], l["credit"], l["debit"]) for l in orig["lines"]]
    memo = f"作废 #{txn_id}（{orig['memo']}）"
    void_txn_id = ledger.post(ref_id, date, memo, reversed_lines)
    return {
        "posted": void_txn_id is not None,
        "voided_ref": ref_id,
        "txn_id": void_txn_id,
        "date": date,
        "memo": memo,
        "lines": [{"account": a, "debit": _r2(d), "credit": _r2(c)} for a, d, c in reversed_lines],
    }
