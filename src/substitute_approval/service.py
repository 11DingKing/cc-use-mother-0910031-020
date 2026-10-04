"""短缺替代料审批应用服务：登记、三方确认、试算、发布与重新评估。"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from . import evaluation
from .models import (
    DEFAULT_QUORUM,
    CustomerRestriction,
    EventState,
    Evidence,
    FitStatus,
    FrozenBasis,
    Role,
    ShortageEvent,
    Signature,
    SubstituteCandidate,
)
from .store import Store

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class ItemResult:
    """逐项结论：被接受或拒绝，附原因。"""

    item: str
    accepted: bool
    blocking: bool = True
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "item": self.item,
            "accepted": self.accepted,
            "blocking": self.blocking,
            "reasons": list(self.reasons),
        }


@dataclass
class ServiceResult:
    accepted: bool
    items: list[ItemResult] = field(default_factory=list)
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "items": [i.to_dict() for i in self.items],
            "data": self.data,
        }


def _pass(item: str, **data) -> ServiceResult:
    return ServiceResult(accepted=True, items=[ItemResult(item, True)], data=data)


def _fail(item: str, *reasons: str) -> ServiceResult:
    return ServiceResult(accepted=False, items=[ItemResult(item, False, True, list(reasons))])


class SubstituteService:
    """短缺替代料审批核心服务。"""

    def __init__(
        self,
        store: Store | None = None,
        clock: Callable[[], str] | None = None,
        quorum: dict[str, int] | None = None,
    ) -> None:
        self.store = store or Store()
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self.quorum = dict(quorum or DEFAULT_QUORUM)

    # ---- 基础工具 ----

    def _now(self, now: str | None) -> str:
        return now or self.clock()

    @staticmethod
    def _role_error(role: str, allowed: tuple[str, ...]) -> ServiceResult | None:
        if role not in Role.ALL:
            return _fail("角色权限", f"未知角色 {role}")
        if role not in allowed:
            return _fail("角色权限", f"{role}无权执行该操作，需{'或'.join(allowed)}")
        return None

    def _event_or_fail(self, event_id: str) -> tuple[ShortageEvent | None, ServiceResult | None]:
        event = self.store.events.get(event_id)
        if event is None:
            return None, _fail("对象存在性", f"短缺事件 {event_id} 不存在")
        return event, None

    def _candidate_or_fail(
        self, candidate_id: str
    ) -> tuple[tuple[ShortageEvent, SubstituteCandidate] | None, ServiceResult | None]:
        found = self.store.find_candidate(candidate_id)
        if found is None:
            return None, _fail("对象存在性", f"候选替代 {candidate_id} 不存在")
        return found, None

    def _touch(self, event: ShortageEvent, reason: str) -> None:
        """生效依据变化：已下达则回退待确认并作废冻结依据，已试算则标记重新评估。"""
        if event.state == EventState.RELEASED:
            event.state = EventState.PENDING
            if event.frozen_basis is not None:
                event.frozen_basis.superseded = True
            event.reevaluation_pending = True
            event.reevaluation_reasons.append(reason)
        elif event.state == EventState.PENDING and event.last_report is not None:
            event.reevaluation_pending = True
            event.reevaluation_reasons.append(reason)

    def _touch_part_users(self, part: str, reason: str, exclude: str | None = None) -> None:
        for event in self.store.events.values():
            if event.event_id == exclude:
                continue
            if any(c.substitute_part == part for c in event.candidates):
                self._touch(event, reason)

    # ---- 登记短缺事件与候选替代 ----

    def register_event(
        self,
        *,
        part_number: str,
        demand_by_model: dict[str, int],
        needed_by: str,
        customers: tuple[str, ...] = (),
        actor: str,
        role: str,
        now: str | None = None,
    ) -> ServiceResult:
        if err := self._role_error(role, (Role.PLANNER,)):
            return err
        problems = []
        if not part_number:
            problems.append("零件号不能为空")
        if not isinstance(demand_by_model, dict) or not demand_by_model:
            problems.append("分车型短缺数量不能为空")
        else:
            for model, qty in demand_by_model.items():
                if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
                    problems.append(f"车型 {model} 短缺数量必须为正整数")
        if not _DATE_RE.match(needed_by or ""):
            problems.append("需求日期须为 YYYY-MM-DD")
        if problems:
            return _fail("登记校验", *problems)
        event = ShortageEvent(
            event_id=self.store.next_id("event", "SH"),
            part_number=part_number,
            demand_by_model=dict(sorted(demand_by_model.items())),
            needed_by=needed_by,
            customers=tuple(customers),
            created_by=actor,
            created_at=self._now(now),
            seq=self.store.counters["event"],
        )
        self.store.events[event.event_id] = event
        return _pass("登记短缺事件", event_id=event.event_id, state=event.state)

    def add_candidate(
        self,
        *,
        event_id: str,
        substitute_part: str,
        ratio: float = 1.0,
        applicable_models: tuple[str, ...] = (),
        actor: str,
        role: str,
        now: str | None = None,
    ) -> ServiceResult:
        if err := self._role_error(role, (Role.PLANNER,)):
            return err
        event, err = self._event_or_fail(event_id)
        if err:
            return err
        if event.state not in (EventState.DRAFT, EventState.PENDING):
            return _fail("事件状态", f"当前状态{event.state}，不能新增候选替代")
        problems = []
        if not substitute_part:
            problems.append("替代料号不能为空")
        if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or ratio <= 0:
            problems.append("替代比例必须为正数")
        unknown = sorted(set(applicable_models) - set(event.demand_by_model))
        if unknown:
            problems.append(f"适用车型不属于本事件：{'、'.join(unknown)}")
        if any(c.substitute_part == substitute_part for c in event.candidates):
            problems.append(f"替代料 {substitute_part} 已存在候选")
        if problems:
            return _fail("登记校验", *problems)
        candidate = SubstituteCandidate(
            candidate_id=self.store.next_id("candidate", "CAND"),
            event_id=event_id,
            substitute_part=substitute_part,
            seq=self.store.counters["candidate"],
            ratio=float(ratio),
            applicable_models=tuple(sorted(applicable_models)),
        )
        event.candidates.append(candidate)
        self._touch(event, f"新增候选替代 {substitute_part}")
        self._touch_part_users(
            substitute_part,
            f"短缺 {event_id} 新增候选，替代料 {substitute_part} 出现竞争",
            exclude=event_id,
        )
        return _pass("登记候选替代", candidate_id=candidate.candidate_id)

    # ---- 工程适配、供应可用量、库存分别维护 ----

    def set_engineering_fit(self, *, candidate_id: str, fit: str, actor: str, role: str, now: str | None = None) -> ServiceResult:
        if err := self._role_error(role, (Role.QUALITY,)):
            return err
        found, err = self._candidate_or_fail(candidate_id)
        if err:
            return err
        event, candidate = found
        if fit not in FitStatus.ALL:
            return _fail("登记校验", f"未知适配结论 {fit}")
        if event.state in (EventState.FULFILLING, EventState.CLOSED):
            return _fail("事件状态", f"当前状态{event.state}，不能变更工程适配")
        candidate.fit_status = fit
        self._touch(event, f"候选 {candidate_id} 工程适配变更为{fit}")
        return _pass("工程适配", candidate_id=candidate_id, fit=fit)

    def set_supply(self, *, candidate_id: str, qty: int, actor: str, role: str, now: str | None = None) -> ServiceResult:
        if err := self._role_error(role, (Role.SUPPLIER,)):
            return err
        found, err = self._candidate_or_fail(candidate_id)
        if err:
            return err
        event, candidate = found
        if not isinstance(qty, int) or isinstance(qty, bool) or qty < 0:
            return _fail("登记校验", "供应确认量必须为非负整数")
        if event.state in (EventState.FULFILLING, EventState.CLOSED):
            return _fail("事件状态", f"当前状态{event.state}，不能变更供应确认量")
        candidate.supplier_qty = qty
        self._touch(event, f"候选 {candidate_id} 供应确认量变化为 {qty}")
        return _pass("供应可用量", candidate_id=candidate_id, qty=qty)

    def set_inventory(self, *, part: str, qty: int, actor: str, role: str, now: str | None = None) -> ServiceResult:
        if err := self._role_error(role, (Role.WAREHOUSE,)):
            return err
        if not isinstance(qty, int) or isinstance(qty, bool) or qty < 0:
            return _fail("登记校验", "库存数量必须为非负整数")
        self.store.inventory[part] = qty
        self._touch_part_users(part, f"替代料 {part} 库存变化为 {qty}")
        return _pass("库存变化", part=part, qty=qty)

    # ---- 验证证据与适用车型 ----

    def add_evidence(
        self,
        *,
        candidate_id: str,
        kind: str,
        reference: str,
        vehicle_models: tuple[str, ...] = (),
        actor: str,
        role: str,
        now: str | None = None,
    ) -> ServiceResult:
        if err := self._role_error(role, (Role.QUALITY, Role.SUPPLIER)):
            return err
        found, err = self._candidate_or_fail(candidate_id)
        if err:
            return err
        event, candidate = found
        if event.state in (EventState.FULFILLING, EventState.CLOSED):
            return _fail("事件状态", f"当前状态{event.state}，不能新增验证证据")
        problems = []
        if not kind:
            problems.append("证据类型不能为空")
        if not reference:
            problems.append("证据编号不能为空")
        unknown = sorted(set(vehicle_models) - set(event.demand_by_model))
        if unknown:
            problems.append(f"证据车型不属于本事件：{'、'.join(unknown)}")
        if problems:
            return _fail("登记校验", *problems)
        evidence = Evidence(
            evidence_id=self.store.next_id("evidence", "EV"),
            kind=kind,
            reference=reference,
            vehicle_models=tuple(sorted(vehicle_models)),
            provided_by=actor,
            provided_at=self._now(now),
        )
        candidate.evidences.append(evidence)
        self._touch(event, f"候选 {candidate_id} 新增验证证据 {evidence.evidence_id}")
        return _pass("登记验证证据", evidence_id=evidence.evidence_id)

    def invalidate_evidence(self, *, evidence_id: str, actor: str, role: str, now: str | None = None) -> ServiceResult:
        if err := self._role_error(role, (Role.QUALITY,)):
            return err
        found = self.store.find_evidence(evidence_id)
        if found is None:
            return _fail("对象存在性", f"验证证据 {evidence_id} 不存在")
        event, _, evidence = found
        if event.state in (EventState.FULFILLING, EventState.CLOSED):
            return _fail("事件状态", f"当前状态{event.state}，不能变更验证证据")
        evidence.valid = False
        self._touch(event, f"验证证据 {evidence_id} 失效")
        return _pass("证据失效", evidence_id=evidence_id)

    def update_applicability(
        self,
        *,
        candidate_id: str,
        applicable_models: tuple[str, ...],
        actor: str,
        role: str,
        now: str | None = None,
    ) -> ServiceResult:
        if err := self._role_error(role, (Role.PLANNER, Role.QUALITY)):
            return err
        found, err = self._candidate_or_fail(candidate_id)
        if err:
            return err
        event, candidate = found
        if event.state in (EventState.FULFILLING, EventState.CLOSED):
            return _fail("事件状态", f"当前状态{event.state}，不能变更适用车型")
        unknown = sorted(set(applicable_models) - set(event.demand_by_model))
        if unknown:
            return _fail("登记校验", f"适用车型不属于本事件：{'、'.join(unknown)}")
        candidate.applicable_models = tuple(sorted(applicable_models))
        label = "、".join(candidate.applicable_models) if candidate.applicable_models else "全部车型"
        self._touch(event, f"候选 {candidate_id} 适用车型调整为 {label}")
        return _pass("适用车型", candidate_id=candidate_id, applicable_models=list(candidate.applicable_models))

    # ---- 客户限制分别维护 ----

    def set_customer_restriction(
        self,
        *,
        customer: str,
        vehicle_model: str,
        original_part: str,
        banned_substitutes: tuple[str, ...],
        note: str = "",
        actor: str,
        role: str,
        now: str | None = None,
    ) -> ServiceResult:
        if err := self._role_error(role, (Role.PLANNER,)):
            return err
        if not customer or not vehicle_model or not original_part:
            return _fail("登记校验", "客户、车型、原零件均不能为空")
        maintained_at = self._now(now)
        for rule in self.store.restrictions:
            if (rule.customer, rule.vehicle_model, rule.original_part) == (
                customer,
                vehicle_model,
                original_part,
            ):
                rule.banned_substitutes = tuple(sorted(banned_substitutes))
                rule.note = note
                rule.maintained_by = actor
                rule.maintained_at = maintained_at
                break
        else:
            self.store.restrictions.append(
                CustomerRestriction(
                    customer=customer,
                    vehicle_model=vehicle_model,
                    original_part=original_part,
                    banned_substitutes=tuple(sorted(banned_substitutes)),
                    note=note,
                    maintained_by=actor,
                    maintained_at=maintained_at,
                )
            )
        for event in self.store.events.values():
            if event.part_number != original_part:
                continue
            if customer in event.customers and vehicle_model in event.demand_by_model:
                self._touch(event, f"客户 {customer} 对车型 {vehicle_model} 的限制更新")
        return _pass("客户限制", customer=customer, vehicle_model=vehicle_model)

    # ---- 流程：提交、签署、撤回 ----

    def submit(self, *, event_id: str, actor: str, role: str, now: str | None = None) -> ServiceResult:
        if err := self._role_error(role, (Role.PLANNER,)):
            return err
        event, err = self._event_or_fail(event_id)
        if err:
            return err
        if event.state != EventState.DRAFT:
            return _fail("事件状态", f"当前状态{event.state}，仅草拟可提交")
        if not event.candidates:
            return _fail("候选替代存在", "尚未登记候选替代料")
        event.state = EventState.PENDING
        for part in sorted({c.substitute_part for c in event.candidates}):
            self._touch_part_users(
                part,
                f"短缺 {event_id} 提交确认，替代料 {part} 出现竞争",
                exclude=event_id,
            )
        return _pass("提交待确认", event_id=event_id, state=event.state)

    def sign(self, *, event_id: str, signer: str, role: str, now: str | None = None) -> ServiceResult:
        if role not in Role.ALL:
            return _fail("角色权限", f"未知角色 {role}")
        event, err = self._event_or_fail(event_id)
        if err:
            return err
        if event.state != EventState.PENDING:
            return _fail("事件状态", f"当前状态{event.state}，仅待确认可签署")
        if any(s.signer == signer and not s.withdrawn for s in event.signatures):
            return _fail("签署", f"{signer} 已存在有效签署")
        event.signatures.append(Signature(signer=signer, role=role, signed_at=self._now(now)))
        return _pass("签署", event_id=event_id, signer=signer, role=role)

    def withdraw_signature(self, *, event_id: str, signer: str, actor: str, role: str, now: str | None = None) -> ServiceResult:
        event, err = self._event_or_fail(event_id)
        if err:
            return err
        if event.state not in (EventState.PENDING, EventState.RELEASED):
            return _fail("事件状态", f"当前状态{event.state}，不能撤回签署")
        signature = next(
            (s for s in event.signatures if s.signer == signer and not s.withdrawn), None
        )
        if signature is None:
            return _fail("签署", f"{signer} 无有效签署可撤回")
        if role != signature.role:
            return _fail("角色权限", f"撤回签署需原签署角色 {signature.role}")
        signature.withdrawn = True
        signature.withdrawn_at = self._now(now)
        self._touch(event, f"{signer} 撤回签署")
        return _pass("撤回签署", event_id=event_id, signer=signer)

    # ---- 试算与发布 ----

    def evaluate(self, *, event_id: str, now: str | None = None) -> ServiceResult:
        event, err = self._event_or_fail(event_id)
        if err:
            return err
        if event.state != EventState.PENDING:
            return _fail("事件状态", f"当前状态{event.state}，仅待确认可试算")
        report = evaluation.evaluate_event(self.store, event, self.quorum, self._now(now))
        event.last_report = report
        if report["accepted"]:
            event.reevaluation_pending = False
            event.reevaluation_reasons.clear()
        items = [
            ItemResult(i["item"], i["accepted"], i["blocking"], i["reasons"])
            for i in report["items"]
        ]
        return ServiceResult(accepted=report["accepted"], items=items, data={"report": report})

    def publish(
        self,
        *,
        event_id: str,
        candidate_ids: list[str],
        actor: str,
        role: str,
        now: str | None = None,
    ) -> ServiceResult:
        if err := self._role_error(role, (Role.PLANNER,)):
            return err
        event, err = self._event_or_fail(event_id)
        if err:
            return err
        if event.state != EventState.PENDING:
            return _fail("事件状态", f"当前状态{event.state}，仅待确认可发布")
        moment = self._now(now)
        report = evaluation.evaluate_event(self.store, event, self.quorum, moment)
        event.last_report = report
        if report["accepted"]:
            event.reevaluation_pending = False
            event.reevaluation_reasons.clear()
        items: list[ItemResult] = []
        if event.reevaluation_pending:
            items.append(
                ItemResult(
                    "重新评估",
                    False,
                    True,
                    ["存在待重新评估事项：" + "；".join(event.reevaluation_reasons)],
                )
            )
        if not report["accepted"]:
            items.append(ItemResult("试算结论", False, True, ["试算未通过，禁止发布"]))
        selected = list(dict.fromkeys(candidate_ids or []))
        if not selected:
            items.append(ItemResult("发布范围", False, True, ["未指定发布的候选替代"]))
        report_by_id = {c["candidate_id"]: c for c in report["candidates"]}
        for candidate_id in selected:
            candidate_report = report_by_id.get(candidate_id)
            if candidate_report is None:
                items.append(ItemResult(f"候选 {candidate_id}", False, True, ["候选不存在"]))
            elif not candidate_report["accepted"]:
                reasons = [
                    reason
                    for i in candidate_report["items"]
                    if i["blocking"] and not i["accepted"]
                    for reason in i["reasons"]
                ]
                items.append(
                    ItemResult(f"候选 {candidate_id}", False, True, reasons or ["候选未通过试算"])
                )
        if any(not i.accepted for i in items if i.blocking):
            return ServiceResult(accepted=False, items=items, data={"report": report})
        basis = self._freeze(event, selected, report, moment)
        event.state = EventState.RELEASED
        return ServiceResult(
            accepted=True,
            items=[ItemResult("发布", True)],
            data={"basis": self._basis_view(basis), "report": report},
        )

    def _freeze(
        self, event: ShortageEvent, selected: list[str], report: dict, moment: str
    ) -> FrozenBasis:
        report_by_id = {c["candidate_id"]: c for c in report["candidates"]}
        parts = {cid: event.candidate(cid).substitute_part for cid in selected}
        basis = FrozenBasis(
            basis_id=self.store.next_id("basis", "BAS"),
            event_id=event.event_id,
            frozen_at=moment,
            released_candidates=selected,
            candidate_parts=parts,
            demand_by_model=dict(event.demand_by_model),
            inventory_allocation={cid: report_by_id[cid]["inventory_grant"] for cid in selected},
            inventory_levels={p: self.store.inventory.get(p, 0) for p in sorted(set(parts.values()))},
            supplier_qtys={cid: event.candidate(cid).supplier_qty for cid in selected},
            evidence_refs={
                cid: [
                    f"{e.kind}:{e.reference}"
                    for e in event.candidate(cid).evidences
                    if e.valid
                ]
                for cid in selected
            },
            signatures=[
                {"signer": s.signer, "role": s.role, "signed_at": s.signed_at}
                for s in event.signatures
                if not s.withdrawn
            ],
            report=report,
        )
        content = {
            "event_id": basis.event_id,
            "frozen_at": basis.frozen_at,
            "released_candidates": basis.released_candidates,
            "candidate_parts": basis.candidate_parts,
            "demand_by_model": basis.demand_by_model,
            "inventory_allocation": basis.inventory_allocation,
            "inventory_levels": basis.inventory_levels,
            "supplier_qtys": basis.supplier_qtys,
            "evidence_refs": basis.evidence_refs,
            "signatures": basis.signatures,
            "report": basis.report,
        }
        basis.basis_hash = hashlib.sha256(
            json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        event.frozen_basis = basis
        self.store.bases.append(basis)
        return basis

    # ---- 领料放行与履行 ----

    def authorize_issue(self, *, event_id: str, candidate_id: str, qty: int) -> ServiceResult:
        """领料放行检查：未经完整确认并冻结依据的替代禁止进入领料。"""
        event, err = self._event_or_fail(event_id)
        if err:
            return err
        basis = event.frozen_basis
        state_ok = (
            event.state == EventState.RELEASED and basis is not None and not basis.superseded
        )
        items = [
            ItemResult(
                "生效状态",
                state_ok,
                True,
                [] if state_ok else [f"当前状态{event.state}，替代方案未正式生效，禁止进入领料"],
            ),
            ItemResult(
                "重新评估",
                not event.reevaluation_pending,
                True,
                []
                if not event.reevaluation_pending
                else ["存在待重新评估事项：" + "；".join(event.reevaluation_reasons)],
            ),
        ]
        in_basis = state_ok and candidate_id in basis.released_candidates
        items.append(
            ItemResult(
                "冻结依据",
                in_basis,
                True,
                [] if in_basis else [f"候选 {candidate_id} 未包含在冻结依据中"],
            )
        )
        remaining = 0
        qty_ok = isinstance(qty, int) and not isinstance(qty, bool) and qty > 0
        if in_basis and qty_ok:
            remaining = (
                basis.inventory_allocation.get(candidate_id, 0)
                + basis.supplier_qtys.get(candidate_id, 0)
                - event.issued.get(candidate_id, 0)
            )
            qty_ok = qty <= remaining
        items.append(
            ItemResult(
                "领料数量",
                qty_ok,
                True,
                [] if qty_ok else [f"领料数量须为 1..{remaining} 的整数"],
            )
        )
        return ServiceResult(
            accepted=all(i.accepted for i in items),
            items=items,
            data={"remaining_qty": remaining},
        )

    def record_issue(
        self,
        *,
        event_id: str,
        candidate_id: str,
        qty: int,
        actor: str,
        role: str,
        now: str | None = None,
    ) -> ServiceResult:
        if err := self._role_error(role, (Role.WAREHOUSE,)):
            return err
        auth = self.authorize_issue(event_id=event_id, candidate_id=candidate_id, qty=qty)
        if not auth.accepted:
            return auth
        event = self.store.events[event_id]
        part = event.frozen_basis.candidate_parts[candidate_id]
        # 仅冻结的库存分配部分从库存台账扣减，供应确认量由供应商直供
        allocation = event.frozen_basis.inventory_allocation.get(candidate_id, 0)
        issued_so_far = event.issued.get(candidate_id, 0)
        draw = min(issued_so_far + qty, allocation) - min(issued_so_far, allocation)
        if self.store.inventory.get(part, 0) < draw:
            return _fail("库存台账", f"替代料 {part} 库存不足，无法登记领料 {qty} 件")
        if draw:
            self.store.inventory[part] = self.store.inventory.get(part, 0) - draw
        event.issued[candidate_id] = issued_so_far + qty
        event.state = EventState.FULFILLING
        self._touch_part_users(
            part,
            f"替代料 {part} 库存变化为 {self.store.inventory.get(part, 0)}",
            exclude=event_id,
        )
        return _pass(
            "登记领料",
            event_id=event_id,
            candidate_id=candidate_id,
            qty=qty,
            state=event.state,
        )

    def close(self, *, event_id: str, actor: str, role: str, now: str | None = None) -> ServiceResult:
        if err := self._role_error(role, (Role.PLANNER,)):
            return err
        event, err = self._event_or_fail(event_id)
        if err:
            return err
        if event.state != EventState.FULFILLING:
            return _fail("事件状态", f"当前状态{event.state}，仅履行中可关闭")
        event.state = EventState.CLOSED
        return _pass("关闭事件", event_id=event_id, state=event.state)

    # ---- 查询 ----

    def get_event(self, event_id: str) -> ServiceResult:
        event, err = self._event_or_fail(event_id)
        if err:
            return err
        return _pass("查询事件", event=self._event_view(event))

    def list_events(self) -> ServiceResult:
        views = [self._event_view(e) for e in sorted(self.store.events.values(), key=lambda e: e.seq)]
        return _pass("查询事件列表", events=views)

    def get_evaluation(self, event_id: str) -> ServiceResult:
        event, err = self._event_or_fail(event_id)
        if err:
            return err
        if event.last_report is None:
            return _fail("对象存在性", f"短缺事件 {event_id} 尚无试算报告")
        return _pass("查询试算", report=event.last_report)

    def get_basis(self, event_id: str) -> ServiceResult:
        event, err = self._event_or_fail(event_id)
        if err:
            return err
        if event.frozen_basis is None:
            return _fail("对象存在性", f"短缺事件 {event_id} 尚无冻结依据")
        return _pass(
            "查询冻结依据",
            basis=self._basis_view(event.frozen_basis),
            report=event.frozen_basis.report,
        )

    def _event_view(self, event: ShortageEvent) -> dict:
        return {
            "event_id": event.event_id,
            "part_number": event.part_number,
            "state": event.state,
            "needed_by": event.needed_by,
            "customers": list(event.customers),
            "demand_by_model": dict(event.demand_by_model),
            "required_qty": event.required_qty,
            "reevaluation_pending": event.reevaluation_pending,
            "reevaluation_reasons": list(event.reevaluation_reasons),
            "candidates": [self._candidate_view(c) for c in event.candidates],
            "signatures": [
                {
                    "signer": s.signer,
                    "role": s.role,
                    "signed_at": s.signed_at,
                    "withdrawn": s.withdrawn,
                }
                for s in event.signatures
            ],
            "issued": dict(event.issued),
            "frozen_basis": self._basis_view(event.frozen_basis) if event.frozen_basis else None,
        }

    @staticmethod
    def _candidate_view(candidate: SubstituteCandidate) -> dict:
        return {
            "candidate_id": candidate.candidate_id,
            "substitute_part": candidate.substitute_part,
            "ratio": candidate.ratio,
            "applicable_models": list(candidate.applicable_models),
            "fit_status": candidate.fit_status,
            "supplier_qty": candidate.supplier_qty,
            "evidences": [
                {
                    "evidence_id": e.evidence_id,
                    "kind": e.kind,
                    "reference": e.reference,
                    "vehicle_models": list(e.vehicle_models),
                    "provided_by": e.provided_by,
                    "provided_at": e.provided_at,
                    "valid": e.valid,
                }
                for e in candidate.evidences
            ],
        }

    @staticmethod
    def _basis_view(basis: FrozenBasis) -> dict:
        return {
            "basis_id": basis.basis_id,
            "event_id": basis.event_id,
            "frozen_at": basis.frozen_at,
            "released_candidates": list(basis.released_candidates),
            "candidate_parts": dict(basis.candidate_parts),
            "demand_by_model": dict(basis.demand_by_model),
            "inventory_allocation": dict(basis.inventory_allocation),
            "inventory_levels": dict(basis.inventory_levels),
            "supplier_qtys": dict(basis.supplier_qtys),
            "evidence_refs": {k: list(v) for k, v in basis.evidence_refs.items()},
            "signatures": [dict(s) for s in basis.signatures],
            "basis_hash": basis.basis_hash,
            "superseded": basis.superseded,
        }
