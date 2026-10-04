"""短缺替代料审批：领域服务的完整回归测试。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from substitution import Service, Store
from substitution.models import (
    PLANNER,
    QUALITY,
    SUPPLIER,
    WAREHOUSE,
    State,
)
from substitution import reason_codes as rc


def codes(outcome) -> list[str]:
    return [i.code for i in outcome.items]


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = Service()

    def register_ready_candidate(
        self,
        cid: str = "C1",
        sid: str = "S1",
        sub: str = "SUB",
        models: list[str] | None = None,
        stock: float = 1000.0,
        shortage_qty: float = 10.0,
        ratio: float = 1.0,
    ) -> None:
        models = models or ["V1"]
        if sid not in self.svc.store.shortages:
            self.svc.register_shortage(
                sid, "OLD-" + sid, "PLANT", PLANNER,
                [{"vehicle_model": m, "shortage_qty": shortage_qty} for m in models],
            )
        self.svc.set_on_hand(sub, stock)
        self.svc.register_candidate(
            cid, sid, sub,
            [{"vehicle_model": m, "conversion_ratio": ratio} for m in models],
        )
        for kind in ("engineering", "supply", "customer"):
            self.svc.add_evidence_doc(cid, kind, f"{cid}-{kind}-doc")
            self.svc.set_evidence_confirmation(cid, kind, True)
        self.svc.submit(cid)
        self.svc.sign(cid, QUALITY, f"qe-{cid}")
        self.svc.sign(cid, SUPPLIER, f"sup-{cid}")
        self.svc.sign(cid, WAREHOUSE, f"wh-{cid}")
        self.svc.run_trial(cid)

    def state(self, cid: str) -> str:
        return self.svc.get_candidate(cid).data["state"]


class RegistrationTest(ServiceTestBase):
    def test_shortage_validation(self) -> None:
        self.assertEqual(codes(self.svc.register_shortage("", "M", "P", PLANNER, [])),
                         [rc.E_VALIDATION])
        self.svc.register_shortage("S1", "M", "P", PLANNER,
                                   [{"vehicle_model": "V1", "shortage_qty": 5}])
        self.assertEqual(codes(self.svc.register_shortage(
            "S1", "M", "P", PLANNER, [{"vehicle_model": "V1", "shortage_qty": 5}])),
            [rc.E_DUPLICATE])
        self.assertEqual(codes(self.svc.register_shortage(
            "S2", "M", "P", PLANNER, [{"vehicle_model": "V1", "shortage_qty": -1}])),
            [rc.E_VALIDATION])
        self.assertEqual(codes(self.svc.register_shortage(
            "S3", "M", "P", PLANNER, [{"vehicle_model": "V1", "shortage_qty": 1},
                                      {"vehicle_model": "V1", "shortage_qty": 1}])),
            [rc.E_VALIDATION])

    def test_candidate_requires_existing_shortage_and_matrix(self) -> None:
        self.svc.set_on_hand("SUB", 10)
        self.assertEqual(codes(self.svc.register_candidate("C1", "NOPE", "SUB",
                                                           [{"vehicle_model": "V1"}])),
                         [rc.E_NOT_FOUND])
        self.svc.register_shortage("S1", "M", "P", PLANNER,
                                   [{"vehicle_model": "V1", "shortage_qty": 5}])
        self.assertEqual(codes(self.svc.register_candidate("C1", "S1", "SUB", [])),
                         [rc.E_VALIDATION])
        self.assertEqual(codes(self.svc.register_candidate("C1", "S1", "SUB",
                                                           [{"vehicle_model": "V1",
                                                             "conversion_ratio": 0}])),
                         [rc.E_VALIDATION])
        self.assertEqual(codes(self.svc.register_candidate("C1", "S1", "SUB",
                                                           [{"vehicle_model": "V1"}])), [rc.OK])
        self.assertEqual(codes(self.svc.register_candidate("C1", "S1", "SUB",
                                                           [{"vehicle_model": "V1"}])),
                         [rc.E_DUPLICATE])


class RequirementTrialTest(ServiceTestBase):
    def test_substitute_need_uses_conversion_ratio(self) -> None:
        self.svc.register_shortage("S1", "OLD", "P", PLANNER,
                                   [{"vehicle_model": "V1", "shortage_qty": 100}])
        self.svc.set_on_hand("SUB", 1000)
        self.svc.register_candidate("C1", "S1", "SUB",
                                    [{"vehicle_model": "V1", "conversion_ratio": 1.25}])
        out = self.svc.run_trial("C1")
        self.assertEqual(out.data["requirement"]["V1"]["substitute_need"], 125.0)

    def test_partial_applicability_leaves_residual_gap(self) -> None:
        self.svc.register_shortage("S1", "OLD", "P", PLANNER,
                                   [{"vehicle_model": "V1", "shortage_qty": 10},
                                    {"vehicle_model": "V2", "shortage_qty": 7}])
        self.svc.set_on_hand("SUB", 1000)
        # V2 未覆盖，V3 在矩阵里显式标记不可用（另一种局部适用）
        self.svc.register_candidate("C1", "S1", "SUB", [
            {"vehicle_model": "V1"},
            {"vehicle_model": "V2", "usable": False},
        ])
        out = self.svc.run_trial("C1")
        self.assertEqual(out.data["requirement"]["V2"]["residual_gap"], 7.0)
        risk = {r["code"] for r in out.data["risks"]}
        self.assertIn(rc.E_APPLICABILITY_GAP, risk)
        # 补全矩阵后风险消失
        self.svc.update_applicability("C1", [{"vehicle_model": "V1"},
                                             {"vehicle_model": "V2"}])
        out = self.svc.run_trial("C1")
        self.assertNotIn(rc.E_APPLICABILITY_GAP, {r["code"] for r in out.data["risks"]})


class EvidenceGateTest(ServiceTestBase):
    def test_three_evidence_kinds_independently_block(self) -> None:
        self.svc.register_shortage("S1", "OLD", "P", PLANNER,
                                   [{"vehicle_model": "V1", "shortage_qty": 1}])
        self.svc.set_on_hand("SUB", 10)
        self.svc.register_candidate("C1", "S1", "SUB", [{"vehicle_model": "V1"}])
        self.svc.submit("C1")
        for role, name in ((QUALITY, "q"), (SUPPLIER, "s"), (WAREHOUSE, "w")):
            self.svc.sign("C1", role, name)

        def open_gates():
            return {r["code"] for r in self.svc.run_trial("C1").data["risks"]}

        gates = open_gates()
        self.assertIn(rc.E_ENGINEERING_UNCONFIRMED, gates)
        self.assertIn(rc.E_SUPPLY_UNCONFIRMED, gates)
        self.assertIn(rc.E_CUSTOMER_RESTRICTED, gates)

        # 没有凭据不能确认
        self.assertEqual(codes(self.svc.set_evidence_confirmation("C1", "engineering", True)),
                         [rc.E_VALIDATION])
        self.svc.add_evidence_doc("C1", "engineering", "E1")
        self.svc.set_evidence_confirmation("C1", "engineering", True)
        self.assertNotIn(rc.E_ENGINEERING_UNCONFIRMED, open_gates())
        self.svc.add_evidence_doc("C1", "supply", "S1")
        self.svc.set_evidence_confirmation("C1", "supply", True)
        self.assertNotIn(rc.E_SUPPLY_UNCONFIRMED, open_gates())
        self.svc.add_evidence_doc("C1", "customer", "K1")
        self.svc.set_evidence_confirmation("C1", "customer", True)
        self.assertNotIn(rc.E_CUSTOMER_RESTRICTED, open_gates())

    def test_unknown_evidence_kind_rejected(self) -> None:
        self.svc.register_shortage("S1", "O", "P", PLANNER,
                                   [{"vehicle_model": "V", "shortage_qty": 1}])
        self.svc.register_candidate("C1", "S1", "SUB", [{"vehicle_model": "V"}])
        self.assertEqual(codes(self.svc.add_evidence_doc("C1", "mystery", "d")),
                         [rc.E_VALIDATION])


class QuorumTest(ServiceTestBase):
    def _candidate_with_rule(self, rule_id: str, cid: str = "C1") -> None:
        self.svc.register_shortage("S1", "O", "P", PLANNER,
                                   [{"vehicle_model": "V", "shortage_qty": 1}])
        self.svc.set_on_hand("SUB", 10)
        self.svc.register_candidate(cid, "S1", "SUB", [{"vehicle_model": "V"}],
                                    quorum_rule_id=rule_id)
        for kind in ("engineering", "supply", "customer"):
            self.svc.add_evidence_doc(cid, kind, "d")
            self.svc.set_evidence_confirmation(cid, kind, True)
        self.svc.submit(cid)

    def test_quorum_requires_all_three_roles(self) -> None:
        self._candidate_with_rule("standard")
        self.svc.sign("C1", QUALITY, "q")
        self.svc.sign("C1", SUPPLIER, "s")
        self.svc.run_trial("C1")
        self.assertEqual(codes(self.svc.release("C1")), [rc.E_QUORUM_MISSING])
        self.svc.sign("C1", WAREHOUSE, "w")
        self.svc.run_trial("C1")
        self.assertEqual(codes(self.svc.release("C1")), [rc.OK])

    def test_custom_rule_with_two_quality_engineers(self) -> None:
        out = self.svc.add_quorum_rule("dual", {QUALITY: 2, SUPPLIER: 1, WAREHOUSE: 1}, 3)
        self.assertEqual(codes(out), [rc.OK])
        self.assertEqual(codes(self.svc.add_quorum_rule(
            "bad", {"外星人": 1}, 1)), [rc.E_UNKNOWN_ROLE])
        self._candidate_with_rule("dual")
        self.svc.sign("C1", QUALITY, "q1")
        self.svc.sign("C1", SUPPLIER, "s")
        self.svc.sign("C1", WAREHOUSE, "w")
        self.svc.run_trial("C1")
        self.assertEqual(codes(self.svc.release("C1")), [rc.E_QUORUM_MISSING])
        self.svc.sign("C1", QUALITY, "q2")
        self.svc.run_trial("C1")
        self.assertEqual(codes(self.svc.release("C1")), [rc.OK])

    def test_unknown_rule_rejected_at_registration(self) -> None:
        self.svc.register_shortage("S1", "O", "P", PLANNER,
                                   [{"vehicle_model": "V", "shortage_qty": 1}])
        self.assertEqual(codes(self.svc.register_candidate(
            "C1", "S1", "SUB", [{"vehicle_model": "V"}], quorum_rule_id="nope")),
            [rc.E_QUORUM_RULE_UNKNOWN])

    def test_duplicate_signature_rejected(self) -> None:
        self._candidate_with_rule("standard")
        self.assertEqual(codes(self.svc.sign("C1", QUALITY, "q")), [rc.OK])
        self.assertEqual(codes(self.svc.sign("C1", QUALITY, "q")), [rc.E_ALREADY_SIGNED])
        self.assertEqual(codes(self.svc.sign("C1", "外星人", "x")), [rc.E_UNKNOWN_ROLE])


class ReleaseGateTest(ServiceTestBase):
    def test_cannot_release_draft_or_without_trial(self) -> None:
        self.svc.register_shortage("S1", "O", "P", PLANNER,
                                   [{"vehicle_model": "V", "shortage_qty": 1}])
        self.svc.set_on_hand("SUB", 10)
        self.svc.register_candidate("C1", "S1", "SUB", [{"vehicle_model": "V"}])
        self.assertEqual(codes(self.svc.release("C1")), [rc.E_NOT_SUBMITTED])

    def test_release_requires_explicit_trial_after_basis_change(self) -> None:
        self.register_ready_candidate()
        # 签署完成但尚未试算时不能发布
        cid = self.svc.store.candidates["C1"]
        cid.trial_confirmed = False
        self.assertEqual(codes(self.svc.release("C1")), [rc.E_TRIAL_STALE])
        self.svc.run_trial("C1")
        self.assertEqual(codes(self.svc.release("C1")), [rc.OK])

    def test_mutually_exclusive_release_per_shortage(self) -> None:
        self.svc.register_shortage("S1", "O", "P", PLANNER,
                                   [{"vehicle_model": "V", "shortage_qty": 1}])
        self.svc.set_on_hand("SUB", 100)
        self.svc.set_on_hand("SUB2", 100)
        self.register_ready_candidate("C1", "S1", "SUB", ["V"])
        self.assertEqual(codes(self.svc.release("C1")), [rc.OK])
        # 同一短缺的第二个方案：齐备但不能发布
        self.svc.register_candidate("C2", "S1", "SUB2", [{"vehicle_model": "V"}])
        for kind in ("engineering", "supply", "customer"):
            self.svc.add_evidence_doc("C2", kind, "d")
            self.svc.set_evidence_confirmation("C2", kind, True)
        self.svc.submit("C2")
        self.svc.sign("C2", QUALITY, "q")
        self.svc.sign("C2", SUPPLIER, "s")
        self.svc.sign("C2", WAREHOUSE, "w")
        self.svc.run_trial("C2")
        self.assertEqual(codes(self.svc.release("C2")), [rc.E_ALREADY_RELEASED])

    def test_release_returns_item_per_blocking_reason(self) -> None:
        self.svc.register_shortage("S1", "O", "P", PLANNER,
                                   [{"vehicle_model": "V1", "shortage_qty": 1},
                                    {"vehicle_model": "V2", "shortage_qty": 1}])
        self.svc.set_on_hand("SUB", 10)
        self.svc.register_candidate("C1", "S1", "SUB", [{"vehicle_model": "V1"}])
        self.svc.submit("C1")
        self.svc.run_trial("C1")
        outcome = self.svc.release("C1")
        rejected = {i.code for i in outcome.items if not i.accepted}
        # 一次返回多个逐项原因
        self.assertIn(rc.E_APPLICABILITY_GAP, rejected)
        self.assertIn(rc.E_QUORUM_MISSING, rejected)
        self.assertIn(rc.E_ENGINEERING_UNCONFIRMED, rejected)
        self.assertFalse(outcome.accepted)


class CompetitionTest(ServiceTestBase):
    def test_two_shortages_compete_for_same_substitute(self) -> None:
        # 池 100，两短缺各需 60；先就绪者优先
        self.register_ready_candidate("C1", "S1", "SUB", ["V"], stock=100, shortage_qty=60)
        self.assertEqual(codes(self.svc.release("C1")), [rc.OK])
        self.register_ready_candidate("C2", "S2", "SUB", ["V"], stock=100, shortage_qty=60)
        out = self.svc.run_trial("C2")
        self.assertEqual(out.data["allocation"]["C1"]["allocated"], 60.0)
        self.assertEqual(out.data["allocation"]["C2"]["allocated"], 40.0)
        self.assertEqual(out.data["allocation"]["C2"]["short"], 20.0)
        self.assertEqual(codes(self.svc.release("C2")), [rc.E_SUPPLY_INSUFFICIENT])

    def test_inbound_supply_relieves_competition(self) -> None:
        self.register_ready_candidate("C1", "S1", "SUB", ["V"], stock=100, shortage_qty=60)
        self.svc.release("C1")
        self.register_ready_candidate("C2", "S2", "SUB", ["V"], stock=100, shortage_qty=60)
        self.assertEqual(codes(self.svc.release("C2")), [rc.E_SUPPLY_INSUFFICIENT])
        # 在途到货补足
        self.assertEqual(codes(self.svc.add_inbound("SUB", "INB-1", 30)), [rc.OK])
        self.svc.run_trial("C2")
        self.assertEqual(codes(self.svc.release("C2")), [rc.OK])
        # 在途取消：已下达的 C2 被撤回，C1 优先级高不受影响
        self.assertEqual(codes(self.svc.remove_inbound("SUB", "INB-1")), [rc.OK])
        self.assertEqual(self.state("C2"), State.PENDING.value)
        self.assertEqual(self.state("C1"), State.RELEASED.value)

    def test_stock_drop_revokes_released_candidate(self) -> None:
        self.register_ready_candidate("C1", "S1", "SUB", ["V"], stock=100)
        self.svc.release("C1")
        self.svc.set_on_hand("SUB", 0)
        self.assertEqual(self.state("C1"), State.PENDING.value)
        self.assertIsNone(self.svc.get_shortage("S1").data["released_candidate_id"])
        # 库存恢复并重新试算后可再发布
        self.svc.set_on_hand("SUB", 100)
        self.svc.run_trial("C1")
        self.assertEqual(codes(self.svc.release("C1")), [rc.OK])

    def test_unchanged_stock_is_noop(self) -> None:
        self.svc.set_on_hand("SUB", 10)
        self.assertEqual(codes(self.svc.set_on_hand("SUB", 10)), [rc.E_VALIDATION])


class WithdrawAndReevaluationTest(ServiceTestBase):
    def test_withdraw_signature_revokes_release(self) -> None:
        self.register_ready_candidate()
        self.svc.release("C1")
        self.assertEqual(self.state("C1"), State.RELEASED.value)
        self.assertEqual(codes(self.svc.withdraw_signature("C1", SUPPLIER, "sup-C1")), [rc.OK])
        self.assertEqual(self.state("C1"), State.PENDING.value)
        # 撤回不存在的签署
        self.assertEqual(codes(self.svc.withdraw_signature("C1", WAREHOUSE, "nobody")),
                         [rc.E_NOT_FOUND])

    def test_withdraw_keeps_claim_until_abandoned_then_frees_competitor(self) -> None:
        # 池 100：C1 已下达占 60，C2 需求 60 因竞争无法发布。
        self.register_ready_candidate("C1", "S1", "SUB", ["V"], stock=100, shortage_qty=60)
        self.svc.release("C1")
        self.register_ready_candidate("C2", "S2", "SUB", ["V"], stock=100, shortage_qty=60)
        self.assertEqual(codes(self.svc.release("C2")), [rc.E_SUPPLY_INSUFFICIENT])

        # 撤回签署只把 C1 退回待确认：它仍代表有效需求，按先登记先得继续占位
        self.svc.withdraw_signature("C1", WAREHOUSE, "wh-C1")
        self.assertEqual(self.state("C1"), State.PENDING.value)
        self.svc.run_trial("C2")
        self.assertEqual(codes(self.svc.release("C2")), [rc.E_SUPPLY_INSUFFICIENT])

        # 计划员正式撤销 C1：退出竞争，C2 重新试算后可发布
        self.assertEqual(codes(self.svc.abandon_candidate("C1", "改用他料")), [rc.OK])
        self.assertEqual(self.state("C1"), State.DRAFT.value)
        self.svc.run_trial("C2")
        self.assertEqual(codes(self.svc.release("C2")), [rc.OK])

    def test_abandon_rejected_in_fulfillment(self) -> None:
        self.register_ready_candidate(stock=100)
        self.svc.release("C1")
        self.svc.effectuate("C1")
        self.assertEqual(codes(self.svc.abandon_candidate("C1")), [rc.E_INVALID_STATE])

    def test_applicability_change_invalidates_signatures(self) -> None:
        self.register_ready_candidate(stock=1000)
        self.svc.release("C1")
        self.svc.update_applicability("C1", [{"vehicle_model": "V1"},
                                             {"vehicle_model": "V9", "usable": False}])
        # 新矩阵既导致残余缺口，也使旧签署失效
        self.assertEqual(self.state("C1"), State.PENDING.value)
        data = self.svc.get_candidate("C1").data
        self.assertTrue(all(s["basis_version"] != data["evidence_version"]
                            for s in data["signatures"]))
        # 修复矩阵并全员重签后方可再发布
        self.svc.update_applicability("C1", [{"vehicle_model": "V1"}])
        for role, name in ((QUALITY, "qe-C1"), (SUPPLIER, "sup-C1"), (WAREHOUSE, "wh-C1")):
            self.svc.sign("C1", role, name)
        self.svc.run_trial("C1")
        self.assertEqual(codes(self.svc.release("C1")), [rc.OK])


class EffectuationFreezeTest(ServiceTestBase):
    def test_effectuate_freezes_immutable_basis(self) -> None:
        self.register_ready_candidate(stock=100)
        self.svc.release("C1")
        out = self.svc.effectuate("C1")
        self.assertEqual(codes(out), [rc.OK])
        freeze = self.svc.get_candidate("C1").data["freeze"]
        self.assertEqual(freeze["stock_version"], 1)
        self.assertTrue(freeze["basis_hash"])
        before = dict(freeze)

        # 履行中库存骤降：状态不回退，冻结依据不变，但产生履行风险
        self.svc.set_on_hand("SUB", 0)
        after = self.svc.get_candidate("C1").data
        self.assertEqual(after["state"], State.FULFILLING.value)
        self.assertEqual(after["freeze"], before)
        self.assertIn("SUPPLY_INSUFFICIENT",
                      {w["code"] for w in after["warnings"] if not w["resolved"]})

        # 未处置风险不能结案
        self.assertEqual(codes(self.svc.complete("C1", PLANNER)), [rc.E_RISK_OPEN])
        self.assertEqual(codes(self.svc.resolve_warning(
            "C1", "SUPPLY_INSUFFICIENT", PLANNER, "已改用现货补救")), [rc.OK])
        self.assertEqual(codes(self.svc.complete("C1", PLANNER)), [rc.OK])
        self.assertEqual(self.state("C1"), State.CLOSED.value)
        self.assertEqual(self.svc.get_shortage("S1").data["fulfilled_candidate_id"], "C1")

    def test_closing_consumes_frozen_allocation_from_pool(self) -> None:
        # C1 需 60，池现有 40 + 在途 30；生效冻结后结案应真正扣减 60
        self.svc.register_shortage("S1", "OLD", "P", PLANNER,
                                   [{"vehicle_model": "V", "shortage_qty": 60}])
        self.svc.set_on_hand("SUB", 40)
        self.svc.add_inbound("SUB", "INB1", 30)
        self.svc.register_candidate("C1", "S1", "SUB", [{"vehicle_model": "V"}])
        for kind in ("engineering", "supply", "customer"):
            self.svc.add_evidence_doc("C1", kind, "d")
            self.svc.set_evidence_confirmation("C1", kind, True)
        self.svc.submit("C1")
        for role, name in ((QUALITY, "q"), (SUPPLIER, "s"), (WAREHOUSE, "w")):
            self.svc.sign("C1", role, name)
        self.svc.run_trial("C1")
        self.assertEqual(codes(self.svc.release("C1")), [rc.OK])
        self.assertEqual(codes(self.svc.effectuate("C1")), [rc.OK])
        self.assertEqual(codes(self.svc.complete("C1", PLANNER)), [rc.OK])
        pool = self.svc.store.pool("SUB")
        self.assertEqual(pool.available(), 10.0)  # 70 - 60
        self.assertEqual(pool.on_hand, 0.0)
        self.assertEqual(pool.inbound.get("INB1"), 10.0)

    def test_effectuate_requires_release(self) -> None:
        self.register_ready_candidate()
        # 待确认不能生效
        self.assertEqual(codes(self.svc.effectuate("C1")), [rc.E_FREEZE_REQUIRED])
        self.svc.release("C1")
        self.assertEqual(codes(self.svc.effectuate("C1")), [rc.OK])
        # 重复生效拒绝
        self.assertEqual(codes(self.svc.effectuate("C1")), [rc.E_INVALID_STATE])

    def test_freeze_records_competing_shortage_occupancy(self) -> None:
        # 池 100：C1 需求 60（先登记并生效），C2 需求 40（待确认）同时竞争
        self.register_ready_candidate("C1", "S1", "SUB", ["V"], stock=100, shortage_qty=60)
        self.svc.release("C1")
        self.register_ready_candidate("C2", "S2", "SUB", ["V"], stock=100, shortage_qty=40)
        self.assertEqual(codes(self.svc.effectuate("C1")), [rc.OK])
        freeze = self.svc.get_candidate("C1").data["freeze"]
        self.assertEqual(freeze["allocation_snapshot"]["C1"]["allocated"], 60.0)
        self.assertEqual(freeze["allocation_snapshot"]["C2"]["allocated"], 40.0)
        self.assertEqual(freeze["allocation_snapshot"]["C2"]["short"], 0.0)
        self.assertIn("C2", freeze["triggered_by_shortages"])


class PersistenceTest(unittest.TestCase):
    def test_snapshot_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "snapshot.json")
            svc = Service(Store(path))
            svc.set_on_hand("SUB", 42)
            svc.add_inbound("SUB", "INB1", 8)
            svc.register_shortage("S1", "OLD", "P", PLANNER,
                                  [{"vehicle_model": "V", "shortage_qty": 3}])
            svc.register_candidate("C1", "S1", "SUB", [{"vehicle_model": "V"}])

            restored = Service(Store(path))
            restored.store.load()
            self.assertEqual(restored.store.pool("SUB").available(), 50.0)
            self.assertIn("S1", restored.store.shortages)
            self.assertIn("C1", restored.store.candidates)
            cand = restored.store.candidates["C1"]
            self.assertEqual(cand.applicability[0].vehicle_model, "V")
            self.assertEqual(cand.state, State.DRAFT)


if __name__ == "__main__":
    unittest.main()
