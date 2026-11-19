"""灌 3-4 笔样例数据（录 demo 视频用）。

用法：cd oneline_books && uv run python seed_demo.py
默认写 data/demo.db；BOOKS_DB 环境变量可覆盖。
"""

from __future__ import annotations

import os
from pathlib import Path

from app.bookkeeping import record_draft
from app.ledger import Ledger

DB = os.environ.get("BOOKS_DB", str(Path(__file__).resolve().parent / "data" / "demo.db"))

# 一个月的完整故事：进货（部分付）→ 卖货（收定金）→ 收尾款 → 付供应商尾款 → 运费
SEED = [
    (
        {"type": "buy", "counterparty": "老王", "goods": "纸箱", "qty": 1000,
         "unit_price": 2.8, "amount": 2800, "paid_amount": 2000,
         "note": "从老王进纸箱1000个，先付2000"},
        "2026-11-03",
    ),
    (
        {"type": "sell", "counterparty": "宏发贸易", "goods": "纸箱", "qty": 600,
         "unit_price": 3.5, "amount": 2100, "paid_amount": 1000,
         "note": "卖给宏发贸易纸箱600个，收定金1000"},
        "2026-11-08",
    ),
    (
        {"type": "expense", "counterparty": None, "goods": None, "qty": None,
         "unit_price": None, "amount": 180, "paid_amount": 180, "note": "这趟送货运费"},
        "2026-11-08",
    ),
    (
        {"type": "receipt", "counterparty": "宏发贸易", "goods": None, "qty": None,
         "unit_price": None, "amount": 1100, "paid_amount": None, "note": "宏发结清尾款"},
        "2026-11-15",
    ),
    (
        {"type": "payment", "counterparty": "老王", "goods": None, "qty": None,
         "unit_price": None, "amount": 800, "paid_amount": None, "note": "给老王结清货款"},
        "2026-11-18",
    ),
]


def main() -> None:
    if Path(DB).exists():
        Path(DB).unlink()  # 重灌
    lg = Ledger(DB, initial_capital=10000)
    for i, (draft, date) in enumerate(SEED):
        r = record_draft(lg, draft, date, f"seed-{i}")
        print(f"[{r['date']}] {r['memo']}  ✅" if r["posted"] else f"[{date}] 已存在跳过")
    print("\n=== 2026-11 月报 ===")
    print("P&L:", lg.month_pnl("2026-11"))
    print("现金:", lg.balance("cash"))
    print("应收/应付:", lg.ar_ap_detail())
    lg.close()
    print(f"\n库文件：{DB}")


if __name__ == "__main__":
    main()
