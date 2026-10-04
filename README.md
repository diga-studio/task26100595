# PeopleDesk Lite — 请假核算库

纯 Python + SQLite 实现的请假核算库，由上层代码直接调用，全部业务状态
保存在单个 SQLite 文件中。员工编号由调用方提供并视为有效。

## 安装与运行

无第三方依赖（仅标准库）。要求 Python 3.10+。

```bash
python3 -m pytest tests/   # 运行自动化测试
```

## 快速上手

```python
from peopledesk import PeopleDesk

desk = PeopleDesk("leave.db")          # 所有状态都在这一个文件里

# 1. 提交新请假（提交即预占额度与日期）
r = desk.submit_request("E001", "annual", "2025-01-06", "2025-01-17")
desk.approve(r["submission_id"])       # 批准 -> 生效；desk.reject(...) 拒绝

# 2. 变更（基于当前生效版本；送审期间原版本继续有效）
req = desk.get_request(r["request_id"])
chg = desk.submit_change(req["request_id"],
                         req["effective"]["version_id"],
                         "annual", "2025-01-06", "2025-01-21")
desk.approve(chg["submission_id"])     # 批准后新版本整体替代原版本

# 3. 查询
desk.get_request(r["request_id"])      # 生效版本 + 待审版本 + 占用日期
desk.get_balance("E001", 2025)         # 各假种 已用/额外预占/可用余额
desk.close()
```

## 业务规则要点

- **额度**：年假 15 天/年、病假 10 天/年、事假 3 天/年，无薪假不限额；
  按员工 × 公历年 × 假种分别核算，不结转。
- **计费**：区间含首尾；周一至周五每天 1 天，周末不计费，无节假日；
  跨年区间按工作日所在年份分摊；无薪假不扣额度。
- **占用**：区间覆盖所有自然日（含周末、含无薪假）。一张单的占用是
  生效版本与待审版本日期的并集；不同单的占用日期不得相交。
- **预占**：每张单对每个（年度, 假种）占用 `max(生效用量, 待审用量)`，
  全部单之和不得超过额度。已批准用量为「已用」，超出部分为「额外预占」。
- **审批**：每次送审有唯一且永久不复用的编号（可传入 `submission_id`，
  缺省自动生成）。结论一旦产生不可改变：重复相同结论返回原结论
  （`replayed: True`），相反结论抛 `ConflictError`。

## API 一览

| 方法 | 说明 |
| --- | --- |
| `submit_request(employee_id, leave_type, start, end, submission_id=None)` | 提交新请假，返回 `request_id / version_id / submission_id` |
| `submit_change(request_id, base_version_id, leave_type, start, end, submission_id=None)` | 基于生效版本提交变更 |
| `approve(submission_id)` / `reject(submission_id)` | 审批，幂等 |
| `get_request(request_id)` | `effective` / `pending` 版本及 `occupied_dates` |
| `get_balance(employee_id, year)` | 各假种 `quota / used / reserved / available`（无薪假 `quota/available` 为 `None`） |

`leave_type ∈ {"annual", "sick", "personal", "unpaid"}`；日期接受
`datetime.date` 或 `"YYYY-MM-DD"` 字符串。

## 异常

均继承 `peopledesk.PeopleDeskError`：

- `QuotaExceededError` — 超出年度额度
- `DateOverlapError` — 与其他单的占用日期相交
- `ConflictError` — 审批结论冲突 / 依据版本已过期 / 已有待审版本 /
  送审编号复用
- `NotFoundError` — 单号或送审编号不存在

额度或日期校验失败时整个提交回滚，不会留下待审版本或部分扣减。

## 并发与一致性

- 数据库使用 WAL 模式 + `BEGIN IMMEDIATE` 事务，写操作串行化；
  同一进程内多个实例（或线程）打开同一文件时，结果等价于某个
  逐个执行顺序，业务冲突以业务异常返回，不会以数据库忙代替结论。
- 余额由版本用量在事务内实时推导，查询不会看到版本已切换而额度
  未更新的中间状态；关闭重开后版本、审批结论与余额保持一致。

## 目录结构

```
peopledesk/
  __init__.py   # 公开 API 导出
  core.py       # PeopleDesk 主类（提交/审批/查询）
  db.py         # SQLite schema 与连接
  dates.py      # 日期工具（工作日计数、跨年分摊、占用展开）
  errors.py     # 业务异常
tests/          # pytest 自动化测试
```
