"""statement.py（账单摄取）+ reconcile.match_statement（确定性匹配）测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.reconcile import match_statement
from app.statement import parse_statement

# ---------- 解析：支付宝 preset ----------

ALIPAY_CSV = """支付宝交易明细查询,--------------------,
账号,[xxx@alipay.com],起始日期,[2025-11-01 00:00:00],终止日期,[2025-11-05 23:59:59]
---------------------------------成交----------------------------------
交易时间,交易分类,交易对方,对方账号,商品说明,收/支,金额,收/付款方式,交易状态,交易订单号,商家订单号,备注
2025-11-01 12:33:01,餐饮,美团平台,,午饭外卖,支出,25.00,余额宝,交易成功,TP001,M001,
2025-11-02 09:00:00,交通,滴滴出行,,上班打车,支出,18.50,招商银行(1234),交易成功,TP002,M002,
2025-11-03 18:20:00,工资,某某公司,,11月工资,收入,12000.00,招商银行(1234),交易成功,TP003,M003,
"""


def test_alipay_parse():
    res = parse_statement(ALIPAY_CSV.encode("utf-8"))
    assert res["source"] == "alipay"
    assert res["needs_mapping"] is False
    assert len(res["rows"]) == 3
    r0 = res["rows"][0]
    assert r0["date"] == "2025-11-01"
    assert r0["amount"] == 25.0
    assert r0["direction"] == "out"
    assert r0["memo"] == "午饭外卖"
    assert r0["counterparty"] == "美团平台"
    assert r0["raw"]["交易订单号"] == "TP001"


def test_alipay_gbk_decode():
    res = parse_statement(ALIPAY_CSV.encode("gb18030"))
    assert res["source"] == "alipay"
    assert len(res["rows"]) == 3


# ---------- 解析：微信 preset（¥、tab 分隔、双列金额） ----------

WECHAT_CSV = ("微信支付账单明细,--------------,\n"
              "账单列表：（共 2 笔）\n"
              "----------------------微信支付账单明细--------------------\n"
              "微信昵称：[小张]\n"
              "起始时间：[2025-11-01 00:00:00] 终止时间：[2025-11-03 23:59:59]\n"
              "导出类型：[全部]\n"
              "----------------------微信支付账单明细--------------------\n"
              "共 2 笔记录\n"
              "收入：20笔 0.00元\n"
              "类型,单号,商户单号,交易时间,交易对方,商品,收/支出,金额(元),支付方式,当前状态,备注,商户分类\n"
              "/2025-11-01 10:00:00,测试,WS001,2025-11-01 10:00:00,全家便利店,早饭,支出,¥12.50,/零钱,支付成功,/,餐饮美食\n"
              "/2025-11-02 10:00:00,测试,WS002,2025-11-02 10:00:00,某某公司,报销回款,收入,¥300.00,/零钱,已存入零钱,/,企业\n")


def test_wechat_parse():
    res = parse_statement(WECHAT_CSV.encode("utf-8"))
    assert res["source"] == "wechat"
    assert len(res["rows"]) == 2
    r1 = res["rows"][1]
    assert r1["amount"] == 300.0  # ¥300.00 → 300.0
    assert r1["direction"] == "in"


# ---------- 解析：generic（收入/支出双列）+ 手动映射 ----------

GENERIC_CSV = "日期,摘要,收入金额,支出金额,余额\n2025-11-01,ATM取款,,500.00,1000.00\n2025-11-02,利息,3.20,,1003.20\n"


def test_generic_income_expense_columns():
    res = parse_statement(GENERIC_CSV.encode("utf-8"))
    assert res["source"] == "generic"
    assert len(res["rows"]) == 2
    assert res["rows"][0]["direction"] == "out"
    assert res["rows"][0]["amount"] == 500.0
    assert res["rows"][1]["direction"] == "in"


def test_manual_mapping_fallback():
    # 罕见表头：自动识别不出 → needs_mapping；手动映射后正常解析
    weird = "XDATE,XAMT,XDIR,XNOTE\n2025/11/01 08:00:00,42.00,收入,礼品\n2025/11/02,10.00,支出,早餐\n"
    res = parse_statement(weird.encode("utf-8"))
    assert res["needs_mapping"] is True and res["rows"] == []
    res2 = parse_statement(weird.encode("utf-8"), mapping={
        "header_row": res["header_row_hint"], "date": 0, "amount": 1, "direction": 2, "memo": 3})
    assert res2["source"] == "custom"
    assert [r["date"] for r in res2["rows"]] == ["2025-11-01", "2025-11-02"]
    assert [r["direction"] for r in res2["rows"]] == ["in", "out"]


def test_junk_rows_skipped_and_dates():
    res = parse_statement(ALIPAY_CSV.encode("utf-8"))
    # 头部 3 行垃圾全部跳过，idx 从 0 连续编号
    assert [r["idx"] for r in res["rows"]] == [0, 1, 2]
    assert res["header_row"] == 3


# ---------- 确定性匹配 ----------

def _row(date, amt, direction="out", idx=0):
    return {"idx": idx, "date": date, "amount": amt, "direction": direction,
            "memo": "", "counterparty": "", "raw": {}}


def _entry(eid, date, amt, type_="expense", direction="out", voided=False):
    return {"id": eid, "ref_id": f"t{eid}", "date": date, "voided": voided, "memo": "",
            "draft": {"type": type_, "amount": amt}, "direction": direction}


def test_match_basic():
    res = match_statement(
        [_row("2025-11-01", 25.0)],
        [_entry(1, "2025-11-01", 25.0, "expense")])
    assert len(res["matched"]) == 1 and not res["unmatched"]
    assert res["matched"][0]["entry"]["id"] == 1


def test_match_date_window_and_direction():
    # 日期差 3 天内可配；方向相反不可配
    res = match_statement([_row("2025-11-04", 25.0)],
                          [_entry(1, "2025-11-01", 25.0, "expense")])
    assert len(res["matched"]) == 1
    res2 = match_statement([_row("2025-11-01", 25.0, "in")],
                           [_entry(1, "2025-11-01", 25.0, "expense")])
    assert res2["unmatched"] and not res2["matched"]


def test_match_greedy_nearest_date():
    e1, e2 = _entry(1, "2025-11-05", 25.0), _entry(2, "2025-11-02", 25.0)
    res = match_statement([_row("2025-11-02", 25.0)], [e1, e2])
    assert res["matched"][0]["entry"]["id"] == 2  # 日期最近的优先


def test_match_skips_voided_and_adjust():
    res = match_statement([_row("2025-11-01", 25.0)],
                          [_entry(1, "2025-11-01", 25.0, voided=True),
                           {"id": 2, "ref_id": "t2", "date": "2025-11-01", "voided": False,
                            "memo": "", "draft": {"type": "adjust", "amount": 25.0}}])
    assert res["unmatched"] and not res["matched"]


def test_dup_candidates():
    # 账本同额同日两笔，账单只有一行 → 多出的一笔是疑似重复
    rows = [_row("2025-11-01", 25.0)]
    entries = [_entry(1, "2025-11-01", 25.0), _entry(2, "2025-11-01", 25.0)]
    res = match_statement(rows, entries)
    assert len(res["matched"]) == 1
    assert len(res["dup_candidates"]) == 1
    dup = res["dup_candidates"][0]
    assert dup["txn_id"] == 2  # id 大的（后被记的那笔）被标为疑似重复
    assert dup["peer_ids"] == [1]


def test_transfer_neutral_direction_pairs_any():
    # 账单行方向 neutral（如银行流水无方向列）可与任意账本方向配
    res = match_statement([_row("2025-11-01", 100.0, "neutral")],
                          [_entry(1, "2025-11-01", 100.0, "transfer", "in")])
    assert len(res["matched"]) == 1
