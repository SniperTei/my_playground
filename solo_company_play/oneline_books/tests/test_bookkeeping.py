import pytest

from app.bookkeeping import (
    EXPENSE_CATEGORIES,
    INCOME_CATEGORIES,
    BookkeepingError,
    add_category,
    categories,
    record_draft,
    remove_category,
    void_transaction,
)
from app.ledger import Ledger


@pytest.fixture()
def ledger(tmp_path):
    lg = Ledger(tmp_path / "t.db", initial_capital=0)
    yield lg
    lg.close()


def test_expense_entry(ledger):
    r = record_draft(ledger, {"type": "expense", "category": "餐饮", "amount": 35, "note": "午饭"}, "2026-11-06", "r1")
    assert r["posted"]
    lines = {(l["account"], l["debit"], l["credit"]) for l in r["lines"]}
    assert ("expense:餐饮", 35.0, 0.0) in lines
    assert ("cash", 0.0, 35.0) in lines
    assert ledger.balance("cash") == -35


def test_income_entry(ledger):
    r = record_draft(ledger, {"type": "income", "category": "工资", "amount": 23000, "note": "发工资"}, "2026-11-05", "r1")
    assert r["posted"]
    lines = {(l["account"], l["debit"], l["credit"]) for l in r["lines"]}
    assert ("cash", 23000.0, 0.0) in lines
    assert ("income:工资", 0.0, 23000.0) in lines
    assert ledger.balance("cash") == 23000


def test_category_accounts_tracked(ledger):
    record_draft(ledger, {"type": "expense", "category": "餐饮", "amount": 35, "note": "午饭"}, "2026-11-01", "a")
    record_draft(ledger, {"type": "expense", "category": "餐饮", "amount": 28, "note": "午饭"}, "2026-11-02", "b")
    record_draft(ledger, {"type": "expense", "category": "交通", "amount": 28, "note": "打车"}, "2026-11-02", "c")
    record_draft(ledger, {"type": "income", "category": "工资", "amount": 23000, "note": "工资"}, "2026-11-05", "d")
    by_acct = ledger.month_pnl_by_account("2026-11")
    assert by_acct["expense:餐饮"] == 63.0
    assert by_acct["expense:交通"] == 28.0
    assert by_acct["income:工资"] == 23000.0


def test_validate_rejects_bad_type(ledger):
    with pytest.raises(BookkeepingError):
        record_draft(ledger, {"type": "buy", "category": "其他", "amount": 100, "note": ""}, "2026-11-01", "x1")


def test_validate_rejects_missing_amount(ledger):
    with pytest.raises(BookkeepingError):
        record_draft(ledger, {"type": "expense", "category": "餐饮", "amount": None, "note": ""}, "2026-11-01", "x1")
    with pytest.raises(BookkeepingError):
        record_draft(ledger, {"type": "expense", "category": "餐饮", "amount": 0, "note": ""}, "2026-11-01", "x2")


def test_validate_rejects_category_outside_table(ledger):
    with pytest.raises(BookkeepingError):
        record_draft(ledger, {"type": "expense", "category": "工资", "amount": 100, "note": ""}, "2026-11-01", "x1")  # 收入分类配支出
    with pytest.raises(BookkeepingError):
        record_draft(ledger, {"type": "income", "category": "餐饮", "amount": 100, "note": ""}, "2026-11-01", "x2")


def test_validate_rejects_unknown_category(ledger):
    with pytest.raises(BookkeepingError):
        record_draft(ledger, {"type": "expense", "category": "宠物", "amount": 100, "note": ""}, "2026-11-01", "x1")


def test_validate_missing_category(ledger):
    with pytest.raises(BookkeepingError):
        record_draft(ledger, {"type": "expense", "category": None, "amount": 100, "note": ""}, "2026-11-01", "x1")


def test_all_builtin_categories_accepted(ledger):
    for i, cat in enumerate(EXPENSE_CATEGORIES):
        record_draft(ledger, {"type": "expense", "category": cat, "amount": 1, "note": ""}, "2026-11-01", f"e{i}")
    for i, cat in enumerate(INCOME_CATEGORIES):
        record_draft(ledger, {"type": "income", "category": cat, "amount": 1, "note": ""}, "2026-11-01", f"i{i}")
    assert len(ledger.month_pnl_by_account("2026-11")) == len(EXPENSE_CATEGORIES) + len(INCOME_CATEGORIES)


def test_idempotent_ref(ledger):
    d = {"type": "expense", "category": "餐饮", "amount": 100, "note": ""}
    r1 = record_draft(ledger, d, "2026-11-01", "same-ref")
    r2 = record_draft(ledger, d, "2026-11-01", "same-ref")
    assert r1["posted"] and not r2["posted"]
    assert ledger.balance("cash") == -100


def test_void_reverses(ledger):
    r = record_draft(ledger, {"type": "expense", "category": "餐饮", "amount": 35, "note": "午饭"}, "2026-11-06", "r1")
    v = void_transaction(ledger, r["txn_id"], "2026-11-06")
    assert v["posted"]
    assert ledger.balance("cash") == 0
    assert ledger.month_pnl("2026-11") == {"income": 0.0, "expense": 0.0, "net": 0.0}  # 月报归零
    assert ledger.month_pnl_by_account("2026-11") == {}  # 分类汇总也归零
    # 留痕：两笔交易都在
    ents = ledger.entries("2026-11")
    assert len(ents) == 2
    orig = next(e for e in ents if e["id"] == r["txn_id"])
    assert orig["voided"] is True


def test_void_is_idempotent(ledger):
    r = record_draft(ledger, {"type": "income", "category": "工资", "amount": 23000, "note": "工资"}, "2026-11-05", "r1")
    v1 = void_transaction(ledger, r["txn_id"], "2026-11-06")
    v2 = void_transaction(ledger, r["txn_id"], "2026-11-06")
    assert v1["posted"] and not v2["posted"]
    assert ledger.balance("cash") == 0
    d, c = ledger.trial_balance()
    assert d == c


def test_void_missing_txn(ledger):
    with pytest.raises(BookkeepingError):
        void_transaction(ledger, 999, "2026-11-06")


def test_void_a_void_rejected(ledger):
    r = record_draft(ledger, {"type": "expense", "category": "餐饮", "amount": 35, "note": ""}, "2026-11-06", "r1")
    v = void_transaction(ledger, r["txn_id"], "2026-11-06")
    with pytest.raises(BookkeepingError):
        void_transaction(ledger, v["txn_id"], "2026-11-07")


def test_void_cross_month_keeps_books_balanced(ledger):
    # 11 月记的账，12 月作废：12 月出现负支出，11 月保持原样，总账仍平衡
    r = record_draft(ledger, {"type": "expense", "category": "餐饮", "amount": 35, "note": "午饭"}, "2026-11-30", "r1")
    void_transaction(ledger, r["txn_id"], "2026-12-01")
    assert ledger.month_pnl("2026-11")["expense"] == 35.0
    assert ledger.month_pnl("2026-12")["expense"] == -35.0
    d, c = ledger.trial_balance()
    assert d == c
    assert ledger.balance("cash") == 0


def test_memo_defaults_to_category(ledger):
    r = record_draft(ledger, {"type": "expense", "category": "交通", "amount": 28, "note": ""}, "2026-11-06", "r1")
    assert r["memo"] == "交通"


# ----- 自定义分类 -----

def test_add_and_use_custom_category(ledger):
    add_category(ledger, "expense", "运动健身")
    cats = categories(ledger)
    assert "运动健身" in cats["expense"] and "运动健身" not in cats["income"]
    r = record_draft(ledger, {"type": "expense", "category": "运动健身", "amount": 20, "note": "健身"}, "2026-11-19", "r1")
    assert r["posted"]
    assert ledger.month_pnl_by_account("2026-11")["expense:运动健身"] == 20.0


def test_custom_category_income_side(ledger):
    add_category(ledger, "income", "房租收入")
    record_draft(ledger, {"type": "income", "category": "房租收入", "amount": 3000, "note": "出租"}, "2026-11-19", "r1")
    assert ledger.month_pnl("2026-11")["income"] == 3000.0


def test_add_category_validations(ledger):
    with pytest.raises(BookkeepingError):  # 重名内置
        add_category(ledger, "expense", "餐饮")
    with pytest.raises(BookkeepingError):  # 重复添加
        add_category(ledger, "expense", "运动")
        add_category(ledger, "expense", "运动")
    with pytest.raises(BookkeepingError):  # 空名/超长
        add_category(ledger, "expense", "  ")
    with pytest.raises(BookkeepingError):
        add_category(ledger, "expense", "这是一个超长分类名字呀")
    with pytest.raises(BookkeepingError):  # 类型不对
        add_category(ledger, "transfer", "转账")


def test_record_with_unregistered_category_rejected(ledger):
    # 没在设置里加过的分类不能直接入账
    with pytest.raises(BookkeepingError):
        record_draft(ledger, {"type": "expense", "category": "运动健身", "amount": 20, "note": ""}, "2026-11-19", "x1")


def test_remove_category_rules(ledger):
    with pytest.raises(BookkeepingError):  # 内置不能删
        remove_category(ledger, "expense", "餐饮")
    with pytest.raises(BookkeepingError):  # 不存在
        remove_category(ledger, "expense", "运动")
    add_category(ledger, "expense", "运动")
    remove_category(ledger, "expense", "运动")
    assert "运动" not in categories(ledger)["expense"]
    # 删掉后新账不能再记这个分类，但历史分录保留
    record_draft(ledger, {"type": "expense", "category": "运动", "amount": 20, "note": ""}, "2026-11-19", "k1")
    remove_category(ledger, "expense", "运动")
    with pytest.raises(BookkeepingError):
        record_draft(ledger, {"type": "expense", "category": "运动", "amount": 20, "note": ""}, "2026-11-19", "k2")
    assert ledger.month_pnl_by_account("2026-11")["expense:运动"] == 20.0
