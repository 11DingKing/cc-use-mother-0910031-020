"""领域模型。

状态机（对应契约 states）：

    草拟 --提交--> 待确认 --发布(试算全通过)--> 已下达
                        ^                          | 生效（冻结依据）
                        |  触发重评且试算失败       v
                        +---------------------- 履行中 --完成--> 已关闭

"未经完整确认的替代可能直接进入领料" 在本模型中是不可能的：
只有 ``已下达`` 之后才能 ``生效``，而发布要求全部风险闸口通过；
履行中触发重评失败时，已下达资格被撤回并补"履行中风险"告警，
但冻结依据保持不可变，便于追溯与处置。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


# ---- 与领域契约 domain/contract.json 保持一致的角色与状态 ----

PLANNER = "采购计划员"
SUPPLIER = "供应商"
QUALITY = "质量工程师"
WAREHOUSE = "仓储管理员"

ROLES = (PLANNER, SUPPLIER, QUALITY, WAREHOUSE)
# 签署所代表的三方确认（客户限制由质量工程师会签确认）
SIGNER_ROLES = (QUALITY, SUPPLIER, WAREHOUSE)


class State(str, Enum):
    DRAFT = "草拟"
    PENDING = "待确认"
    RELEASED = "已下达"
    FULFILLING = "履行中"
    CLOSED = "已关闭"


# ---- 原因码 ----

class Reason(str, Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---- 明细模型 ----

@dataclass
class Applicability:
    """替代适用矩阵中的一个车型行（局部适用）。"""
    vehicle_model: str
    conversion_ratio: float = 1.0
    usable: bool = True
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "vehicle_model": self.vehicle_model,
            "conversion_ratio": self.conversion_ratio,
            "usable": self.usable,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Applicability":
        return cls(
            vehicle_model=str(raw["vehicle_model"]),
            conversion_ratio=float(raw.get("conversion_ratio", 1.0)),
            usable=bool(raw.get("usable", True)),
            note=str(raw.get("note", "")),
        )


@dataclass
class Evidence:
    """验证证据：工程适配、供应可用量、客户限制分别维护。

    三个桶各自登记凭据（doc_id -> 说明），并各自带独立确认状态；
    客户限制以"客户放行"为准，未登记或未放行都会阻断发布。
    """
    engineering: dict[str, str] = field(default_factory=dict)
    supply: dict[str, str] = field(default_factory=dict)
    customer: dict[str, str] = field(default_factory=dict)
    engineering_confirmed: bool = False
    supply_confirmed: bool = False
    customer_approved: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "engineering": dict(self.engineering),
            "supply": dict(self.supply),
            "customer": dict(self.customer),
            "engineering_confirmed": self.engineering_confirmed,
            "supply_confirmed": self.supply_confirmed,
            "customer_approved": self.customer_approved,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Evidence":
        return cls(
            engineering=dict(raw.get("engineering", {})),
            supply=dict(raw.get("supply", {})),
            customer=dict(raw.get("customer", {})),
            engineering_confirmed=bool(raw.get("engineering_confirmed", False)),
            supply_confirmed=bool(raw.get("supply_confirmed", False)),
            customer_approved=bool(raw.get("customer_approved", False)),
        )


@dataclass
class Signature:
    role: str
    signer: str
    at: str
    basis_version: int  # 签署时所依据的证据/矩阵版本，版本前进后签署失效

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "signer": self.signer, "at": self.at, "basis_version": self.basis_version}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Signature":
        return cls(
            role=raw["role"],
            signer=raw["signer"],
            at=raw["at"],
            # 兼容旧字段名 evidence_version
            basis_version=raw.get("basis_version", raw.get("evidence_version", 0)),
        )


@dataclass
class FreezeBasis:
    """正式生效时冻结的依据（不可变快照 + 指纹）。"""
    frozen_at: str
    requirement_snapshot: dict[str, dict[str, float]]
    allocation_snapshot: dict[str, dict[str, Any]]
    evidence: dict[str, Any]
    applicability: list[dict[str, Any]]
    quorum_snapshot: dict[str, Any]
    signatures: list[dict[str, Any]]
    stock_version: int
    basis_hash: str
    triggered_by_shortages: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "frozen_at": self.frozen_at,
            "requirement_snapshot": self.requirement_snapshot,
            "allocation_snapshot": self.allocation_snapshot,
            "evidence": self.evidence,
            "applicability": self.applicability,
            "quorum_snapshot": self.quorum_snapshot,
            "signatures": self.signatures,
            "stock_version": self.stock_version,
            "basis_hash": self.basis_hash,
            "triggered_by_shortages": list(self.triggered_by_shortages),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FreezeBasis":
        return cls(
            frozen_at=raw["frozen_at"],
            requirement_snapshot=raw.get("requirement_snapshot", {}),
            allocation_snapshot=raw.get("allocation_snapshot", {}),
            evidence=raw.get("evidence", {}),
            applicability=raw.get("applicability", []),
            quorum_snapshot=raw.get("quorum_snapshot", {}),
            signatures=raw.get("signatures", []),
            stock_version=raw.get("stock_version", 0),
            basis_hash=raw["basis_hash"],
            triggered_by_shortages=raw.get("triggered_by_shortages", []),
        )


@dataclass
class DemandLine:
    """车型维度的短缺需求行。"""
    vehicle_model: str
    shortage_qty: float  # 被替代原物料的缺口件数

    def to_dict(self) -> dict[str, Any]:
        return {"vehicle_model": self.vehicle_model, "shortage_qty": self.shortage_qty}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DemandLine":
        return cls(vehicle_model=str(raw["vehicle_model"]), shortage_qty=float(raw["shortage_qty"]))


@dataclass
class Shortage:
    """短缺事件。

    一次短缺可能并行提出多个候选替代（``candidate_ids``），
    但同一物料上同一时刻至多一个候选可被发布（互斥锁）。
    """
    shortage_id: str
    material: str
    plant: str
    raised_by: str
    demand: list[DemandLine]
    candidate_ids: list[str] = field(default_factory=list)
    released_candidate_id: Optional[str] = None
    fulfilled_candidate_id: Optional[str] = None
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "shortage_id": self.shortage_id,
            "material": self.material,
            "plant": self.plant,
            "raised_by": self.raised_by,
            "demand": [d.to_dict() for d in self.demand],
            "candidate_ids": list(self.candidate_ids),
            "released_candidate_id": self.released_candidate_id,
            "fulfilled_candidate_id": self.fulfilled_candidate_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Shortage":
        return cls(
            shortage_id=raw["shortage_id"],
            material=raw["material"],
            plant=raw["plant"],
            raised_by=raw["raised_by"],
            demand=[DemandLine.from_dict(x) for x in raw.get("demand", [])],
            candidate_ids=list(raw.get("candidate_ids", [])),
            released_candidate_id=raw.get("released_candidate_id"),
            fulfilled_candidate_id=raw.get("fulfilled_candidate_id"),
            created_at=raw.get("created_at", now_iso()),
        )


@dataclass
class Candidate:
    """候选替代方案及其全部审批状态。"""
    candidate_id: str
    shortage_id: str
    substitute_material: str
    default_ratio: float
    applicability: list[Applicability]
    seq: int = 0  # 登记序号：待确认候选之间按先登记先得(FCFS)参与竞争
    evidence: Evidence = field(default_factory=Evidence)
    evidence_version: int = 0
    quorum_rule_id: str = "standard"
    signatures: list[Signature] = field(default_factory=list)
    state: State = State.DRAFT
    requirement: dict[str, dict[str, float]] = field(default_factory=dict)
    allocation: dict[str, dict[str, Any]] = field(default_factory=dict)
    risk: list[dict[str, Any]] = field(default_factory=list)
    risk_gate_version: int = 0
    # 最近一次试算所基于的版本（任一变化都使试算过期）：
    # 库存池版本 / 证据矩阵版本 / 竞争者集合版本
    trial_stock_version: Optional[int] = None
    trial_evidence_version: Optional[int] = None
    trial_comp_version: Optional[str] = None
    # 计划员是否在最近一次依据变化后显式重新试算并看过结果
    trial_confirmed: bool = False
    last_trial_at: Optional[str] = None
    freeze: Optional[FreezeBasis] = None
    warnings: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "shortage_id": self.shortage_id,
            "substitute_material": self.substitute_material,
            "default_ratio": self.default_ratio,
            "seq": self.seq,
            "applicability": [a.to_dict() for a in self.applicability],
            "evidence": self.evidence.to_dict(),
            "evidence_version": self.evidence_version,
            "quorum_rule_id": self.quorum_rule_id,
            "signatures": [v.to_dict() for v in self.signatures],
            "state": self.state.value,
            "requirement": self.requirement,
            "allocation": self.allocation,
            "risk": self.risk,
            "risk_gate_version": self.risk_gate_version,
            "trial_stock_version": self.trial_stock_version,
            "trial_evidence_version": self.trial_evidence_version,
            "trial_comp_version": self.trial_comp_version,
            "trial_confirmed": self.trial_confirmed,
            "last_trial_at": self.last_trial_at,
            "freeze": self.freeze.to_dict() if self.freeze else None,
            "warnings": list(self.warnings),
            "events": list(self.events),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Candidate":
        return cls(
            candidate_id=raw["candidate_id"],
            shortage_id=raw["shortage_id"],
            substitute_material=raw["substitute_material"],
            default_ratio=float(raw.get("default_ratio", 1.0)),
            applicability=[Applicability.from_dict(x) for x in raw.get("applicability", [])],
            evidence=Evidence.from_dict(raw.get("evidence", {})),
            evidence_version=raw.get("evidence_version", 0),
            quorum_rule_id=raw.get("quorum_rule_id", "standard"),
            signatures=[Signature.from_dict(v) for v in raw.get("signatures", [])],
            state=State(raw.get("state", State.DRAFT.value)),
            requirement=raw.get("requirement", {}),
            allocation=raw.get("allocation", {}),
            risk=raw.get("risk", []),
            risk_gate_version=raw.get("risk_gate_version", 0),
            trial_stock_version=raw.get("trial_stock_version"),
            trial_evidence_version=raw.get("trial_evidence_version"),
            trial_comp_version=raw.get("trial_comp_version"),
            trial_confirmed=bool(raw.get("trial_confirmed", False)),
            last_trial_at=raw.get("last_trial_at"),
            freeze=FreezeBasis.from_dict(raw["freeze"]) if raw.get("freeze") else None,
            warnings=list(raw.get("warnings", [])),
            events=list(raw.get("events", [])),
            created_at=raw.get("created_at", now_iso()),
        )
