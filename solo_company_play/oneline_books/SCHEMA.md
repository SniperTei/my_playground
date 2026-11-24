# oneline_books 数据库表结构

SQLite 单文件账本（默认 `data/books.db`），5 张业务表 + 1 张 SQLite 自带的 `sqlite_sequence`。

设计核心：**复式记账（double-entry）**——交易是容器，钱怎么流由分录行表达。

```
draft(JSON) ──record_draft()──> transactions ──1:N──> postings ──N:1──> accounts
names(kind=…) ──UI/AI 白名单校验──> 解析提示词 / 设置页 / 入账校验
```

## transactions（交易）

每笔记账一条，是「发生了什么」的容器。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | INTEGER | PK 自增 | 交易号（流水里的 #N） |
| ref_id | TEXT | NOT NULL **UNIQUE** | 幂等键：重复确认不重复入账；作废冲销固定用 `void-{id}` |
| date | TEXT | NOT NULL | 记账日期 `YYYY-MM-DD`，月报按自然月 `LIKE 'YYYY-MM-%'` 切分 |
| memo | TEXT | NOT NULL | 摘要；note 为空时退化为分类/默认文案（如「借给老王」） |
| draft | TEXT | NOT NULL DEFAULT '{}' | AI 原始草稿 JSON（见下方 draft 类型表），防重复/CSV 导出/前端渲染都用它 |
| created_at | TEXT | NOT NULL | 入库时间（`datetime('now')`） |

## postings（分录行）

复式核心。每笔交易恰好 2 行（简单分录），一行记借、一行记贷，金额总和相等。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | INTEGER | PK 自增 | |
| txn_id | INTEGER | NOT NULL, FK → transactions.id | 所属交易 |
| account | TEXT | NOT NULL, FK → accounts.name | 记在哪个科目上 |
| debit | REAL | NOT NULL DEFAULT 0, CHECK ≥ 0 | 借方金额 |
| credit | REAL | NOT NULL DEFAULT 0, CHECK ≥ 0 | 贷方金额 |

约定：一行只有一边为正（CHECK 由 `Ledger.post()` 保证）；资产/费用借方为正，负债/权益/收入贷方为正。`balance()` = `SUM(debit) - SUM(credit)`。

## accounts（科目表）

所有科目的总目录，`type` 决定借贷方向和报表归属。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| name | TEXT | PK | 科目名，按前缀区分用途（见下方命名约定） |
| type | TEXT | NOT NULL, CHECK ∈ (asset, liability, equity, income, expense) | asset/expense 借方自然；liability/equity/income 贷方自然 |

### 科目命名约定

| 前缀/形态 | type | 用途 | 示例 |
|---|---|---|---|
| （无前缀） | asset | 资金账户 | `现金`、`尾号1234`、`支付宝` |
| （无前缀） | equity | 对冲账户 | `余额调整`（设置余额的差额） |
| `expense:` | expense | 支出分类 | `expense:餐饮`、`expense:token消费` |
| `income:` | income | 收入分类 | `income:工资` |
| `ar:` | asset | 应收（别人欠我） | `ar:老王` |
| `ap:` | liability | 应付（我欠别人） | `ap:小美` |

注意：accounts 只增不减（历史分录引用的科目永远保留），「当前有效的可选集」由 names 注册表管理。

## names（注册表）

UI/AI 层的白名单，一张表复用成多个小注册表。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| kind | TEXT | NOT NULL, UNIQUE(kind, name) | 注册表类别 |
| name | TEXT | NOT NULL | 条目名 |

### kind 一览

| kind | 用途 | 写入时机 |
|---|---|---|
| `account` | 资金账户 | 设置页手动添加 |
| `account:default` | 默认账户（单条） | 设置页点账户名 / 自动 |
| `category:expense` / `category:income` | 自定义分类 | 设置页手动添加 |
| `person` | 往来人名 | 记应收/应付/结清时**自动注册** |

内置分类不进 names，硬编码在 `bookkeeping.py` 的 `EXPENSE_CATEGORIES` / `INCOME_CATEGORIES` 元组里。

## positions（库存批次）

贸易商版遗留的 FIFO 库存模块，**当前未接线**（0 行），保留给将来的库存/订单模块。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| kind | TEXT NOT NULL | 批次类别 |
| ref | TEXT NOT NULL | 关联引用 |
| qty / unit_cost | REAL NOT NULL | 数量 / 单位成本 |
| opened_date | TEXT NOT NULL | 开仓日期 |
| closed_date | TEXT | 平仓日期（NULL = 未平） |
| meta | TEXT NOT NULL DEFAULT '{}' | 元数据 JSON |

## draft 类型（存于 transactions.draft 的 JSON）

| type | 必要字段 | 分录（Dr / Cr） | 计收支 |
|---|---|---|---|
| expense | category, amount, account? | expense:分类 / 账户 | 支出 |
| income | category, amount, account? | 账户 / income:分类 | 收入 |
| transfer | from_account, to_account, amount | 入账账户 / 出账账户 | 否 |
| receivable | person, amount, account? | ar:人 / 账户 | 否 |
| payable（垫付） | person, category, amount | expense:分类 / ap:人 | **支出（发生时）** |
| payable（借入） | person, amount, account? | 账户 / ap:人 | 否 |
| settle | kind(ar/ap), person, amount, account? | ar：账户 / ar:人；ap：ap:人 / 账户 | 否 |
| adjust | account, amount | 账户 / 余额调整（方向看差额） | 否 |

## 设计要点（为什么这样分）

1. **一单多行**：转账/垫付/欠款天然一笔影响多个科目，单表流水表达不了；分录行组合可以表达任何业务，加功能不动表结构（v2→v4 schema 零迁移）
2. **ref_id UNIQUE = 幂等**：网络重试、连点确认不会重复入账
3. **作废 = 反转分录**：不 UPDATE/DELETE，只追加 `void-{id}` 冲销，历史不可改写
4. **draft 存 JSON**：AI 草稿字段一直在演进，JSON 免迁移；且只有整读场景，无需按字段查询
5. **accounts 与 names 分离**：账本内核（只增）与用户可选集（可增删）解耦，删分类/账户不影响历史账
