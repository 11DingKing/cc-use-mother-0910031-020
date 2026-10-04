"""短缺替代料审批领域模型。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


class Role:
    """契约约定的四类参与角色。"""

    PLANNER = "采购计划员"
    SUPPLIER = "供应商"
    QUALITY = "质量工程师"
    WAREHOUSE = "仓储管理员"

    ALL = (PLANNER, SUPPLIER, QUALITY, WAREHOUSE)


class EventState:
    """契约约定的短缺事件状态机。"""

    DRAFT = "草拟"
    PENDING = "待确认"
    RELEASED = "已下达"
    FULFILLING = "履行中"
    CLOSED = "已关闭"


class FitStatus:
    """工程适配结论。"""

    UNSET = "未评估"
    FIT = "适配"
    UNFIT = "不适配"

    ALL = (UNSET, FIT, UNFIT)


class RiskLevel:
    """试算风险等级。"""

    LOW = "低"
    MEDIUM = "中"
    HIGH = "高"


# 多方审批法定数：各角色所需的最少有效签署数
DEFAULT_QUORUM = {Role.QUALITY: 1, Role.SUPPLIER: 1, Role.WAREHOUSE: 1}


@dataclass
class Evidence:
    """验证证据：台架试验、道路试验、客户认可等。"""

    evidence_id: str
    kind: str
    reference: str
    vehicle_models: tuple[str, ...]  # 空元组表示覆盖全部适用车型
    provided_by: str
    provided_at: str
    valid: bool = True

    def covers(self, model: str) -> bool:
        return self.valid and (not self.vehicle_models or model in self.vehicle_models)


@dataclass
class SubstituteCandidate:
    """候选替代料及其三方分别维护的确认信息。"""

    candidate_id: str
    event_id: str
    substitute_part: str
    seq: int
    ratio: float = 1.0  # 1 件原零件所需替代料数量
    applicable_models: tuple[str, ...] = ()  # 空元组表示全部涉及车型
    fit_status: str = FitStatus.UNSET  # 质量工程师维护
    supplier_qty: int = 0  # 供应商维护的确认可用量
    evidences: list[Evidence] = field(default_factory=list)

    def models_for(self, event: ShortageEvent) -> list[str]:
        if not self.applicable_models:
            return sorted(event.demand_by_model)
        return sorted(self.applicable_models)

    def needed_qty(self, event: ShortageEvent) -> int:
        demand = sum(event.demand_by_model[m] for m in self.models_for(event))
        return math.ceil(round(demand * self.ratio, 6))


@dataclass
class Signature:
    """审批签署，可撤回。"""

    signer: str
    role: str
    signed_at: str
    withdrawn: bool = False
    withdrawn_at: str | None = None


@dataclass
class FrozenBasis:
    """生效时冻结的依据快照，发布后不可变。"""

    basis_id: str
    event_id: str
    frozen_at: str
    released_candidates: list[str]
    candidate_parts: dict[str, str]
    demand_by_model: dict[str, int]
    inventory_allocation: dict[str, int]  # candidate_id -> 冻结的库存分配
    inventory_levels: dict[str, int]  # 替代料号 -> 冻结时库存
    supplier_qtys: dict[str, int]  # candidate_id -> 冻结时供应确认量
    evidence_refs: dict[str, list[str]]  # candidate_id -> 有效证据引用
    signatures: list[dict]  # 冻结时有效签署
    report: dict  # 冻结时试算报告
    basis_hash: str = ""
    superseded: bool = False


@dataclass
class ShortageEvent:
    """短缺事件及其候选替代、签署与生效依据。"""

    event_id: str
    part_number: str
    demand_by_model: dict[str, int]
    needed_by: str  # 需求日期 YYYY-MM-DD
    customers: tuple[str, ...]
    created_by: str
    created_at: str
    seq: int
    state: str = EventState.DRAFT
    candidates: list[SubstituteCandidate] = field(default_factory=list)
    signatures: list[Signature] = field(default_factory=list)
    issued: dict[str, int] = field(default_factory=dict)  # candidate_id -> 已领数量
    reevaluation_pending: bool = False
    reevaluation_reasons: list[str] = field(default_factory=list)
    frozen_basis: FrozenBasis | None = None
    last_report: dict | None = None

    @property
    def required_qty(self) -> int:
        return sum(self.demand_by_model.values())

    def candidate(self, candidate_id: str) -> SubstituteCandidate | None:
        for cand in self.candidates:
            if cand.candidate_id == candidate_id:
                return cand
        return None


@dataclass
class CustomerRestriction:
    """客户限制：按客户、车型、原零件分别维护的禁用替代料。"""

    customer: str
    vehicle_model: str
    original_part: str
    banned_substitutes: tuple[str, ...]
    note: str
    maintained_by: str
    maintained_at: str
