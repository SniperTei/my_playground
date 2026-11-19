import pytest

from app.bookkeeping import (
    EXPENSE_CATEGORIES,
    INCOME_CATEGORIES,
    BookkeepingError,
    account_balances,
    adjust_account_balance,
    accounts_list,
    add_account,
    add_category,
    categories,
    default_account,
    ensure_seed_account,
    migrate_cash_account,
    record_draft,
    remove_account,
    remove_category,
    set_default_account,
    void_transaction,
)
from app.ledger import Ledger


@pytest.fixture()
def ledger(tmp_path):
    lg = Ledger(tmp_path / "t.db", initial_capital=0)
    ensure_seed_account(lg)  # 种默认「现金」，等价 main 启动
    yield lg
    lg.close()


def test_expense_entry(ledger):
    r = record_draft(ledger, {"type": "expense", "category": "餐饮", "amount": 35, "note": "午饭"}, "2026-11-06", "r1")
    assert r["posted"]
    lines = {(l["account"], l["debit"], l["credit"]) for l in r["lines"]}
    assert ("expense:餐饮", 35.0, 0.0) in lines
    assert ("现金", 0.0, 35.0) in lines
    assert ledger.balance("现金") == -35


def test_income_entry(ledger):
    r = record_draft(ledger, {"type": "income", "category": "工资", "amount": 23000, "note": "发工资"}, "2026-11-05", "r1")
    assert r["posted"]
    lines = {(l["account"], l["debit"], l["credit"]) for l in r["lines"]}
    assert ("现金", 23000.0, 0.0) in lines
    assert ("income:工资", 0.0, 23000.0) in lines
    assert ledger.balance("现金") == 23000


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
    assert ledger.balance("现金") == -100


def test_void_reverses(ledger):
    r = record_draft(ledger, {"type": "expense", "category": "餐饮", "amount": 35, "note": "午饭"}, "2026-11-06", "r1")
    v = void_transaction(ledger, r["txn_id"], "2026-11-06")
    assert v["posted"]
    assert ledger.balance("现金") == 0
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
    assert ledger.balance("现金") == 0
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
    assert ledger.balance("现金") == 0


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
        add_category(ledger, "expense", "这是一个超过了十二个字的分类名字")
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
    record_draft(ledger, {"type": "expense", "category": "运动", "amount": 20, "note": ""}, "2026-11-19", "k1")
    remove_category(ledger, "expense", "运动")
    assert "运动" not in categories(ledger)["expense"]
    # 删掉后新账不能再记这个分类，但历史分录保留
    with pytest.raises(BookkeepingError):
        record_draft(ledger, {"type": "expense", "category": "运动", "amount": 20, "note": ""}, "2026-11-19", "k2")
    assert ledger.month_pnl_by_account("2026-11")["expense:运动"] == 20.0


# ----- 多账户 + 转账 -----

def test_accounts_seed_and_default(ledger):
    accts = accounts_list(ledger)
    assert accts == ["现金"]  # 空注册表自动种「现金」
    assert default_account(ledger) == "现金"


def test_expense_with_account(ledger):
    add_account(ledger, "尾号1234")
    add_account(ledger, "支付宝")
    r = record_draft(ledger, {"type": "expense", "category": "餐饮", "amount": 35, "account": "支付宝", "note": "午饭"}, "2026-11-06", "r1")
    lines = {(l["account"], l["debit"], l["credit"]) for l in r["lines"]}
    assert ("支付宝", 0.0, 35.0) in lines
    assert ledger.balance("支付宝") == -35
    assert ledger.balance("现金") == 0
    assert ledger.balance("尾号1234") == 0


def test_income_with_account_default_fallback(ledger):
    add_account(ledger, "尾号1234")
    record_draft(ledger, {"type": "income", "category": "工资", "amount": 23000, "account": "尾号1234", "note": "工资"}, "2026-11-05", "r1")
    # account 不填 → 落到默认账户「现金」
    record_draft(ledger, {"type": "income", "category": "理财", "amount": 100, "note": ""}, "2026-11-06", "r2")
    assert ledger.balance("尾号1234") == 23000
    assert ledger.balance("现金") == 100


def test_transfer_moves_balance_not_pnl(ledger):
    add_account(ledger, "尾号1234")
    add_account(ledger, "支付宝")
    record_draft(ledger, {"type": "income", "category": "工资", "amount": 23000, "account": "尾号1234", "note": ""}, "2026-11-05", "r0")
    r = record_draft(ledger, {"type": "transfer", "from_account": "尾号1234", "to_account": "支付宝", "amount": 2000, "note": "转零花"}, "2026-11-06", "t1")
    assert r["posted"]
    assert ledger.balance("尾号1234") == 21000
    assert ledger.balance("支付宝") == 2000
    # 转账不计收支：收入仍是 23000，支出 0
    assert ledger.month_pnl("2026-11") == {"income": 23000.0, "expense": 0.0, "net": 23000.0}
    assert "expense:" not in str(ledger.month_pnl_by_account("2026-11"))
    d, c = ledger.trial_balance()
    assert d == c


def test_transfer_same_account_rejected(ledger):
    with pytest.raises(BookkeepingError):
        record_draft(ledger, {"type": "transfer", "from_account": "现金", "to_account": "现金", "amount": 100, "note": ""}, "2026-11-06", "t1")


def test_transfer_missing_account_rejected(ledger):
    with pytest.raises(BookkeepingError):
        record_draft(ledger, {"type": "transfer", "from_account": "不存在的卡", "to_account": "现金", "amount": 100, "note": ""}, "2026-11-06", "t2")


def test_draft_with_unknown_account_rejected(ledger):
    with pytest.raises(BookkeepingError):
        record_draft(ledger, {"type": "expense", "category": "餐饮", "amount": 35, "account": "支付宝", "note": ""}, "2026-11-06", "x1")


def test_account_validations(ledger):
    with pytest.raises(BookkeepingError):  # 空名
        add_account(ledger, "  ")
    with pytest.raises(BookkeepingError):  # 与分类账户冲突的前缀
        add_account(ledger, "expense:餐饮")
    add_account(ledger, "微信")
    with pytest.raises(BookkeepingError):  # 重复
        add_account(ledger, "微信")


def test_remove_account_rules(ledger):
    add_account(ledger, "支付宝")
    record_draft(ledger, {"type": "expense", "category": "娱乐", "amount": 40, "account": "支付宝", "note": ""}, "2026-11-06", "r1")
    with pytest.raises(BookkeepingError):  # 有流水（余额不为 0）不能删
        remove_account(ledger, "支付宝")
    add_account(ledger, "零钱通")
    remove_account(ledger, "零钱通")  # 余额为 0 可删
    assert "零钱通" not in accounts_list(ledger)
    # 删到只剩一个（有流水的支付宝）时不能再删
    remove_account(ledger, "现金")
    with pytest.raises(BookkeepingError):
        remove_account(ledger, "支付宝")


def test_default_account_switch(ledger):
    add_account(ledger, "尾号1234")
    set_default_account(ledger, "尾号1234")
    assert default_account(ledger) == "尾号1234"
    assert accounts_list(ledger)[0] == "尾号1234"  # 默认排最前
    record_draft(ledger, {"type": "expense", "category": "餐饮", "amount": 10, "note": ""}, "2026-11-06", "r1")
    assert ledger.balance("尾号1234") == -10


def test_account_balances_total(ledger):
    add_account(ledger, "支付宝")
    record_draft(ledger, {"type": "income", "category": "工资", "amount": 1000, "account": "支付宝", "note": ""}, "2026-11-05", "r1")
    record_draft(ledger, {"type": "expense", "category": "餐饮", "amount": 30, "note": ""}, "2026-11-06", "r2")  # 默认 现金
    b = account_balances(ledger)
    assert b["支付宝"] == 1000
    assert b["现金"] == -30
    assert b["__total__"] == 970


# ----- 设置余额 -----

def test_adjust_balance_up_and_down(ledger):
    add_account(ledger, "尾号1234")
    r = adjust_account_balance(ledger, "尾号1234", 5000, "2026-11-01", "adj1")
    assert r["posted"]
    assert ledger.balance("尾号1234") == 5000
    adjust_account_balance(ledger, "尾号1234", 3000, "2026-11-02", "adj2")
    assert ledger.balance("尾号1234") == 3000
    # 不算收支，总账平衡
    assert ledger.month_pnl("2026-11") == {"income": 0.0, "expense": 0.0, "net": 0.0}
    d, c = ledger.trial_balance()
    assert d == c


def test_adjust_balance_noop_and_errors(ledger):
    add_account(ledger, "尾号1234")
    adjust_account_balance(ledger, "尾号1234", 2000, "2026-11-01", "adj1")
    r = adjust_account_balance(ledger, "尾号1234", 2000, "2026-11-02", "adj2")  # 相同值
    assert not r["posted"]
    assert ledger.balance("尾号1234") == 2000
    with pytest.raises(BookkeepingError):  # 未知账户
        adjust_account_balance(ledger, "不存在的", 100, "2026-11-01", "x")


def test_adjust_balance_voidable_and_not_in_pnl(ledger):
    add_account(ledger, "尾号1234")
    r = adjust_account_balance(ledger, "尾号1234", 5000, "2026-11-01", "adj1")
    record_draft(ledger, {"type": "expense", "category": "餐饮", "amount": 35, "account": "尾号1234", "note": ""}, "2026-11-06", "r1")
    assert ledger.balance("尾号1234") == 4965
    b = account_balances(ledger)
    assert "余额调整" not in b  # 权益账户不混进资金账户列表
    assert b["尾号1234"] == 4965
    void_transaction(ledger, r["txn_id"], "2026-11-07")  # 作废调整 → 余额回退
    assert ledger.balance("尾号1234") == -35


def test_migrate_cash_account(tmp_path):
    # 模拟 v2 老库：entries 记在 cash 上
    lg = Ledger(tmp_path / "old.db", initial_capital=0)
    lg.ensure_account("cash", "asset")
    lg.ensure_account("expense:餐饮", "expense")
    lg.post("e1", "2026-11-01", "午饭", [("expense:餐饮", 35, 0), ("cash", 0, 35)])
    migrate_cash_account(lg)
    assert "现金" in lg.accounts() and "cash" not in lg.accounts()
    assert lg.balance("现金") == -35
    assert accounts_list(lg) == ["现金"]
    migrate_cash_account(lg)  # 幂等
    assert lg.balance("现金") == -35
    lg.close()
