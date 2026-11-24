"""账单摄取能力层：CSV/文本 → 归一化交易行。

零业务知识（不知道「记账/对账」是什么），只做数据摄取：
- 编码级联 utf-8 → gb18030（支付宝/微信导出是 GBK 系）
- 表头探测：扫描前 40 行找命中 ≥2 个列关键词的行
- 来源判定：支付宝 / 微信支付 / 通用（银行流水按 generic 处理）
- 列提取 preset + 自动识别失败时的手动列映射（mapping 参数）
- 归一化行：{idx, date, amount(>0), direction: in|out|neutral, memo, counterparty, raw}

调用方（业务层 reconcile.py）拿归一化行去做匹配和归类。
"""

from __future__ import annotations

import csv
import re
from datetime import date as date_cls

# 列名别名（按优先级，取第一个命中的）
DATE_ALIASES = ("交易时间", "交易日期", "交易日期时间", "记账时间", "记账日期", "日期", "时间")
DIRECTION_ALIASES = ("收/支出", "收/支", "收支", "记账方向", "借贷标志", "方向")
AMOUNT_ALIASES = ("金额(元)", "金额（元）", "交易金额", "发生金额", "金额")
INCOME_ALIASES = ("收入金额", "收入(元)", "收入（元）", "收入")
EXPENSE_ALIASES = ("支出金额", "支出(元)", "支出（元）", "支出")
MEMO_ALIASES = ("商品说明", "商品名称", "商品", "摘要", "用途", "附言", "备注", "详情", "说明")
COUNTERPARTY_ALIASES = ("交易对方", "交易对方名称", "对方户名", "商户名称", "对方账户", "对方")

# 辅助判定来源的标记列（不参与提取）
_ALIPAY_MARKS = ("交易订单号", "商家订单号", "收/支")
_WECHAT_MARKS = ("商户单号", "收/支出")
# 表头探测时算「命中」的额外列
_EXTRA_MARKS = ("交易分类", "交易状态", "支付方式", "收/付款方式", "交易单号", "余额", "交易类型")

MAX_SCAN_LINES = 40


def _decode(data) -> str:
    """字节流解码级联：utf-8 → gb18030 → latin-1（不抛错兜底）。str 原样返回。"""
    if isinstance(data, str):
        return data
    for enc in ("utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1", errors="replace")


def _read_rows(text: str) -> list[list[str]]:
    """按行解析 CSV，自动嗅探分隔符（逗号/制表符），失败退回逗号。"""
    sample = "\n".join(text.splitlines()[:60])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    return list(csv.reader(text.splitlines(), delimiter=delimiter))


def _money(value) -> float | None:
    """¥25.00 / 1,024.50 / 25 → 25.0；解析失败或 ≤0 返回 None。"""
    s = str(value or "").replace("¥", "").replace("￥", "").replace(",", "").replace("，", "").strip()
    if not s:
        return None
    try:
        v = round(float(s), 2)
    except ValueError:
        return None
    return abs(v) if v > 0 else None


def _date(value) -> str | None:
    """'2025-11-01 12:33:01' / '2025/11/1' / '2025年11月1日' → 'YYYY-MM-DD'。"""
    s = str(value or "").strip()
    m = re.match(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", s)
    if m:
        try:
            return date_cls(int(m[1]), int(m[2]), int(m[3])).isoformat()
        except ValueError:
            return None
    m = re.match(r"^(\d{4})(\d{2})(\d{2})\D", s) or re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
    if m:
        try:
            return date_cls(int(m[1]), int(m[2]), int(m[3])).isoformat()
        except ValueError:
            return None
    return None


def _direction(value) -> str:
    s = str(value or "").strip()
    if s in ("收入", "收", "贷", "贷记", "入账", "转入"):
        return "in"
    if s in ("支出", "借", "借记", "出账", "转出"):
        return "out"
    return "neutral"


def _hit_count(cells: list[str]) -> int:
    """一行里像「列名」的单元格数（表头探测用）。"""
    marks = set(DATE_ALIASES + DIRECTION_ALIASES + AMOUNT_ALIASES + INCOME_ALIASES + EXPENSE_ALIASES
                + MEMO_ALIASES + COUNTERPARTY_ALIASES + _EXTRA_MARKS)
    return sum(1 for c in cells if str(c).strip() in marks)


def _find_column(cells: list[str], aliases: tuple[str, ...]) -> int | None:
    lowered = [str(c).strip() for c in cells]
    for a in aliases:
        if a in lowered:
            return lowered.index(a)
    return None


def detect_source(cells: list[str]) -> str:
    s = {str(c).strip() for c in cells}
    if s & set(_WECHAT_MARKS):
        return "wechat"
    if s & set(_ALIPAY_MARKS):
        return "alipay"
    return "generic"


def _column_map(cells: list[str]) -> dict[str, int | None]:
    return {
        "date": _find_column(cells, DATE_ALIASES),
        "direction": _find_column(cells, DIRECTION_ALIASES),
        "amount": _find_column(cells, AMOUNT_ALIASES),
        "income": _find_column(cells, INCOME_ALIASES),
        "expense": _find_column(cells, EXPENSE_ALIASES),
        "memo": _find_column(cells, MEMO_ALIASES),
        "counterparty": _find_column(cells, COUNTERPARTY_ALIASES),
    }


def _build_row(cells: list[str], col: dict[str, int | None], idx: int, header: list[str]) -> dict | None:
    """从一行原始单元格提取归一化行；缺日期或金额的行跳过（垃圾/汇总行）。"""
    def cell(key: str) -> str:
        i = col.get(key)
        return str(cells[i]).strip() if i is not None and i < len(cells) else ""

    d = _date(cell("date"))
    if d is None:
        return None

    direction = _direction(cell("direction"))
    if direction == "neutral" and (col.get("income") is not None or col.get("expense") is not None):
        inc, exp = _money(cell("income")), _money(cell("expense"))
        if inc and inc > 0:
            direction, amount = "in", inc
        elif exp and exp > 0:
            direction, amount = "out", exp
        else:
            return None  # 收支两列都空（如只有余额列的行）
    else:
        amount = _money(cell("amount"))
    if amount is None:
        return None

    raw = {h: (cells[i] if i < len(cells) else "") for i, h in enumerate(header) if str(h).strip()}
    return {
        "idx": idx,
        "date": d,
        "amount": amount,
        "direction": direction,
        "memo": cell("memo"),
        "counterparty": cell("counterparty"),
        "raw": raw,
    }


def parse_statement(data, mapping: dict | None = None) -> dict:
    """解析账单文本/字节为归一化行。

    mapping（手动列映射，自动识别失败时前端选好再调）：
      {header_row: int, date: int, direction?: int, amount?: int,
       income?: int, expense?: int, memo?: int, counterparty?: int}

    返回：
      成功  {source, header_row, columns, rows, needs_mapping: False}
      失败  {source: None, columns: <行数最多一行的单元格>, header_row_hint, needs_mapping: True, rows: []}
    """
    text = _decode(data)
    rows_csv = _read_rows(text)

    if mapping:
        header_idx = int(mapping["header_row"])
        header = rows_csv[header_idx] if header_idx < len(rows_csv) else []
        col = {k: mapping.get(k) for k in ("date", "direction", "amount", "income", "expense", "memo", "counterparty")}
        source = "custom"
    else:
        header_idx, header, hit = None, [], -1
        for i, cells in enumerate(rows_csv[:MAX_SCAN_LINES]):
            c = _hit_count(cells)
            if c > hit:
                header_idx, header, hit = i, cells, c
        if hit < 2 or header_idx is None:
            # 识别失败：给前端返回候选列（取前 40 行里单元格数最多的一行）供手动映射
            best = max(rows_csv[:MAX_SCAN_LINES], key=len, default=[]) if rows_csv else []
            return {"source": None, "columns": best, "header_row_hint": rows_csv.index(best) if best else 0,
                    "rows": [], "needs_mapping": True}
        col = _column_map(header)
        if col["date"] is None or (col["amount"] is None and col["income"] is None and col["expense"] is None):
            return {"source": None, "columns": header, "header_row_hint": header_idx,
                    "rows": [], "needs_mapping": True}
        source = detect_source(header)

    rows = []
    for i, cells in enumerate(rows_csv[header_idx + 1:], start=header_idx + 1):
        if not any(str(c).strip() for c in cells):
            continue
        row = _build_row(cells, col, i - header_idx - 1, header)
        if row:
            rows.append(row)

    return {"source": source, "header_row": header_idx, "columns": header, "rows": rows, "needs_mapping": False}
