"""应用服务：短缺替代料审批的全部用例。

所有写方法都返回 :class:`Outcome`，其中 ``items`` 逐项给出
"接受(OK)/拒绝(原因码 + 明细)"，便于调用方逐条展示。

重评触发点（覆盖题目要求的四类变化）：

- 撤回签署 → 法定人数闸口立即重算，已下达方案被撤回至待确认；
- 局部适用矩阵/证据变更 → 依据版本前进，旧签署失效，全员重评；
- 库存变化（现有量/在途）→ 库存池版本前进，试算过期，全员重评；
- 新短缺候选进入待确认（或他案发布/生效）→ 竞争指纹变化，全员重评。

已下达方案重评失败时撤回至"待确认"并清除同短缺发布锁；
履行中方案不回退状态（冻结依据不可变），但追加履行风险告警，
未处置前不能结案。
"""
from __future__ import annotations

import hashlib
import json as _json
from dataclasses import dataclass, field
from typing import Any, Optional

from . import reason_codes as rc
from . import trial
from .models import (
    PLANNER,
    SIGNER_ROLES,
    Applicability,
    Candidate,
    DemandLine,
    FreezeBasis,
    Shortage,
    Signature,
    State,
    now_iso,
)
from .store import QuorumRule, Store


# ---- 返回结构 ----

@dataclass
class Item:
    target: str
    accepted: bool
    code: str
    message: str
    detail: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "accepted": self.accepted,
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass
class Outcome:
    items: list[Item] = field(default_factory=list)
    data: Any = None

    @property
    def accepted(self) -> bool:
        return bool(self.items) and all(i.accepted for i in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {"accepted": self.accepted, "items": [i.to_dict() for i in self.items], "data": self.data}

    @classmethod
    def ok(cls, target: str, data: Any = None, detail: Any = None) -> "Outcome":
        return cls(items=[Item(target, True, rc.OK, rc.MESSAGES[rc.OK], detail)], data=data)

    @classmethod
    def reject(cls, target: str, code: str, detail: Any = None) -> "Outcome":
        return cls(items=[Item(target, False, code, rc.MESSAGES[code], detail)])


class Service:
    def __init__(self, store: Optional[Store] = None, snapshot_path: Optional[str] = None) -> None:
        self.store = store or Store(snapshot_path=snapshot_path)

    # ================= 内部基础工具 =================

    def _event(self, cand: Candidate, kind: str, detail: Any = None) -> None:
        cand.events.append({"at": now_iso(), "kind": kind, "detail": detail})

    def _save(self) -> None:
        self.store.save()

    def _get_candidate(self, candidate_id: str) -> Optional[Candidate]:
        return self.store.candidates.get(candidate_id)

    def _refresh_trial(self, cand: Candidate) -> None:
        """用当前仓储状态重算某候选的需求/分配/风险，并写入试算基准。

        基准（库存版本/证据版本/竞争指纹）较上次发生变化时，试算确认
        标志失效，计划员必须显式重新试算后才能发布（TRIAL_STALE）。
        """
        basis = trial.trial_basis(self.store, cand)
        drifted = (
            cand.trial_stock_version != basis["stock_version"]
            or cand.trial_evidence_version != basis["evidence_version"]
            or cand.trial_comp_version != basis["competition_fingerprint"]
        )
        cand.requirement = trial.compute_requirement(self.store, cand)
        cand.allocation = trial.compute_allocation(self.store, cand)
        cand.risk = trial.evaluate_risks(self.store, cand)
        cand.trial_stock_version = basis["stock_version"]
        cand.trial_evidence_version = basis["evidence_version"]
        cand.trial_comp_version = basis["competition_fingerprint"]
        cand.last_trial_at = now_iso()
        if drifted:
            cand.trial_confirmed = False

    def _gates_block(self, cand: Candidate) -> list[str]:
        return trial.blocking_codes(cand.risk)

    def _reevaluate_candidate(self, cand: Candidate) -> bool:
        """重评单个已提交候选。返回其发布资格是否被撤回。"""
        if cand.state not in (State.PENDING, State.RELEASED, State.FULFILLING):
            return False
        self._refresh_trial(cand)
        blocking = self._gates_block(cand)

        if cand.state == State.FULFILLING:
            # 冻结依据不可变；只刷新履行风险告警
            open_codes = {w["code"] for w in cand.warnings if not w.get("resolved")}
            fresh_codes = set(blocking)
            for code in fresh_codes - open_codes:
                risk = next(r for r in cand.risk if r["code"] == code)
                cand.warnings.append({
                    "code": code, "detail": risk["detail"], "detected_at": now_iso(),
                    "resolved": False, "resolved_by": None, "resolve_note": None,
                })
                self._event(cand, "fulfillment_risk", {"code": code})
            for w in cand.warnings:
                if not w.get("resolved") and w["code"] not in fresh_codes:
                    w["resolved"] = True
                    w["resolved_by"] = "system"
                    w["resolve_note"] = "重评通过，风险自动消除"
                    w["resolved_at"] = now_iso()
            return False

        if cand.state == State.RELEASED and blocking:
            cand.state = State.PENDING
            cand.trial_confirmed = False
            shortage = self.store.shortages[cand.shortage_id]
            if shortage.released_candidate_id == cand.candidate_id:
                shortage.released_candidate_id = None
            self._event(cand, "revoked_to_pending", {"reasons": blocking})
            return True
        return False

    def _reevaluate_material(self, material: str) -> None:
        """对消费某替代料的全部候选重评；撤回可能级联，迭代至稳定。"""
        affected = [
            c for c in self.store.candidates.values()
            if c.substitute_material == material
            and c.state in (State.PENDING, State.RELEASED, State.FULFILLING)
        ]
        while True:
            revoked = False
            for cand in sorted(affected, key=lambda c: c.candidate_id):
                if self._reevaluate_candidate(cand):
                    revoked = True
            if not revoked:
                break

    # ================= 1. 短缺事件登记 =================

    def register_shortage(
        self,
        shortage_id: str,
        material: str,
        plant: str,
        raised_by: str,
        demand: list[dict[str, Any]],
    ) -> Outcome:
        with self.store.lock:
            target = f"shortage:{shortage_id}"
            if not shortage_id or not material or not plant:
                return Outcome.reject(target, rc.E_VALIDATION, "shortage_id/material/plant 不能为空")
            if shortage_id in self.store.shortages:
                return Outcome.reject(target, rc.E_DUPLICATE)
            if not isinstance(demand, list) or not demand:
                return Outcome.reject(target, rc.E_VALIDATION, "demand 至少一行")
            lines: list[DemandLine] = []
            seen: set[str] = set()
            for i, row in enumerate(demand):
                model = row.get("vehicle_model")
                qty = row.get("shortage_qty")
                if not model or not isinstance(model, str):
                    return Outcome.reject(target, rc.E_VALIDATION, f"demand[{i}].vehicle_model 非法")
                if not isinstance(qty, (int, float)) or qty <= 0:
                    return Outcome.reject(target, rc.E_VALIDATION, f"demand[{i}].shortage_qty 必须为正数")
                if model in seen:
                    return Outcome.reject(target, rc.E_VALIDATION, f"车型 {model} 重复")
                seen.add(model)
                lines.append(DemandLine(vehicle_model=model, shortage_qty=float(qty)))
            shortage = Shortage(
                shortage_id=shortage_id, material=material, plant=plant,
                raised_by=raised_by or PLANNER, demand=lines,
            )
            self.store.shortages[shortage_id] = shortage
            self._save()
            return Outcome.ok(target, shortage.to_dict())

    # ================= 2. 候选替代 + 适用车型矩阵 =================

    def register_candidate(
        self,
        candidate_id: str,
        shortage_id: str,
        substitute_material: str,
        applicability: list[dict[str, Any]],
        default_ratio: float = 1.0,
        quorum_rule_id: str = "standard",
    ) -> Outcome:
        with self.store.lock:
            target = f"candidate:{candidate_id}"
            if not candidate_id or not substitute_material:
                return Outcome.reject(target, rc.E_VALIDATION, "candidate_id/substitute_material 不能为空")
            if candidate_id in self.store.candidates:
                return Outcome.reject(target, rc.E_DUPLICATE)
            shortage = self.store.shortages.get(shortage_id)
            if shortage is None:
                return Outcome.reject(target, rc.E_NOT_FOUND, {"shortage_id": shortage_id})
            if quorum_rule_id not in self.store.quorum_rules:
                return Outcome.reject(target, rc.E_QUORUM_RULE_UNKNOWN, {"rule_id": quorum_rule_id})

            rows, bad = self._parse_applicability(applicability, default_ratio)
            if bad:
                return Outcome.reject(target, rc.E_VALIDATION, bad)

            cand = Candidate(
                candidate_id=candidate_id,
                shortage_id=shortage_id,
                substitute_material=substitute_material,
                default_ratio=default_ratio,
                applicability=rows,
                quorum_rule_id=quorum_rule_id,
                seq=len(self.store.candidates),
            )
            self.store.candidates[candidate_id] = cand
            shortage.candidate_ids.append(candidate_id)
            # 登记即做一次试算，让计划员看到初始需求与风险
            self._refresh_trial(cand)
            self._event(cand, "created")
            self._save()
            return Outcome.ok(target, cand.to_dict())

    @staticmethod
    def _parse_applicability(
        raw: list[dict[str, Any]], default_ratio: float
    ) -> tuple[list[Applicability], Optional[str]]:
        if not isinstance(raw, list) or not raw:
            return [], "applicability 至少一行（局部适用请显式给出各车型）"
        rows: list[Applicability] = []
        seen: set[str] = set()
        for i, row in enumerate(raw):
            model = row.get("vehicle_model")
            if not model or not isinstance(model, str):
                return [], f"applicability[{i}].vehicle_model 非法"
            if model in seen:
                return [], f"车型 {model} 在适用矩阵中重复"
            seen.add(model)
            ratio = float(row.get("conversion_ratio", default_ratio))
            if ratio <= 0:
                return [], f"applicability[{i}].conversion_ratio 必须为正数"
            rows.append(Applicability(
                vehicle_model=model,
                conversion_ratio=ratio,
                usable=bool(row.get("usable", True)),
                note=str(row.get("note", "")),
            ))
        return rows, None

    def update_applicability(self, candidate_id: str, applicability: list[dict[str, Any]]) -> Outcome:
        """局部适用矩阵变更：依据版本前进，签署失效，物料级重评。"""
        with self.store.lock:
            target = f"candidate:{candidate_id}.applicability"
            cand = self._get_candidate(candidate_id)
            if cand is None:
                return Outcome.reject(target, rc.E_NOT_FOUND)
            if cand.state in (State.CLOSED,):
                return Outcome.reject(target, rc.E_INVALID_STATE, {"state": cand.state.value})
            rows, bad = self._parse_applicability(applicability, cand.default_ratio)
            if bad:
                return Outcome.reject(target, rc.E_VALIDATION, bad)
            cand.applicability = rows
            cand.evidence_version += 1
            self._event(cand, "applicability_changed", {"version": cand.evidence_version})
            self._reevaluate_material(cand.substitute_material)
            self._save()
            return Outcome.ok(target, cand.to_dict())

    # ================= 3. 提交 / 发布 / 生效 / 结案 =================

    def submit(self, candidate_id: str) -> Outcome:
        """草拟 → 待确认。提交即试算并暴露全部风险。"""
        with self.store.lock:
            target = f"candidate:{candidate_id}.submit"
            cand = self._get_candidate(candidate_id)
            if cand is None:
                return Outcome.reject(target, rc.E_NOT_FOUND)
            if cand.state != State.DRAFT:
                return Outcome.reject(target, rc.E_INVALID_STATE, {"state": cand.state.value})
            shortage = self.store.shortages[cand.shortage_id]
            if shortage.fulfilled_candidate_id:
                return Outcome.reject(target, rc.E_ALREADY_FULFILLED,
                                      {"fulfilled_by": shortage.fulfilled_candidate_id})
            cand.state = State.PENDING
            self._event(cand, "submitted")
            # 新消费者进入竞争，同物料全员重评
            self._reevaluate_material(cand.substitute_material)
            self._save()
            return Outcome.ok(target, cand.to_dict())

    def release(self, candidate_id: str) -> Outcome:
        """待确认 → 已下达：发布前做权威试算，任一阻断性风险即拒绝（逐项原因）。"""
        with self.store.lock:
            target = f"candidate:{candidate_id}.release"
            cand = self._get_candidate(candidate_id)
            if cand is None:
                return Outcome.reject(target, rc.E_NOT_FOUND)
            if cand.state == State.DRAFT:
                return Outcome.reject(target, rc.E_NOT_SUBMITTED)
            if cand.state in (State.RELEASED, State.FULFILLING, State.CLOSED):
                return Outcome.reject(target, rc.E_INVALID_STATE, {"state": cand.state.value})

            shortage = self.store.shortages[cand.shortage_id]
            if shortage.fulfilled_candidate_id:
                return Outcome.reject(target, rc.E_ALREADY_FULFILLED,
                                      {"fulfilled_by": shortage.fulfilled_candidate_id})
            # 同一短缺互斥：至多一个替代方案可发布
            other = shortage.released_candidate_id
            if other and other != candidate_id:
                return Outcome.reject(target, rc.E_ALREADY_RELEASED, {"released_candidate_id": other})

            # 权威重算：先让同物料全部竞争者重评（可能有人刚被撤回/生效），
            # 再以最新竞争格局刷新自身；依据若有变化则试算确认失效。
            self._reevaluate_material(cand.substitute_material)
            self._refresh_trial(cand)
            if not cand.trial_confirmed:
                return Outcome.reject(target, rc.E_TRIAL_STALE, {
                    "required_basis": trial.trial_basis(self.store, cand),
                    "hint": "依据可能已变化，计划员须显式重新试算后再发布",
                })

            blocking = self._gates_block(cand)
            if blocking:
                items = [Item(target, False, code, rc.MESSAGES[code],
                              next((r["detail"] for r in cand.risk if r["code"] == code), None))
                         for code in blocking]
                outcome = Outcome(items=items, data=cand.to_dict())
                self._save()
                return outcome

            cand.state = State.RELEASED
            shortage.released_candidate_id = candidate_id
            self._event(cand, "released")
            # 优先级变化，竞争各方重新分配
            self._reevaluate_material(cand.substitute_material)
            self._save()
            return Outcome.ok(target, cand.to_dict())

    def effectuate(self, candidate_id: str) -> Outcome:
        """已下达 → 履行中：正式生效，冻结全部依据（不可变快照 + 指纹）。"""
        with self.store.lock:
            target = f"candidate:{candidate_id}.effectuate"
            cand = self._get_candidate(candidate_id)
            if cand is None:
                return Outcome.reject(target, rc.E_NOT_FOUND)
            if cand.state != State.RELEASED:
                return Outcome.reject(target, rc.E_FREEZE_REQUIRED if cand.state == State.PENDING
                                      else rc.E_INVALID_STATE, {"state": cand.state.value})
            # 生效前再做一次权威试算，冻结的必须是"当前仍成立"的依据
            self._refresh_trial(cand)
            blocking = self._gates_block(cand)
            if blocking:
                items = [Item(target, False, code, rc.MESSAGES[code],
                              next((r["detail"] for r in cand.risk if r["code"] == code), None))
                         for code in blocking]
                return Outcome(items=items, data=cand.to_dict())

            basis_dict = trial.trial_basis(self.store, cand)
            _, quorum_detail = trial.evaluate_quorum(self.store, cand)
            payload = {
                "requirement": cand.requirement,
                "allocation": cand.allocation,
                "evidence": cand.evidence.to_dict(),
                "applicability": [a.to_dict() for a in cand.applicability],
                "quorum": quorum_detail,
                "signatures": [s.to_dict() for s in cand.signatures],
                "stock_version": basis_dict["stock_version"],
            }
            basis_hash = hashlib.sha256(
                _json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()

            triggered = [
                cid for cid, d in cand.allocation.items()
                if cid != "__pool__" and not d.get("self") and d["allocated"] > 0
            ]
            cand.freeze = FreezeBasis(
                frozen_at=now_iso(),
                requirement_snapshot=dict(cand.requirement),
                allocation_snapshot=dict(cand.allocation),
                evidence=cand.evidence.to_dict(),
                applicability=[a.to_dict() for a in cand.applicability],
                quorum_snapshot=quorum_detail,
                signatures=[s.to_dict() for s in cand.signatures],
                stock_version=basis_dict["stock_version"],
                basis_hash=basis_hash,
                triggered_by_shortages=triggered,
            )
            cand.state = State.FULFILLING
            self._event(cand, "effectuated", {"basis_hash": basis_hash})
            self._reevaluate_material(cand.substitute_material)
            self._save()
            return Outcome.ok(target, cand.to_dict(), {"basis_hash": basis_hash})

    def complete(self, candidate_id: str, by: str = "") -> Outcome:
        """履行中 → 已关闭（允许领料结案）；存在未处置履行风险时拒绝。"""
        with self.store.lock:
            target = f"candidate:{candidate_id}.complete"
            cand = self._get_candidate(candidate_id)
            if cand is None:
                return Outcome.reject(target, rc.E_NOT_FOUND)
            if cand.state != State.FULFILLING:
                return Outcome.reject(target, rc.E_INVALID_STATE, {"state": cand.state.value})
            if cand.freeze is None:
                return Outcome.reject(target, rc.E_FREEZE_REQUIRED)
            open_warnings = [w for w in cand.warnings if not w.get("resolved")]
            if open_warnings:
                return Outcome.reject(target, rc.E_RISK_OPEN, {"open": open_warnings})
            cand.state = State.CLOSED
            shortage = self.store.shortages[cand.shortage_id]
            shortage.fulfilled_candidate_id = candidate_id
            self._event(cand, "closed", {"by": by})
            # 领料结案：按冻结的分配量真正扣减可用池（先现有后在途），
            # 版本前进并触发重评，避免结案后等待者看到已被领走的"幽灵库存"。
            material = cand.substitute_material
            pool = self.store.pool(material)
            taken = float(cand.freeze.allocation_snapshot.get(candidate_id, {}).get("allocated", 0.0))
            remaining = taken
            if remaining > 0:
                from_hand = min(pool.on_hand, remaining)
                pool.on_hand = round(pool.on_hand - from_hand, 6)
                remaining = round(remaining - from_hand, 6)
                consumed_inbound: dict[str, float] = {}
                for inbound_id in sorted(pool.inbound):
                    if remaining <= 0:
                        break
                    give = min(pool.inbound[inbound_id], remaining)
                    pool.inbound[inbound_id] = round(pool.inbound[inbound_id] - give, 6)
                    consumed_inbound[inbound_id] = give
                    remaining = round(remaining - give, 6)
                    if pool.inbound[inbound_id] == 0:
                        del pool.inbound[inbound_id]
                pool.version += 1
                self._event(cand, "stock_consumed", {
                    "material": material, "qty": taken,
                    "from_on_hand": from_hand, "from_inbound": consumed_inbound,
                    "version": pool.version,
                })
            self._reevaluate_material(material)
            self._save()
            return Outcome.ok(target, cand.to_dict(), {"consumed": {material: taken}})

    def abandon_candidate(self, candidate_id: str, reason: str = "") -> Outcome:
        """撤销候选替代方案：退回草拟并退出库存竞争。

        待确认/已下达（如撤回签署后）均可撤销；已下达撤销时释放同短缺
        发布锁。履行中（已领料领料）不允许撤销，只能完成或处置风险。
        """
        with self.store.lock:
            target = f"candidate:{candidate_id}.abandon"
            cand = self._get_candidate(candidate_id)
            if cand is None:
                return Outcome.reject(target, rc.E_NOT_FOUND)
            if cand.state in (State.FULFILLING, State.CLOSED):
                return Outcome.reject(target, rc.E_INVALID_STATE, {"state": cand.state.value})
            if cand.state == State.DRAFT:
                return Outcome.reject(target, rc.E_INVALID_STATE, {"state": cand.state.value})
            was_released = cand.state == State.RELEASED
            cand.state = State.DRAFT
            cand.trial_confirmed = False
            shortage = self.store.shortages[cand.shortage_id]
            if was_released and shortage.released_candidate_id == candidate_id:
                shortage.released_candidate_id = None
            self._event(cand, "abandoned", {"reason": reason})
            # 退出消费集合同物料全员重评（等待者可能获得库存）
            self._reevaluate_material(cand.substitute_material)
            self._save()
            return Outcome.ok(target, cand.to_dict())

    # ================= 4. 验证证据（三类分别维护） =================

    _EVIDENCE_KINDS = ("engineering", "supply", "customer")

    def add_evidence_doc(self, candidate_id: str, kind: str, doc_id: str, note: str = "") -> Outcome:
        with self.store.lock:
            target = f"candidate:{candidate_id}.evidence.{kind}"
            cand = self._get_candidate(candidate_id)
            if cand is None:
                return Outcome.reject(target, rc.E_NOT_FOUND)
            if kind not in self._EVIDENCE_KINDS:
                return Outcome.reject(target, rc.E_VALIDATION,
                                      {"kind": kind, "allowed": self._EVIDENCE_KINDS})
            if not doc_id:
                return Outcome.reject(target, rc.E_VALIDATION, "doc_id 不能为空")
            bucket = getattr(cand.evidence, kind)
            if doc_id in bucket:
                return Outcome.reject(target, rc.E_DUPLICATE, {"doc_id": doc_id})
            bucket[doc_id] = note
            cand.evidence_version += 1
            self._event(cand, "evidence_added", {"kind": kind, "doc_id": doc_id,
                                                  "version": cand.evidence_version})
            self._reevaluate_material(cand.substitute_material)
            self._save()
            return Outcome.ok(target, cand.to_dict())

    def set_evidence_confirmation(self, candidate_id: str, kind: str, value: bool) -> Outcome:
        """分别确认三类证据：engineering/supply 为"已确认"，customer 为"客户放行"。"""
        with self.store.lock:
            target = f"candidate:{candidate_id}.evidence.{kind}.confirm"
            cand = self._get_candidate(candidate_id)
            if cand is None:
                return Outcome.reject(target, rc.E_NOT_FOUND)
            if kind not in self._EVIDENCE_KINDS:
                return Outcome.reject(target, rc.E_VALIDATION,
                                      {"kind": kind, "allowed": self._EVIDENCE_KINDS})
            flag = {
                "engineering": "engineering_confirmed",
                "supply": "supply_confirmed",
                "customer": "customer_approved",
            }[kind]
            if value and not getattr(cand.evidence, kind):
                return Outcome.reject(target, rc.E_VALIDATION,
                                      f"{kind} 桶内尚无凭据，不能确认")
            if getattr(cand.evidence, flag) == value:
                return Outcome.reject(target, rc.E_VALIDATION, f"{kind} 确认状态已是 {value}")
            setattr(cand.evidence, flag, value)
            cand.evidence_version += 1
            self._event(cand, "evidence_confirmed",
                        {"kind": kind, "value": value, "version": cand.evidence_version})
            self._reevaluate_material(cand.substitute_material)
            self._save()
            return Outcome.ok(target, cand.to_dict())

    # ================= 5. 法定人数规则 + 签署 / 撤回 =================

    def add_quorum_rule(self, rule_id: str, required_counts: dict[str, int], min_roles: int) -> Outcome:
        with self.store.lock:
            target = f"quorum_rule:{rule_id}"
            if not rule_id:
                return Outcome.reject(target, rc.E_VALIDATION, "rule_id 不能为空")
            if rule_id in self.store.quorum_rules:
                return Outcome.reject(target, rc.E_DUPLICATE)
            if not isinstance(required_counts, dict) or not required_counts:
                return Outcome.reject(target, rc.E_VALIDATION, "required_counts 不能为空")
            for role, count in required_counts.items():
                if role not in SIGNER_ROLES:
                    return Outcome.reject(target, rc.E_UNKNOWN_ROLE,
                                          {"role": role, "allowed": SIGNER_ROLES})
                if not isinstance(count, int) or count <= 0:
                    return Outcome.reject(target, rc.E_VALIDATION, f"{role} 人数必须为正整数")
            if not isinstance(min_roles, int) or min_roles <= 0 or min_roles > len(required_counts):
                return Outcome.reject(target, rc.E_VALIDATION, "min_roles 超出角色集合范围")
            self.store.quorum_rules[rule_id] = QuorumRule(rule_id, dict(required_counts), min_roles)
            self._save()
            return Outcome.ok(target, self.store.quorum_rules[rule_id].to_dict())

    def sign(self, candidate_id: str, role: str, signer: str) -> Outcome:
        with self.store.lock:
            target = f"candidate:{candidate_id}.sign"
            cand = self._get_candidate(candidate_id)
            if cand is None:
                return Outcome.reject(target, rc.E_NOT_FOUND)
            if role not in SIGNER_ROLES:
                return Outcome.reject(target, rc.E_UNKNOWN_ROLE,
                                      {"role": role, "allowed": SIGNER_ROLES})
            if not signer:
                return Outcome.reject(target, rc.E_VALIDATION, "signer 不能为空")
            if cand.state not in (State.PENDING, State.RELEASED, State.FULFILLING):
                return Outcome.reject(target, rc.E_INVALID_STATE, {"state": cand.state.value})
            if any(s.role == role and s.signer == signer and s.basis_version == cand.evidence_version
                   for s in cand.signatures):
                return Outcome.reject(target, rc.E_ALREADY_SIGNED, {"role": role, "signer": signer})
            # 同一签署人重新签署即取代其历史签署；残留的旧版本签署代表
            # "尚未对新依据重签"的人，继续计入 SIGNATURE_STALE 闸口。
            superseded = [s for s in cand.signatures if s.role == role and s.signer == signer]
            cand.signatures = [s for s in cand.signatures
                               if not (s.role == role and s.signer == signer)]
            cand.signatures.append(Signature(
                role=role, signer=signer, at=now_iso(),
                basis_version=cand.evidence_version,
            ))
            self._event(cand, "signed", {"role": role, "signer": signer,
                                         "superseded": len(superseded)})
            self._refresh_trial(cand)
            self._save()
            return Outcome.ok(target, cand.to_dict())

    def withdraw_signature(self, candidate_id: str, role: str, signer: str) -> Outcome:
        """撤回签署：立即重评；已下达方案若法定人数不再满足，撤回至待确认。"""
        with self.store.lock:
            target = f"candidate:{candidate_id}.withdraw"
            cand = self._get_candidate(candidate_id)
            if cand is None:
                return Outcome.reject(target, rc.E_NOT_FOUND)
            if role not in SIGNER_ROLES:
                return Outcome.reject(target, rc.E_UNKNOWN_ROLE, {"role": role})
            if cand.state not in (State.PENDING, State.RELEASED, State.FULFILLING):
                return Outcome.reject(target, rc.E_INVALID_STATE, {"state": cand.state.value})
            before = len(cand.signatures)
            cand.signatures = [
                s for s in cand.signatures
                if not (s.role == role and s.signer == signer and s.basis_version == cand.evidence_version)
            ]
            if len(cand.signatures) == before:
                return Outcome.reject(target, rc.E_NOT_FOUND,
                                      {"role": role, "signer": signer, "reason": "未找到有效签署"})
            self._event(cand, "signature_withdrawn", {"role": role, "signer": signer})
            # 撤回可能使本方案被撤回并释放库存，故同物料全员重评
            self._reevaluate_material(cand.substitute_material)
            self._save()
            return Outcome.ok(target, cand.to_dict())

    def resolve_warning(self, candidate_id: str, code: str, by: str, note: str) -> Outcome:
        """处置履行中风险告警（记录处置人与说明），处置后方可结案。"""
        with self.store.lock:
            target = f"candidate:{candidate_id}.warning.{code}"
            cand = self._get_candidate(candidate_id)
            if cand is None:
                return Outcome.reject(target, rc.E_NOT_FOUND)
            matched = [w for w in cand.warnings if w["code"] == code and not w.get("resolved")]
            if not matched:
                return Outcome.reject(target, rc.E_NOT_FOUND, {"code": code})
            for w in matched:
                w["resolved"] = True
                w["resolved_by"] = by
                w["resolve_note"] = note
                w["resolved_at"] = now_iso()
            self._event(cand, "warning_resolved", {"code": code, "by": by})
            self._save()
            return Outcome.ok(target, cand.to_dict())

    # ================= 6. 库存 / 在途供应（库存变化触发重评） =================

    def set_on_hand(self, material: str, qty: float) -> Outcome:
        with self.store.lock:
            target = f"stock:{material}.on_hand"
            if not isinstance(qty, (int, float)) or qty < 0:
                return Outcome.reject(target, rc.E_VALIDATION, "现有库存不能为负")
            pool = self.store.pool(material)
            old = pool.on_hand
            if old == qty:
                return Outcome.reject(target, rc.E_VALIDATION, "库存未变化")
            pool.on_hand = float(qty)
            pool.version += 1
            self._reevaluate_material(material)
            self._save()
            return Outcome.ok(target, {"material": material, "old": old, "new": qty,
                                        "version": pool.version})

    def add_inbound(self, material: str, inbound_id: str, qty: float) -> Outcome:
        with self.store.lock:
            target = f"stock:{material}.inbound:{inbound_id}"
            if not inbound_id:
                return Outcome.reject(target, rc.E_VALIDATION, "inbound_id 不能为空")
            if not isinstance(qty, (int, float)) or qty <= 0:
                return Outcome.reject(target, rc.E_VALIDATION, "在途数量必须为正数")
            pool = self.store.pool(material)
            if inbound_id in pool.inbound:
                return Outcome.reject(target, rc.E_DUPLICATE, {"inbound_id": inbound_id})
            pool.inbound[inbound_id] = float(qty)
            pool.version += 1
            self._reevaluate_material(material)
            self._save()
            return Outcome.ok(target, pool.to_dict())

    def remove_inbound(self, material: str, inbound_id: str) -> Outcome:
        """在途供应取消/延迟：供应池收缩并触发竞争重评。"""
        with self.store.lock:
            target = f"stock:{material}.inbound:{inbound_id}"
            pool = self.store.pool(material)
            if inbound_id not in pool.inbound:
                return Outcome.reject(target, rc.E_NOT_FOUND, {"inbound_id": inbound_id})
            qty = pool.inbound.pop(inbound_id)
            pool.version += 1
            self._reevaluate_material(material)
            self._save()
            return Outcome.ok(target, {"material": material, "removed": inbound_id,
                                        "qty": qty, "version": pool.version})

    # ================= 7. 试算与查询 =================

    def run_trial(self, candidate_id: str) -> Outcome:
        with self.store.lock:
            target = f"candidate:{candidate_id}.trial"
            cand = self._get_candidate(candidate_id)
            if cand is None:
                return Outcome.reject(target, rc.E_NOT_FOUND)
            self._refresh_trial(cand)
            # 显式试算 = 计划员基于当前依据看过结果，允许据此发布
            cand.trial_confirmed = True
            self._save()
            return Outcome.ok(target, {
                "candidate_id": candidate_id,
                "state": cand.state.value,
                "requirement": cand.requirement,
                "allocation": cand.allocation,
                "risks": cand.risk,
                "gates_passed": not self._gates_block(cand),
                "basis": trial.trial_basis(self.store, cand),
            })

    def get_candidate(self, candidate_id: str) -> Outcome:
        with self.store.lock:
            cand = self.store.candidates.get(candidate_id)
            if cand is None:
                return Outcome.reject(f"candidate:{candidate_id}", rc.E_NOT_FOUND)
            return Outcome.ok(f"candidate:{candidate_id}", cand.to_dict())

    def get_shortage(self, shortage_id: str) -> Outcome:
        with self.store.lock:
            shortage = self.store.shortages.get(shortage_id)
            if shortage is None:
                return Outcome.reject(f"shortage:{shortage_id}", rc.E_NOT_FOUND)
            return Outcome.ok(f"shortage:{shortage_id}", shortage.to_dict())

    def list_candidates(self, state: Optional[str] = None) -> Outcome:
        with self.store.lock:
            items = [c.to_dict() for c in self.store.candidates.values()
                     if state is None or c.state.value == state]
            return Outcome.ok("candidates", sorted(items, key=lambda x: x["candidate_id"]))
