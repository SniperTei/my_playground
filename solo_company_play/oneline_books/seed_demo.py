"""灌一个人一个月的样例数据（演示/开发用，含多账户+转账）。

用法：cd oneline_books && uv run python seed_demo.py
默认写 data/demo.db；BOOKS_DB 环境变量可覆盖。
"""

from __future__ import annotations

import os
from pathlib import Path

from app.bookkeeping import add_account, record_draft, set_default_account
from app.ledger import Ledger

DB = os.environ.get("BOOKS_DB", str(Path(__file__).resolve().parent / "data" / "demo.db"))

ACCOUNTS = ["尾号1234", "尾号2345", "支付宝"]

# 一个普通打工人的 11 月：工资进 1234 → 转钱到支付宝 → 日常花销分散在几个账户
SEED = [
    ({"type": "income", "category": "工资", "amount": 23000, "account": "尾号1234", "note": "发工资"}, "2026-11-05"),
    ({"type": "expense", "category": "居住", "amount": 3500, "account": "尾号1234", "note": "11月房租"}, "2026-11-05"),
    ({"type": "transfer", "from_account": "尾号1234", "to_account": "支付宝", "amount": 2000, "note": "转支付宝零花"}, "2026-11-06"),
    ({"type": "expense", "category": "餐饮", "amount": 35, "account": "尾号1234", "note": "午饭"}, "2026-11-06"),
    ({"type": "expense", "category": "餐饮", "amount": 28, "account": "支付宝", "note": "午饭"}, "2026-11-07"),
    ({"type": "expense", "category": "娱乐", "amount": 40, "account": "支付宝", "note": "电影票"}, "2026-11-08"),
    ({"type": "income", "category": "副业", "amount": 2000, "account": "尾号2345", "note": "帮朋友做设计"}, "2026-11-10"),
    ({"type": "expense", "category": "人情", "amount": 500, "account": "支付宝", "note": "朋友结婚随礼"}, "2026-11-12"),
    ({"type": "income", "category": "理财", "amount": 1200, "account": "尾号1234", "note": "基金收益"}, "2026-11-15"),
    ({"type": "expense", "category": "通讯", "amount": 100, "account": "尾号1234", "note": "充话费"}, "2026-11-15"),
    ({"type": "expense", "category": "交通", "amount": 28, "account": "支付宝", "note": "打车"}, "2026-11-16"),
    ({"type": "expense", "category": "购物", "amount": 699, "account": "尾号2345", "note": "买外套"}, "2026-11-18"),
]


def main() -> None:
    if Path(DB).exists():
        Path(DB).unlink()  # 重灌
    lg = Ledger(DB, initial_capital=0)
    for a in ACCOUNTS:
        add_account(lg, a)
    set_default_account(lg, "尾号1234")
    for i, (draft, date) in enumerate(SEED):
        r = record_draft(lg, draft, date, f"seed-{i}")
        print(f"[{r['date']}] {r['memo']}  ✅" if r["posted"] else f"[{date}] 已存在跳过")
    print("\n=== 2026-11 月报 ===")
    print("P&L:", lg.month_pnl("2026-11"))
    print("分类汇总:", lg.month_pnl_by_account("2026-11"))
    from app.bookkeeping import account_balances

    print("账户余额:", account_balances(lg))
    lg.close()
    print(f"\n库文件：{DB}")


if __name__ == "__main__":
    main()
