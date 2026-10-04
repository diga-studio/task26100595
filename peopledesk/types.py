"""PeopleDesk Lite 公共类型定义。"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date


class LeaveType(str, enum.Enum):
    ANNUAL = "annual"      # 年假, 每年 15 天
    SICK = "sick"          # 病假, 每年 10 天
    PERSONAL = "personal"  # 事假, 每年 3 天
    UNPAID = "unpaid"      # 无薪假, 不限额


ANNUAL_QUOTAS: dict[LeaveType, "int | None"] = {
    LeaveType.ANNUAL: 15,
    LeaveType.SICK: 10,
    LeaveType.PERSONAL: 3,
    LeaveType.UNPAID: None,  # 不限额
}


class VersionStatus(str, enum.Enum):
    PENDING = "pending"          # 待审
    EFFECTIVE = "effective"      # 生效
    SUPERSEDED = "superseded"    # 已被新版本替代
    REJECTED = "rejected"        # 已拒绝


class Decision(str, enum.Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class PeopleDeskError(Exception):
    """业务规则冲突的基类。"""


class QuotaExceededError(PeopleDeskError):
    """额度不足。"""


class DateConflictError(PeopleDeskError):
    """日期占用与其他请假单相交。"""


class StaleBaseVersionError(PeopleDeskError):
    """变更依据的生效版本已过期。"""


class PendingExistsError(PeopleDeskError):
    """该请假单已存在待审版本。"""


class NotFoundError(PeopleDeskError):
    """请假单 / 版本 / 送审编号不存在。"""


class DecisionConflictError(PeopleDeskError):
    """审批结论与已有结论相反。"""


class PeopleDeskStateError(PeopleDeskError):
    """内部状态不一致(防御性检查)。"""


@dataclass(frozen=True)
class VersionInfo:
    version_id: int
    submission_id: str
    leave_type: LeaveType
    start_date: date
    end_date: date
    status: VersionStatus
    base_version_id: int | None


@dataclass(frozen=True)
class RequestInfo:
    request_id: str
    employee_id: str
    effective: VersionInfo | None
    pending: VersionInfo | None


@dataclass(frozen=True)
class TypeBalance:
    leave_type: LeaveType
    quota: "int | None"        # None 表示不限额
    used: int                  # 已批准(生效版本)用量
    extra_reserved: int        # 待审版本带来的额外预占
    available: "int | None"    # 可用余额, 不限额时为 None


@dataclass(frozen=True)
class YearBalance:
    employee_id: str
    year: int
    balances: tuple[TypeBalance, ...]
