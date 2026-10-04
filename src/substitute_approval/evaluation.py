"""试算引擎：替代后需求、风险等级与竞争库存分配。"""
from __future__ import annotations

from .models import EventState, FitStatus, RiskLevel, ShortageEvent, SubstituteCandidate
from .store import Store


def check_item(name: str, accepted: bool, reasons: list[str] | None = None, blocking: bool = True) -> dict:
    """逐项检查结论：被接受或拒绝，附原因。"""
    return {
        "item": name,
        "accepted": bool(accepted),
        "blocking": bool(blocking),
        "reasons": list(reasons or []),
    }


def fit_reasons(candidate: SubstituteCandidate) -> list[str]:
    if candidate.fit_status == FitStatus.FIT:
        return []
    if candidate.fit_status == FitStatus.UNSET:
        return ["工程适配未确认"]
    return ["工程判定不适配"]


def evidence_reasons(event: ShortageEvent, candidate: SubstituteCandidate) -> list[str]:
    return [
        f"车型 {model} 缺少有效验证证据"
        for model in candidate.models_for(event)
        if not any(e.covers(model) for e in candidate.evidences)
    ]


def customer_reasons(store: Store, event: ShortageEvent, candidate: SubstituteCandidate) -> list[str]:
    reasons = []
    models = candidate.models_for(event)
    for rule in store.restrictions:
        if rule.original_part != event.part_number:
            continue
        if rule.vehicle_model not in models or rule.customer not in event.customers:
            continue
        if candidate.substitute_part in rule.banned_substitutes:
            note = f"（{rule.note}）" if rule.note else ""
            reasons.append(
                f"客户 {rule.customer} 禁止替代料 {candidate.substitute_part} "
                f"用于车型 {rule.vehicle_model}{note}"
            )
    return reasons


def hard_failures(store: Store, event: ShortageEvent, candidate: SubstituteCandidate) -> list[str]:
    """阻断性失败：工程适配、验证证据、客户限制任一不通过。"""
    return (
        fit_reasons(candidate)
        + evidence_reasons(event, candidate)
        + customer_reasons(store, event, candidate)
    )


def frozen_reservation(store: Store, part: str) -> int:
    """已下达/履行中事件的冻结依据所占用的库存。"""
    total = 0
    for event in store.events.values():
        if event.state not in (EventState.RELEASED, EventState.FULFILLING):
            continue
        basis = event.frozen_basis
        if basis is None or basis.superseded:
            continue
        for candidate_id, qty in basis.inventory_allocation.items():
            if basis.candidate_parts.get(candidate_id) == part:
                total += max(qty - event.issued.get(candidate_id, 0), 0)
    return total


def compute_allocation(store: Store, part: str) -> dict[str, dict]:
    """在竞争同一替代料的待确认短缺间按优先级分配库存。

    优先级：需求日期 → 登记顺序 → 事件编号 → 候选顺序。
    返回 {candidate_id: {"grant": 分配数量, "ahead": 占用库存的更高优先级事件}}。
    """
    available = max(store.inventory.get(part, 0) - frozen_reservation(store, part), 0)
    contenders = []
    for event in store.events.values():
        if event.state != EventState.PENDING:
            continue
        for candidate in event.candidates:
            if candidate.substitute_part != part:
                continue
            if hard_failures(store, event, candidate):
                continue  # 前置确认未通过，不参与库存分配
            need = max(candidate.needed_qty(event) - candidate.supplier_qty, 0)
            if need:
                contenders.append(
                    ((event.needed_by, event.seq, event.event_id, candidate.seq), event, candidate, need)
                )
    contenders.sort(key=lambda entry: entry[0])
    result: dict[str, dict] = {}
    consumers: list[str] = []
    for _, event, candidate, need in contenders:
        grant = min(need, available)
        available -= grant
        result[candidate.candidate_id] = {
            "grant": grant,
            "ahead": sorted(set(consumers) - {event.event_id}),
        }
        if grant:
            consumers.append(event.event_id)
    return result


def evaluate_candidate(
    store: Store,
    event: ShortageEvent,
    candidate: SubstituteCandidate,
    allocation: dict[str, dict],
) -> dict:
    fit = fit_reasons(candidate)
    evidence = evidence_reasons(event, candidate)
    customer = customer_reasons(store, event, candidate)
    items = [
        check_item("工程适配", not fit, fit),
        check_item("验证证据覆盖", not evidence, evidence),
        check_item("客户限制", not customer, customer),
    ]
    models = candidate.models_for(event)
    uncovered_models = [m for m in sorted(event.demand_by_model) if m not in models]
    items.append(
        check_item(
            "适用车型覆盖",
            not uncovered_models,
            [f"局部适用：未覆盖车型 {m}（短缺 {event.demand_by_model[m]} 件）" for m in uncovered_models],
            blocking=False,
        )
    )
    need = candidate.needed_qty(event)
    alloc = allocation.get(candidate.candidate_id, {})
    grant = alloc.get("grant", 0)
    ahead = alloc.get("ahead", [])
    covered = min(need, candidate.supplier_qty + grant)
    hard_ok = not (fit or evidence or customer)
    supply_reasons = []
    if not hard_ok:
        supply_reasons.append("前置确认未通过，不参与库存分配")
    if covered < need:
        supply_reasons.append(
            f"供应缺口 {need - covered} 件"
            f"（需求 {need}，供应确认 {candidate.supplier_qty}，库存分配 {grant}）"
        )
    if ahead and grant < max(need - candidate.supplier_qty, 0):
        supply_reasons.append(f"替代料 {candidate.substitute_part} 被更高优先级短缺 {'、'.join(ahead)} 占用")
    items.append(check_item("供应可用量", hard_ok and covered > 0, supply_reasons))

    accepted = all(i["accepted"] for i in items if i["blocking"])
    if not accepted:
        risk = RiskLevel.HIGH
    elif covered >= need and not uncovered_models:
        risk = RiskLevel.LOW
    elif covered >= need or covered * 2 >= need:
        risk = RiskLevel.MEDIUM
    else:
        risk = RiskLevel.HIGH
    return {
        "candidate_id": candidate.candidate_id,
        "substitute_part": candidate.substitute_part,
        "accepted": accepted,
        "risk_level": risk,
        "needed_qty": need,
        "covered_qty": covered,
        "uncovered_qty": need - covered,
        "covered_models": models,
        "uncovered_models": uncovered_models,
        "supplier_qty": candidate.supplier_qty,
        "inventory_grant": grant,
        "items": items,
    }


def evaluate_event(store: Store, event: ShortageEvent, quorum: dict[str, int], now: str) -> dict:
    """试算替代后需求与风险，输出事件级与候选级的逐项结论。"""
    items = [
        check_item(
            "候选替代存在",
            bool(event.candidates),
            [] if event.candidates else ["尚未登记候选替代料"],
        )
    ]
    active = [s for s in event.signatures if not s.withdrawn]
    quorum_reasons = []
    for role, required in quorum.items():
        have = sum(1 for s in active if s.role == role)
        if have < required:
            quorum_reasons.append(f"{role}有效签署 {have}/{required}")
    items.append(check_item("审批法定人数", not quorum_reasons, quorum_reasons))

    allocation: dict[str, dict] = {}
    for part in sorted({c.substitute_part for c in event.candidates}):
        allocation.update(compute_allocation(store, part))
    candidates = [evaluate_candidate(store, event, c, allocation) for c in event.candidates]

    covered_models: set[str] = set()
    for candidate, report in zip(event.candidates, candidates):
        if report["accepted"]:
            covered_models.update(candidate.models_for(event))
    residual = [m for m in sorted(event.demand_by_model) if m not in covered_models]
    items.append(
        check_item(
            "替代覆盖完整性",
            not residual,
            [f"车型 {m} 短缺 {event.demand_by_model[m]} 件无可用替代覆盖" for m in residual],
            blocking=False,
        )
    )

    own_parts = {c.substitute_part for c in event.candidates}
    competing = sorted(
        other.event_id
        for other in store.events.values()
        if other.event_id != event.event_id
        and other.state in (EventState.PENDING, EventState.RELEASED, EventState.FULFILLING)
        and own_parts & {c.substitute_part for c in other.candidates}
    )
    accepted = all(i["accepted"] for i in items if i["blocking"]) and any(
        c["accepted"] for c in candidates
    )
    return {
        "event_id": event.event_id,
        "evaluated_at": now,
        "accepted": accepted,
        "items": items,
        "candidates": candidates,
        "competing_events": competing,
    }
