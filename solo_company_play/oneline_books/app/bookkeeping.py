"""draft → 复式分录（个人记账 v3：收支两类 + 多账户 + 转账）。

draft schema（来自 /api/parse）：
{type: income|expense, category, amount, account, note}，date 由前端带。
{type: transfer, from_account, to_account, amount, note}。

- 分类 = 复式账户：`expense:餐饮`、`income:工资`……月度分类汇总走 month_pnl_by_account()。
- 资金账户 = 资产账户：尾号1234、支付宝……注册在 names 表（kind=account），余额各自独立。
- 转账 = 资产互转（Dr 入账账户 / Cr 出账账户），不计收支。

分录规则：
- expense:  Dr expense:{分类} / Cr {账户}
- income:   Dr {账户} / Cr income:{分类}
- transfer: Dr {入账账户} / Cr {出账账户}

纠错 = 作废重记：void_transaction 读原分录→逐行反转→post ref=`void-{id}`（幂等留痕）。
"""

from __future__ import annotations

from .ledger import Ledger

DRAFT_TYPES = ("income", "expense", "transfer")

# 内置分类（与 prompts.py 的 few-shot 同步维护）
EXPENSE_CATEGORIES = ("餐饮", "交通", "购物", "日用", "居住", "娱乐", "医疗", "人情", "教育", "通讯", "其他")
INCOME_CATEGORIES = ("工资", "副业", "理财", "报销", "红包", "其他")

# 注册表都存 ledger 的 names 表
CAT_KIND = {"expense": "category:expense", "income": "category:income"}
ACCOUNT_KIND = "account"
DEFAULT_ACCOUNT_KIND = "account:default"
DEFAULT_SEED_ACCOUNT = "现金"


class BookkeepingError(ValueError):
    """draft 有问题，message 是直接给用户看的中文提示。"""


def _r2(x: float) -> float:
    return round(float(x), 2)


# ----- 资金账户 -----

def migrate_cash_account(ledger: Ledger) -> None:
    """老库迁移：v2 的 `cash` 账户改名「现金」，分录一并指向。幂等。"""
    accounts = ledger.accounts()
    if "cash" in accounts:
        has_seed = "现金" in accounts
        with ledger.conn:
            if not has_seed:
                ledger.conn.execute("UPDATE accounts SET name='现金' WHERE name='cash'")
            ledger.conn.execute("UPDATE postings SET account='现金' WHERE account='cash'")
    ensure_seed_account(ledger)


def ensure_seed_account(ledger: Ledger) -> None:
    """账户注册表为空时种一个「现金」并设为默认，保证永远有可用账户。"""
    if not ledger.names(ACCOUNT_KIND):
        ledger.add_name(ACCOUNT_KIND, DEFAULT_SEED_ACCOUNT)
    if not ledger.names(DEFAULT_ACCOUNT_KIND):
        ledger.add_name(DEFAULT_ACCOUNT_KIND, default_account(ledger) or DEFAULT_SEED_ACCOUNT)


def accounts_list(ledger: Ledger) -> list[str]:
    ensure_seed_account(ledger)
    names = ledger.names(ACCOUNT_KIND)
    default = default_account(ledger)
    if default in names:  # 默认账户排最前
        names = [default] + [n for n in names if n != default]
    return names


def add_account(ledger: Ledger, name: str) -> None:
    name = (name or "").strip()
    if not name or len(name) > 20:
        raise BookkeepingError("账户名 1-20 个字，比如「尾号1234」「支付宝」")
    if name in ledger.names(ACCOUNT_KIND):
        raise BookkeepingError(f"账户「{name}」已经存在了")
    if name.startswith(("expense:", "income:", "ar:", "ap:")):
        raise BookkeepingError("这个名字跟分类账户冲突，换一个")
    ledger.add_name(ACCOUNT_KIND, name)
    if not ledger.names(DEFAULT_ACCOUNT_KIND):
        set_default_account(ledger, name)


def remove_account(ledger: Ledger, name: str) -> None:
    """删账户：注册表里移除；有余额或有流水的不能删（账不能凭空消失）。"""
    if name not in ledger.names(ACCOUNT_KIND):
        raise BookkeepingError(f"没有账户「{name}」")
    if len(ledger.names(ACCOUNT_KIND)) == 1:
        raise BookkeepingError("至少要留一个账户")
    if ledger.balance(name) != 0:
        raise BookkeepingError(f"「{name}」还有余额，先转走或作废相关流水再删")
    ledger.remove_name(ACCOUNT_KIND, name)
    if default_account(ledger) == name:
        ledger.remove_name(DEFAULT_ACCOUNT_KIND, name)
        ensure_seed_account(ledger)
        set_default_account(ledger, accounts_list(ledger)[0])


def default_account(ledger: Ledger) -> str | None:
    names = ledger.names(DEFAULT_ACCOUNT_KIND)
    if names:
        return names[0]
    accts = ledger.names(ACCOUNT_KIND)  # 没设过默认 → 注册表第一个
    return accts[0] if accts else None


def set_default_account(ledger: Ledger, name: str) -> None:
    if name not in ledger.names(ACCOUNT_KIND):
        raise BookkeepingError(f"没有账户「{name}」")
    ledger.remove_name(DEFAULT_ACCOUNT_KIND, default_account(ledger) or "")
    ledger.add_name(DEFAULT_ACCOUNT_KIND, name)


def account_balances(ledger: Ledger) -> dict[str, float]:
    """各资金账户余额（资产借方为正）+ 合计。"""
    registered = set(ledger.names(ACCOUNT_KIND))
    result = {n: ledger.balance(n) for n in accounts_list(ledger)}
    # 注册表外可能残留已删账户的历史余额，归到「其他账户」
    extra = {a: b for a, b in ledger.balances().items() if a not in registered and b != 0
             and not a.startswith(("expense:", "income:")) and a not in ("cash", "founder_equity", "inventory")}
    if extra:
        result.update(extra)
    result["__total__"] = round(sum(v for k, v in result.items() if k != "__total__"), 2)
    return result


# ----- 分类（内置 + 自定义）-----

def builtin_categories(type_: str) -> tuple[str, ...]:
    return EXPENSE_CATEGORIES if type_ == "expense" else INCOME_CATEGORIES


def categories(ledger: Ledger) -> dict[str, list[str]]:
    """完整分类表：内置 + 自定义（设置页与解析提示词用）。"""
    result = {}
    for t in ("income", "expense"):
        result[t] = list(builtin_categories(t)) + [
            n for n in ledger.names(CAT_KIND[t]) if n not in builtin_categories(t)
        ]
    return result


def add_category(ledger: Ledger, type_: str, name: str) -> None:
    if type_ not in ("income", "expense"):
        raise BookkeepingError("类型只能是 expense 或 income")
    name = (name or "").strip()
    if not name or len(name) > 12:
        raise BookkeepingError("分类名 1-12 个字")
    if name in builtin_categories(type_) or name in ledger.names(CAT_KIND[type_]):
        raise BookkeepingError(f"分类「{name}」已经存在了")
    ledger.add_name(CAT_KIND[type_], name)


def remove_category(ledger: Ledger, type_: str, name: str) -> None:
    """只能删自定义分类；内置的不给删。历史账目不受影响（账户和分录都留着）。"""
    if name in builtin_categories(type_):
        raise BookkeepingError(f"「{name}」是内置分类，不能删")
    if name not in ledger.names(CAT_KIND[type_]):
        raise BookkeepingError(f"自定义分类里没有「{name}」")
    ledger.remove_name(CAT_KIND[type_], name)


def category_account(ledger: Ledger, type_: str, category: str) -> str:
    if type_ not in ("income", "expense"):
        raise BookkeepingError("没看懂这是收入还是支出，换个说法试试，比如「午饭花了35」")
    allowed = categories(ledger)[type_]
    if category not in allowed:
        raise BookkeepingError(f"分类「{category}」不存在，去设置页加一个，或先用「其他」")
    return f"{type_}:{category}"


def _validated_account(ledger: Ledger, name: str | None, what: str) -> str:
    """draft 里的资金账户：没填用默认账户；填了必须在注册表里。"""
    if not name or not str(name).strip():
        name = default_account(ledger)
        if not name:
            raise BookkeepingError("还没有资金账户，去设置页加一个")
        return name
    name = str(name).strip()
    if name not in ledger.names(ACCOUNT_KIND):
        raise BookkeepingError(f"没有「{name}」这个账户（{what}），去设置页加一个")
    return name


# ----- draft 校验 + 入账 -----

def validate_draft(ledger: Ledger, draft: dict) -> None:
    """校验 draft 完整性，缺信息抛 BookkeepingError（中文追问）。"""
    t = draft.get("type")
    if t not in DRAFT_TYPES:
        raise BookkeepingError("没看懂这是收入还是支出，换个说法试试，比如「午饭花了35」")

    amount = draft.get("amount")
    if amount is None or amount == "" or float(amount) <= 0:
        raise BookkeepingError("这笔多少钱？")

    if t == "transfer":
        if not draft.get("from_account") or not draft.get("to_account"):
            raise BookkeepingError("从哪个账户转到哪个账户？")
        if draft["from_account"] == draft["to_account"]:
            raise BookkeepingError("转出和转入是同一个账户，记它干嘛？")
        return

    category = draft.get("category")
    if not category:
        raise BookkeepingError("这笔算哪个分类？（拿不准就填「其他」）")
    category_account(ledger, t, category)  # 落在 内置+自定义 表内


def record_draft(ledger: Ledger, draft: dict, date: str, ref_id: str) -> dict:
    """校验 + 生成分录 + 入账（ref_id 幂等）。返回入账预览。"""
    validate_draft(ledger, draft)
    t = draft["type"]
    amount = _r2(draft["amount"])
    note = (draft.get("note") or "").strip()

    if t == "transfer":
        frm = _validated_account(ledger, draft.get("from_account"), "转出")
        to = _validated_account(ledger, draft.get("to_account"), "转入")
        ledger.ensure_account(frm, "asset")
        ledger.ensure_account(to, "asset")
        lines = [(to, amount, 0.0), (frm, 0.0, amount)]
        memo = note or f"{frm} → {to}"
    else:
        cat_account = category_account(ledger, t, draft["category"])
        acct = _validated_account(ledger, draft.get("account"), "收/付款")
        ledger.ensure_account(acct, "asset")
        ledger.ensure_account(cat_account, t)
        if t == "expense":
            lines = [(cat_account, amount, 0.0), (acct, 0.0, amount)]
        else:
            lines = [(acct, amount, 0.0), (cat_account, 0.0, amount)]
        memo = note or draft["category"]

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

    复式留痕：不删历史，反转分录让账目回到入账前。纯反转，任何类型（含转账）通用。
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
