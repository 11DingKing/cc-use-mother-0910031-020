"""试算引擎：替代后需求、竞争库存分配与风险闸口。

设计要点
--------

1. **替代后需求**：逐车型按 ``短缺件数 × 转换比例`` 计算替代料净需求；
   局部适用矩阵中未覆盖或标记不可用的车型形成残余缺口（APPLICABILITY_GAP）。
2. **竞争分配**：多个短缺竞争同一替代料时，按优先级分配库存池
   （已生效/已下达先占，待确认按确定性次序），净需求未被满足即
   SUPPLY_INSUFFICIENT，并逐项给出每个竞争者的占用与自身缺口。
3. **风险闸口**：工程适配、供应可用量、客户限制三类证据独立维护，
   任一未确认/未放行即阻断；法定人数逐条核对角色人数与去重角色数；
   签署的依据版本落后则失效（SIGNATURE_STALE）。
4. **试算有效性**：库存池版本、证据矩阵版本、竞争者指纹三者任一变化，
   旧试算即过期（TRIAL_STALE），发布前必须重新试算。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Optional

from . import reason_codes as rc
from .models import (
    SIGNER_ROLES,
    Candidate,
    State,
)
from .store import Store


# ---- 需求计算 ----

def _ratio_for(candidate: Candidate, vehicle_model: str) -> Optional[float]:
    for row in candidate.applicability:
        if row.vehicle_model == vehicle_model:
            if not row.usable:
                return None
            return row.conversion_ratio
    return None


def compute_requirement(store: Store, candidate: Candidate) -> dict[str, dict[str, float]]:
    """返回 {车型: {shortage_qty, ratio, substitute_need, residual_gap}}。"""
    shortage = store.shortages[candidate.shortage_id]
    result: dict[str, dict[str, float]] = {}
    for line in shortage.demand:
        ratio = _ratio_for(candidate, line.vehicle_model)
        if ratio is None:
            result[line.vehicle_model] = {
                "shortage_qty": line.shortage_qty,
                "ratio": 0.0,
                "substitute_need": 0.0,
                "residual_gap": line.shortage_qty,
            }
        else:
            need = round(line.shortage_qty * ratio, 6)
            result[line.vehicle_model] = {
                "shortage_qty": line.shortage_qty,
                "ratio": ratio,
                "substitute_need": need,
                "residual_gap": 0.0,
            }
    return result


def total_need(requirement: dict[str, dict[str, float]]) -> float:
    return round(sum(row["substitute_need"] for row in requirement.values()), 6)


def residual_gap(requirement: dict[str, dict[str, float]]) -> float:
    return round(sum(row["residual_gap"] for row in requirement.values()), 6)


# ---- 竞争指纹 ----

_CONSUMING_STATES = (State.PENDING, State.RELEASED, State.FULFILLING)


def competition_fingerprint(store: Store, material: str) -> str:
    """竞争者集合指纹：参与分配的候选状态或需求变化即改变。"""
    parts = []
    for cand in store.candidates.values():
        if cand.substitute_material != material or cand.state not in _CONSUMING_STATES:
            continue
        need = total_need(cand.requirement) if cand.requirement else 0.0
        parts.append({"id": cand.candidate_id, "state": cand.state.value, "need": need})
    parts.sort(key=lambda x: x["id"])
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ---- 竞争分配 ----

@dataclass
class Consumer:
    candidate_id: str
    need: float
    priority: int  # 越小越优先
    state: str


def _priority(cand: Candidate) -> int:
    # 履行中（已领料）最优先，其次已下达；同级别按登记序号先登记先得
    if cand.state == State.FULFILLING:
        return cand.seq
    if cand.state == State.RELEASED:
        return _BASE_RELEASED + cand.seq
    return _BASE_PENDING + cand.seq


_BASE_RELEASED = 1_000_000_000
_BASE_PENDING = 2_000_000_000


def compute_allocation(store: Store, candidate: Candidate) -> dict[str, dict[str, Any]]:
    """对该候选的替代料池执行竞争分配，返回每个候选的分配明细。

    被试算候选自身始终参与（即使仍在草拟态），以便计划员在提交前
    就能看到"提交后"的竞争结果；其余候选仅在消费态（待确认/已下达/
    履行中）占用库存。
    """
    material = candidate.substitute_material
    pool = store.pool(material)
    total_available = pool.available()

    consumers: list[Consumer] = []
    needs: dict[str, float] = {}
    states: dict[str, str] = {}
    for cand in store.candidates.values():
        if cand.substitute_material != material:
            continue
        if cand.state not in _CONSUMING_STATES and cand.candidate_id != candidate.candidate_id:
            continue
        need = total_need(cand.requirement) if cand.requirement else 0.0
        needs[cand.candidate_id] = need
        states[cand.candidate_id] = cand.state.value
        # 草拟态试算时按"待确认 + 自身序号"模拟其提交后的竞争位置
        if cand.state == State.DRAFT:
            priority = _BASE_PENDING + cand.seq
        else:
            priority = _priority(cand)
        consumers.append(Consumer(cand.candidate_id, need, priority, cand.state.value))

    consumers.sort(key=lambda c: (c.priority, c.candidate_id))

    remaining = total_available
    allocated: dict[str, float] = {}
    for consumer in consumers:
        give = min(max(consumer.need, 0.0), max(remaining, 0.0))
        allocated[consumer.candidate_id] = round(give, 6)
        remaining = round(remaining - give, 6)

    detail: dict[str, dict[str, Any]] = {}
    for consumer in consumers:
        need = needs[consumer.candidate_id]
        got = allocated.get(consumer.candidate_id, 0.0)
        detail[consumer.candidate_id] = {
            "state": states[consumer.candidate_id],
            "need": need,
            "allocated": got,
            "short": round(max(need - got, 0.0), 6),
            "priority": consumer.priority,
            "self": consumer.candidate_id == candidate.candidate_id,
        }
    detail["__pool__"] = {
        "material": material,
        "on_hand": pool.on_hand,
        "inbound": dict(pool.inbound),
        "available": round(total_available, 6),
        "pool_version": pool.version,
        "competitor_count": len(consumers),
    }
    return detail


# ---- 法定人数 ----

def evaluate_quorum(store: Store, candidate: Candidate) -> tuple[bool, dict[str, Any]]:
    rule = store.quorum_rules.get(candidate.quorum_rule_id)
    if rule is None:
        return False, {"rule_id": candidate.quorum_rule_id, "error": rc.E_QUORUM_RULE_UNKNOWN}

    # 仅统计基于当前依据版本的有效签署
    valid = [s for s in candidate.signatures if s.basis_version == candidate.evidence_version]
    counts: dict[str, int] = {role: 0 for role in SIGNER_ROLES}
    signers: dict[str, list[str]] = {role: [] for role in SIGNER_ROLES}
    for sig in valid:
        if sig.role in counts:
            counts[sig.role] += 1
            signers[sig.role].append(sig.signer)

    missing_roles: list[str] = []
    for role, required in rule.required_counts.items():
        if counts.get(role, 0) < required:
            missing_roles.append(role)
    distinct_roles = len({s.role for s in valid if s.role in rule.required_counts})
    quorum_ok = not missing_roles and distinct_roles >= rule.min_roles

    return quorum_ok, {
        "rule_id": rule.rule_id,
        "required_counts": dict(rule.required_counts),
        "actual_counts": counts,
        "signers": signers,
        "missing_roles": missing_roles,
        "distinct_roles": distinct_roles,
        "min_roles": rule.min_roles,
        "stale_signatures": len(candidate.signatures) - len(valid),
    }


# ---- 风险闸口汇总 ----

def _risk(code: str, blocking: bool, detail: Any) -> dict[str, Any]:
    return {"code": code, "blocking": blocking, "message": rc.MESSAGES[code], "detail": detail}


def evaluate_risks(store: Store, candidate: Candidate) -> list[dict[str, Any]]:
    risks: list[dict[str, Any]] = []

    # 1. 适用矩阵 / 局部适用残余
    gap_models = [m for m, row in candidate.requirement.items() if row["residual_gap"] > 0]
    if gap_models:
        risks.append(_risk(rc.E_APPLICABILITY_GAP, True, {"uncovered_models": gap_models}))

    # 2~4. 三类独立证据
    ev = candidate.evidence
    if not ev.engineering_confirmed:
        risks.append(_risk(rc.E_ENGINEERING_UNCONFIRMED, True, {"docs": list(ev.engineering)}))
    if not ev.supply_confirmed:
        risks.append(_risk(rc.E_SUPPLY_UNCONFIRMED, True, {"docs": list(ev.supply)}))
    if not ev.customer_approved:
        risks.append(_risk(rc.E_CUSTOMER_RESTRICTED, True, {"docs": list(ev.customer)}))

    # 5. 签署失效
    stale = [s.to_dict() for s in candidate.signatures if s.basis_version != candidate.evidence_version]
    if stale:
        risks.append(_risk(rc.E_SIGNATURE_STALE, True, {"stale_signatures": stale,
                                                        "current_version": candidate.evidence_version}))

    # 6. 法定人数
    quorum_ok, quorum_detail = evaluate_quorum(store, candidate)
    if not quorum_ok:
        risks.append(_risk(
            rc.E_QUORUM_RULE_UNKNOWN if quorum_detail.get("error") else rc.E_QUORUM_MISSING,
            True, quorum_detail,
        ))

    # 7. 竞争供给
    alloc = candidate.allocation
    own = alloc.get(candidate.candidate_id)
    if own is None or own["short"] > 0:
        competitors = [
            {"candidate_id": cid, **{k: v for k, v in d.items() if k != "self"}}
            for cid, d in alloc.items()
            if cid != "__pool__" and not d.get("self") and d["allocated"] > 0
        ]
        risks.append(_risk(rc.E_SUPPLY_INSUFFICIENT, True, {
            "self": own,
            "pool": alloc.get("__pool__"),
            "competitors_occupying": competitors,
        }))

    return risks


def blocking_codes(risks: list[dict[str, Any]]) -> list[str]:
    return [r["code"] for r in risks if r["blocking"]]


def trial_basis(store: Store, candidate: Candidate) -> dict[str, int | str]:
    pool = store.pool(candidate.substitute_material)
    return {
        "stock_version": pool.version,
        "evidence_version": candidate.evidence_version,
        "competition_fingerprint": competition_fingerprint(store, candidate.substitute_material),
    }
