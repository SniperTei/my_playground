import pytest

from app.bookkeeping import BookkeepingError, record_draft
from app.ledger import Ledger


@pytest.fixture()
def ledger(tmp_path):
    lg = Ledger(tmp_path / "t.db", initial_capital=10000)
    yield lg
    lg.close()


def test_buy_full_payment(ledger):
    r = record_draft(ledger, {
        "type": "buy", "counterparty": None, "goods": "纸箱", "qty": 500,
        "unit_price": 2.8, "amount": 1400, "paid_amount": 1400, "note": "进纸箱付全款",
    }, "2026-11-01", "r1")
    assert r["posted"]
    lines = {(l["account"], l["debit"], l["credit"]) for l in r["lines"]}
    assert ("inventory", 1400.0, 0.0) in lines
    assert ("cash", 0.0, 1400.0) in lines
    assert ledger.balance("cash") == 10000 - 1400


def test_buy_partial_payment_creates_ap(ledger):
    record_draft(ledger, {
        "type": "buy", "counterparty": "老王", "goods": "缠绕膜", "qty": None,
        "unit_price": None, "amount": 5600, "paid_amount": 3000, "note": "",
    }, "2026-11-02", "r2")
    assert ledger.balance("inventory") == 5600
    assert ledger.ar_ap_detail()["ap"] == {"老王": 2600.0}
    assert ledger.balance("cash") == 10000 - 3000


def test_buy_unpaid_without_supplier_asked(ledger):
    with pytest.raises(BookkeepingError) as e:
        record_draft(ledger, {
            "type": "buy", "counterparty": None, "goods": "纸箱", "qty": 100,
            "unit_price": 2.0, "amount": 200, "paid_amount": None, "note": "",
        }, "2026-11-01", "r3")
    assert "供应商" in str(e.value)


def test_sell_fifo_cogs(ledger):
    # 两批进货不同成本，跨月卖，验证 FIFO
    record_draft(ledger, {
        "type": "buy", "counterparty": None, "goods": "纸箱", "qty": 100,
        "unit_price": 2.0, "amount": 200, "paid_amount": 200, "note": "",
    }, "2026-10-01", "b1")
    record_draft(ledger, {
        "type": "buy", "counterparty": None, "goods": "纸箱", "qty": 100,
        "unit_price": 3.0, "amount": 300, "paid_amount": 300, "note": "",
    }, "2026-10-15", "b2")
    r = record_draft(ledger, {
        "type": "sell", "counterparty": "宏发贸易", "goods": "纸箱", "qty": 150,
        "unit_price": 4.0, "amount": 600, "paid_amount": 300, "note": "",
    }, "2026-11-05", "s1")
    # FIFO: 100×2 + 50×3 = 350
    assert r["cogs"] == 350.0
    assert ledger.month_pnl("2026-11") == {"income": 600.0, "expense": 350.0, "net": 250.0}
    assert ledger.month_pnl("2026-10") == {"income": 0.0, "expense": 0.0, "net": 0.0}


def test_sell_deposit_creates_ar(ledger):
    record_draft(ledger, {
        "type": "buy", "counterparty": None, "goods": "纸箱", "qty": 500,
        "unit_price": 2.8, "amount": 1400, "paid_amount": 1400, "note": "",
    }, "2026-11-01", "b1")
    record_draft(ledger, {
        "type": "sell", "counterparty": "宏发贸易", "goods": "纸箱", "qty": 500,
        "unit_price": 3.2, "amount": 1600, "paid_amount": 1000, "note": "",
    }, "2026-11-03", "s1")
    assert ledger.balance("ar:宏发贸易") == 600
    assert ledger.balance("sales") == -1600  # credit-natural
    # 毛利 = 1600 - 500*2.8 = 200
    assert ledger.month_pnl("2026-11")["net"] == 200.0


def test_receipt_closes_ar(ledger):
    record_draft(ledger, {
        "type": "buy", "counterparty": None, "goods": "纸箱", "qty": 100,
        "unit_price": 1.0, "amount": 100, "paid_amount": 100, "note": "",
    }, "2026-11-01", "b1")
    record_draft(ledger, {
        "type": "sell", "counterparty": "宏发", "goods": "纸箱", "qty": 100,
        "unit_price": 2.0, "amount": 200, "paid_amount": 0, "note": "",
    }, "2026-11-02", "s1")
    record_draft(ledger, {
        "type": "receipt", "counterparty": "宏发", "goods": None, "qty": None,
        "unit_price": None, "amount": 200, "paid_amount": None, "note": "宏发结款",
    }, "2026-11-10", "rc1")
    assert ledger.balance("ar:宏发") == 0
    assert ledger.ar_ap_detail() == {"ar": {}, "ap": {}}


def test_payment_closes_ap(ledger):
    record_draft(ledger, {
        "type": "buy", "counterparty": "老王", "goods": "膜", "qty": None,
        "unit_price": None, "amount": 5600, "paid_amount": 3000, "note": "",
    }, "2026-11-01", "b1")
    record_draft(ledger, {
        "type": "payment", "counterparty": "老王", "goods": None, "qty": None,
        "unit_price": None, "amount": 2600, "paid_amount": None, "note": "",
    }, "2026-11-09", "p1")
    assert ledger.balance("ap:老王") == 0


def test_expense(ledger):
    record_draft(ledger, {
        "type": "expense", "counterparty": None, "goods": None, "qty": None,
        "unit_price": None, "amount": 300, "paid_amount": 300, "note": "运费",
    }, "2026-11-04", "e1")
    assert ledger.balance("other_expense") == 300
    assert ledger.month_pnl("2026-11")["expense"] == 300


def test_sell_service_no_goods_cogs_zero(ledger):
    r = record_draft(ledger, {
        "type": "sell", "counterparty": "宏发", "goods": None, "qty": None,
        "unit_price": None, "amount": 800, "paid_amount": 800, "note": "搬运服务费",
    }, "2026-11-05", "s2")
    assert r["cogs"] == 0
    assert ledger.month_pnl("2026-11")["net"] == 800


def test_idempotent_ref(ledger):
    d = {"type": "expense", "counterparty": None, "goods": None, "qty": None,
         "unit_price": None, "amount": 100, "paid_amount": 100, "note": ""}
    r1 = record_draft(ledger, d, "2026-11-01", "same-ref")
    r2 = record_draft(ledger, d, "2026-11-01", "same-ref")
    assert r1["posted"] and not r2["posted"]
    assert ledger.balance("other_expense") == 100


def test_sell_fifo_insufficient_raises_friendly(ledger):
    record_draft(ledger, {
        "type": "buy", "counterparty": None, "goods": "纸箱", "qty": 10,
        "unit_price": 1.0, "amount": 10, "paid_amount": 10, "note": "",
    }, "2026-11-01", "b1")
    with pytest.raises(BookkeepingError) as e:
        record_draft(ledger, {
            "type": "sell", "counterparty": "宏发", "goods": "纸箱", "qty": 20,
            "unit_price": 2.0, "amount": 40, "paid_amount": 40, "note": "",
        }, "2026-11-02", "s1")
    assert "库存" in str(e.value)


def test_sell_missing_counterparty(ledger):
    with pytest.raises(BookkeepingError):
        record_draft(ledger, {
            "type": "sell", "counterparty": None, "goods": "纸箱", "qty": 10,
            "unit_price": 2.0, "amount": 20, "paid_amount": 20, "note": "",
        }, "2026-11-01", "s1")


def test_paid_more_than_amount_rejected(ledger):
    with pytest.raises(BookkeepingError):
        record_draft(ledger, {
            "type": "buy", "counterparty": "老王", "goods": "纸箱", "qty": 10,
            "unit_price": 2.0, "amount": 20, "paid_amount": 30, "note": "",
        }, "2026-11-01", "b1")


def test_amount_from_qty_times_price(ledger):
    # amount 为 null，靠 qty*unit_price
    r = record_draft(ledger, {
        "type": "buy", "counterparty": None, "goods": "纸箱", "qty": 100,
        "unit_price": 2.5, "amount": None, "paid_amount": 250, "note": "",
    }, "2026-11-01", "b1")
    assert ledger.balance("inventory") == 250
