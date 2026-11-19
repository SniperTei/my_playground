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
