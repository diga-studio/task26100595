# PeopleDesk Lite

基于 Python + SQLite 的请假核算库, 由上层直接调用, 全部业务状态保存在单个 SQLite 文件中。

## 能力概览

- **提交**: `submit_new` 新请假(立即预占)、`submit_change` 变更(原版本送审期间仍生效)。
- **审批**: `approve` / `reject`, 结论永久不可变, 重复相同结论幂等返回, 相反结论抛 `DecisionConflictError`。
- **查询**: `get_request` / `list_requests` 查看生效与待审版本, `get_balance` 查看各年度各假种的已用、额外预占与可用余额。

## 业务规则要点

- 额度: 年假 15 天 / 病假 10 天 / 事假 3 天, 按员工 × 公历年 × 假种分别核算, 不结转; 无薪假不限额(只展示用量)。
- 计费: 区间含首尾, 周一至周五每天 1 天, 周末不计费, 跨年按工作日所在年份分摊。
- 占用: 区间覆盖所有自然日(含周末与无薪假); 同一单的占用 = 生效 ∪ 待审版本日期; 不同单的占用不得相交。
- 额度占用: 每单每(年, 假种)取 max(生效用量, 待审用量), 全部单合计不得超过额度; 提交整体成功或整体失败。
- 送审编号(如 `SB00000003`)全局唯一且永不复用; 对旧编号的审批只返回原结论, 不影响新待审版本。

## 快速开始

```python
from datetime import date
from peopledesk import PeopleDesk, LeaveType

with PeopleDesk("leave.db") as desk:
    # 提交新请假(立即预占日期与额度)
    req = desk.submit_new("E001", LeaveType.ANNUAL, date(2026, 1, 5), date(2026, 1, 9))
    submission = req.pending.submission_id

    # 审批
    desk.approve(submission)            # -> "approved"

    # 变更: 必须指明依据的生效版本
    base = desk.get_request(req.request_id).effective.version_id
    changed = desk.submit_change(req.request_id, base, LeaveType.ANNUAL,
                                 date(2026, 1, 5), date(2026, 1, 13))
    desk.reject(changed.pending.submission_id)   # 拒绝后原版本不变

    # 查询余额
    balance = desk.get_balance("E001", 2026)
    for entry in balance.balances:
        print(entry.leave_type.value, entry.used, entry.extra_reserved, entry.available)
```

## 异常

全部继承 `PeopleDeskError`: `QuotaExceededError`(额度不足)、`DateConflictError`(日期相交)、
`StaleBaseVersionError`(依据版本已过期)、`PendingExistsError`(已有待审版本)、
`NotFoundError`(单/版本/送审编号不存在)、`DecisionConflictError`(审批结论冲突)。
失败的提交整体回滚, 不留下待审版本或部分扣减。

## 并发与持久化

- 面向 200 名员工、同进程 16 并发调用设计; 调用方可各自用 `PeopleDesk(path)` 打开同一文件。
- 写操作走 `BEGIN IMMEDIATE` 事务串行化, 结果等价于某个逐个执行顺序, 不会以数据库忙代替业务结论。
- 版本切换与额度账目在同一事务提交, 查询不会看到中间状态; 关闭重开后版本、审批结论与余额一致。

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

## 代码结构

- `peopledesk/types.py` — 枚举、数据类与异常定义
- `peopledesk/dates.py` — 工作日计费与自然日展开
- `peopledesk/db.py` — SQLite schema 与连接管理
- `peopledesk/service.py` — 提交 / 审批 / 查询业务逻辑
- `tests/` — 变更核算、争抢占用、审批竞争与持久化测试
