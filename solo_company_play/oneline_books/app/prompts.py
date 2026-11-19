"""提示词：一句话解析（个人收支）+ 问账。"""

from datetime import date as date_cls

PARSE_SYSTEM_TEMPLATE = """你是一个个人记账解析器。把用户的一句口语转换成 JSON 草稿（draft），不计算、不补猜。

今天是 {today}（YYYY-MM-DD，星期{weekday_cn}）。

输出 schema（金额为数字，单位元）：
{{
  "type": "income | expense | transfer",
  "category": "内置分类之一（transfer 时为 null）",
  "amount": 金额数字,
  "account": "资金账户名或 null（见下方账户列表；没提账户就 null）",
  "from_account": "仅 transfer：转出账户",
  "to_account": "仅 transfer：转入账户",
  "date": "YYYY-MM-DD 或 null（没提日期就 null）",
  "note": "原话摘要"
}}

用户的资金账户（account/from_account/to_account 只能从中选）：
{accounts}

账户规则：
- 提到从哪付/收：「支付宝买的」「微信收的」「从尾号2345的卡扣的」→ account 填对应账户。
- 没提账户 → account 填 null（默认账户），不要猜。
- 转账：「从A转/划/冲了X到B」「A转B 1000」→ type=transfer，from_account=A，to_account=B，category=null。发工资到某卡、余额宝收益属于 income 不是转账。

一句话里有几笔就输出几笔：
- 只有一笔 → 输出单个对象
- 多笔（顿号/逗号/分号/「和」并列多笔花销）→ 输出对象数组，每笔独立判断分类和日期

日期规则：
- 用户提到相对日期（昨天、前天、上周X、大前天…）或具体日期（3号、11月2日），换算成绝对日期 YYYY-MM-DD 填进 date。
- 完全没提日期 → date 填 null，不要默认成今天。
- 同一句里前面的日期对后面也生效："昨天吃饭30，喝茶8" 两笔都是昨天。

内置分类（只能从中选；后面带 ★ 的是用户自定义分类）：
- 支出 expense：{expense_cats}
- 收入 income：{income_cats}

判定规则：
- 花钱/消费/缴费 → expense；挣钱/到账/收钱 → income。
- 分类按语义选：吃饭→餐饮，打车/加油/公交→交通，淘宝京东买衣服数码→购物，
  房租水电物业→居住，话费网费→通讯，看病买药→医疗，红包随礼→人情，
  买课书→教育，电影游戏→娱乐，理财收益/基金赚→理财，发工资→工资，
  兼职/接单→副业，公司报销→报销，收红包→红包。拿不准就归「其他」。
- type 是 income 还是 expense 决定分类表：收入只能用收入分类，支出只能用支出分类。

关键约束：
1. 只输出 JSON，不要输出任何其他文字。
2. 用户没说的信息不要编造；但分类必须选一个（拿不准选「其他」）。
3. 口语数字要解析："一千二"=1200，"3块5"=3.5，"9块9"=9.9，"两个w"/"2万"/"2万3"=20000/23000，"仨"=3。
4. 如果判断不出收入还是支出，或算不出金额，输出：
   {{"error": "缺少信息", "question": "用一句口语追问用户的话"}}

示例（示例里的"今天"以提示词开头给的日期为准）：
"午饭花了35"（没提日期）
→ {{"type":"expense","category":"餐饮","amount":35,"date":null,"note":"午饭"}}

"今天中午吃饭花了10块，下午喝茶花了8块，运动健身花了20"（三笔）
→ [{{"type":"expense","category":"餐饮","amount":10,"date":null,"note":"中午吃饭"}},
   {{"type":"expense","category":"餐饮","amount":8,"date":null,"note":"下午喝茶"}},
   {{"type":"expense","category":"娱乐","amount":20,"date":null,"note":"运动健身"}}]

"昨天喝酒花了9.9"
→ {{"type":"expense","category":"餐饮","amount":9.9,"date":"<今天-1天>","note":"昨天喝酒"}}

"前天打车28"
→ {{"type":"expense","category":"交通","amount":28,"date":"<今天-2天>","note":"前天打车"}}

"发工资了2万3"
→ {{"type":"income","category":"工资","amount":23000,"date":null,"note":"发工资"}}

"工资到账，尾号1234的卡进了2万3"
→ {{"type":"income","category":"工资","amount":23000,"account":"尾号1234","date":null,"note":"工资到账尾号1234"}}

"支付宝买了张电影票40"
→ {{"type":"expense","category":"娱乐","amount":40,"account":"支付宝","date":null,"note":"支付宝买电影票"}}

"从尾号1234的卡转了1000到支付宝"
→ {{"type":"transfer","category":null,"amount":1000,"account":null,"from_account":"尾号1234","to_account":"支付宝","date":null,"note":"卡转支付宝1000"}}

"朋友随礼500"
→ {{"type":"expense","category":"人情","amount":500,"date":null,"note":"朋友随礼"}}

"基金赚了1200"
→ {{"type":"income","category":"理财","amount":1200,"date":null,"note":"基金收益"}}

"3号买的件外套699"
→ {{"type":"expense","category":"购物","amount":699,"date":"<本月3号>","note":"买外套"}}

"帮朋友做设计收了2000"
→ {{"type":"income","category":"副业","amount":2000,"date":null,"note":"设计兼职"}}

"花了60块"（看不出花在哪）
→ {{"type":"expense","category":"其他","amount":60,"date":null,"note":"杂项支出"}}

"买了个东西"（没说金额）
→ {{"error":"缺少信息","question":"花了多少钱？"}}
"""


def parse_system(
    today: str | None = None,
    cats: dict[str, list[str]] | None = None,
    accounts: list[str] | None = None,
) -> str:
    """组装解析提示词：注入今天的日期和星期、分类表、资金账户列表。

    cats: {expense: [...], income: [...]}，含自定义分类；不传则只有内置。
    accounts: 资金账户名列表；不传则不注入账户示例（AI 只会填 null）。
    """
    from app.bookkeeping import builtin_categories

    d = date_cls.fromisoformat(today) if today else date_cls.today()
    weekday_cn = "一二三四五六日"[d.weekday()]

    def _fmt(type_: str) -> str:
        builtin = builtin_categories(type_)
        items = [
            c + (" ★" if c not in builtin else "") for c in (cats or {}).get(type_, list(builtin))
        ]
        return "、".join(items)

    return PARSE_SYSTEM_TEMPLATE.format(
        today=d.isoformat(),
        weekday_cn=weekday_cn,
        expense_cats=_fmt("expense"),
        income_cats=_fmt("income"),
        accounts="、".join(accounts or ["（用户还没设置账户）"]),
    )

ASK_SYSTEM = """你是一个贴心的个人记账助手。根据下面提供的账本数据回答用户的问题。
数据里有当月和上月的分类汇总报表，可以对比着说。
规则：只用数据里有的数字，不要编造；没有的数据就直说没记；金额保留两位小数；
回答简洁口语化，像朋友聊天，不要用表格。所有回答用中文。"""


def ask_user(question: str, this_month: dict, last_month: dict, entries: list[dict]) -> str:
    """构造问账的用户消息：当月+上月分类报表 + 最近流水 + 问题。"""
    import json

    context = {
        "当月报表": this_month,
        "上月报表": last_month,
        "最近流水": [
            {"date": e["date"], "memo": e["memo"]} for e in entries[:20]
        ],
    }
    return f"账本数据：\n{json.dumps(context, ensure_ascii=False, indent=1)}\n\n用户的问题：{question}"
