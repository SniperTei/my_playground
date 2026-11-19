import pytest

from app.ledger import Ledger, LedgerError


@pytest.fixture()
def ledger(tmp_path):
    lg = Ledger(tmp_path / "t.db", initial_capital=5000)
    yield lg
    lg.close()


def _post(lg, ref, date, memo, lines):
    for name, type_ in [("cash", "asset"), ("sales", "income"), ("other_expense", "expense"),
                        ("inventory", "asset"), ("cogs", "expense"),
                        ("ar:宏发", "asset"), ("ap:老王", "liability")]:
        lg.ensure_account(name, type_)
    return lg.post(ref, date, memo, lines)


def test_date_column_and_month_filter(ledger):
    _post(ledger, "t1", "2026-10-31", "十月 sale", [("cash", 100, 0), ("sales", 0, 100)])
    _post(ledger, "t2", "2026-11-02", "十一月 sale", [("cash", 200, 0), ("sales", 0, 200)])
    oct_entries = ledger.entries("2026-10")
    nov_entries = ledger.entries("2026-11")
    assert len(oct_entries) == 1 and oct_entries[0]["memo"] == "十月 sale"
    assert len(nov_entries) == 1 and nov_entries[0]["memo"] == "十一月 sale"
    # 不传 month = 全部（期初资本也是一笔交易）
    assert len(ledger.entries()) == 3


def test_month_pnl_cross_month(ledger):
    _post(ledger, "s1", "2026-10-05", "sale", [("cash", 1000, 0), ("sales", 0, 1000)])
    _post(ledger, "e1", "2026-11-05", "expense", [("other_expense", 300, 0), ("cash", 0, 300)])
    assert ledger.month_pnl("2026-10") == {"income": 1000.0, "expense": 0.0, "net": 1000.0}
    assert ledger.month_pnl("2026-11") == {"income": 0.0, "expense": 300.0, "net": -300.0}


def test_ar_ap_detail(ledger):
    _post(ledger, "s1", "2026-11-01", "赊销", [("ar:宏发", 600, 0), ("sales", 0, 600)])
    _post(ledger, "p1", "2026-11-02", "欠供应商", [("inventory", 5600, 0), ("ap:老王", 0, 5600)])
    detail = ledger.ar_ap_detail()
    assert detail == {"ar": {"宏发": 600.0}, "ap": {"老王": 5600.0}}


def test_trial_balance_always_balanced(ledger):
    _post(ledger, "s1", "2026-11-01", "赊销", [("ar:宏发", 600, 0), ("sales", 0, 600)])
    d, c = ledger.trial_balance()
    assert d == c


def test_fifo_positions_with_dates(ledger):
    ledger.ensure_account("inventory", "asset")
    ledger.ensure_account("cash", "asset")
    ledger.open_position("goods", "纸箱", 100, 2.0, "2026-10-01")
    ledger.open_position("goods", "纸箱", 100, 3.0, "2026-10-15")
    basis = ledger.reduce_positions_fifo("goods", "纸箱", 150, "2026-11-05")
    assert basis == 350.0  # 100×2 + 50×3
    remain = ledger.open_positions("goods")
    assert len(remain) == 1 and remain[0]["qty"] == 50 and remain[0]["unit_cost"] == 3.0


def test_transaction_returns_lines_and_draft(ledger):
    ledger.ensure_account("cash", "asset")
    ledger.ensure_account("expense:餐饮", "expense")
    txn_id = ledger.post(
        "t1", "2026-11-06", "午饭",
        [("expense:餐饮", 35, 0), ("cash", 0, 35)],
        draft={"type": "expense", "category": "餐饮", "amount": 35, "note": "午饭"},
    )
    t = ledger.transaction(txn_id)
    assert t["memo"] == "午饭"
    assert t["draft"]["category"] == "餐饮"
    assert {"account": "expense:餐饮", "debit": 35.0, "credit": 0.0} in t["lines"]
    assert {"account": "cash", "debit": 0.0, "credit": 35.0} in t["lines"]
    assert ledger.transaction(999) is None


def test_month_pnl_by_account(ledger):
    ledger.ensure_account("cash", "asset")
    ledger.ensure_account("expense:餐饮", "expense")
    ledger.ensure_account("income:工资", "income")
    ledger.post("e1", "2026-11-01", "午饭", [("expense:餐饮", 35, 0), ("cash", 0, 35)])
    ledger.post("i1", "2026-11-05", "工资", [("cash", 23000, 0), ("income:工资", 0, 23000)])
    by_acct = ledger.month_pnl_by_account("2026-11")
    assert by_acct == {"expense:餐饮": 35.0, "income:工资": 23000.0}
    assert ledger.month_pnl_by_account("2026-10") == {}


def test_entries_carry_draft_and_voided_flag(ledger):
    ledger.ensure_account("cash", "asset")
    ledger.ensure_account("expense:餐饮", "expense")
    txn_id = ledger.post(
        "e1", "2026-11-01", "午饭",
        [("expense:餐饮", 35, 0), ("cash", 0, 35)],
        draft={"type": "expense", "category": "餐饮", "amount": 35, "note": "午饭"},
    )
    ents = ledger.entries("2026-11")
    assert ents[0]["draft"]["category"] == "餐饮"
    assert ents[0]["voided"] is False
    # 作废（反转）后，原交易标 voided
    ledger.post(f"void-{txn_id}", "2026-11-02", "作废", [("cash", 35, 0), ("expense:餐饮", 0, 35)])
    ents = {e["id"]: e for e in ledger.entries("2026-11")}
    assert ents[txn_id]["voided"] is True
