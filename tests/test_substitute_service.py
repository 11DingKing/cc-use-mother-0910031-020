"""短缺替代料审批服务的行为测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from substitute_approval.service import SubstituteService

NOW = "2026-10-04T08:00:00+00:00"
PLANNER = ("张计划", "采购计划员")
QUALITY = ("李质量", "质量工程师")
SUPPLIER = ("王供应", "供应商")
WAREHOUSE = ("赵仓储", "仓储管理员")


def make_service() -> SubstituteService:
    return SubstituteService(clock=lambda: NOW)


def register_event(service, *, part="P-100", demand=None, needed_by="2026-10-20", customers=("客户A",)):
    result = service.register_event(
        part_number=part,
        demand_by_model=demand or {"SUV-X": 20},
        needed_by=needed_by,
        customers=customers,
        actor=PLANNER[0],
        role=PLANNER[1],
    )
    assert result.accepted, result.to_dict()
    return result.data["event_id"]


def add_candidate(service, event_id, *, part="P-200", models=()):
    result = service.add_candidate(
        event_id=event_id,
        substitute_part=part,
        applicable_models=models,
        actor=PLANNER[0],
        role=PLANNER[1],
    )
    assert result.accepted, result.to_dict()
    return result.data["candidate_id"]


def confirm_candidate(service, candidate_id, *, supply=30, models=()):
    assert service.set_engineering_fit(
        candidate_id=candidate_id, fit="适配", actor=QUALITY[0], role=QUALITY[1]
    ).accepted
    assert service.add_evidence(
        candidate_id=candidate_id,
        kind="台架试验",
        reference="RPT-1",
        vehicle_models=models,
        actor=QUALITY[0],
        role=QUALITY[1],
    ).accepted
    assert service.set_supply(
        candidate_id=candidate_id, qty=supply, actor=SUPPLIER[0], role=SUPPLIER[1]
    ).accepted


def submit_and_sign(service, event_id):
    assert service.submit(event_id=event_id, actor=PLANNER[0], role=PLANNER[1]).accepted
    for signer, role in (QUALITY, SUPPLIER, WAREHOUSE):
        assert service.sign(event_id=event_id, signer=signer, role=role).accepted


def ready_event(service, **kwargs):
    """完成登记、三方确认、提交与签署，返回 (event_id, candidate_id)。"""
    supply = kwargs.pop("supply", 30)
    event_id = register_event(service, **kwargs)
    candidate_id = add_candidate(service, event_id)
    confirm_candidate(service, candidate_id, supply=supply)
    submit_and_sign(service, event_id)
    return event_id, candidate_id


def item_of(result, name):
    for item in result.items:
        if item.item == name:
            return item
    raise AssertionError(f"缺少逐项结论 {name}")


class FullFlowTest(unittest.TestCase):
    def test_full_flow_publish_issue_close(self):
        service = make_service()
        event_id, candidate_id = ready_event(service)

        evaluation = service.evaluate(event_id=event_id)
        self.assertTrue(evaluation.accepted)
        report = evaluation.data["report"]
        candidate_report = report["candidates"][0]
        self.assertEqual(candidate_report["risk_level"], "低")
        self.assertEqual(candidate_report["needed_qty"], 20)
        self.assertEqual(candidate_report["covered_qty"], 20)

        published = service.publish(
            event_id=event_id, candidate_ids=[candidate_id], actor=PLANNER[0], role=PLANNER[1]
        )
        self.assertTrue(published.accepted, published.to_dict())
        basis = published.data["basis"]
        self.assertTrue(basis["basis_hash"])
        self.assertFalse(basis["superseded"])
        self.assertEqual(service.get_event(event_id).data["event"]["state"], "已下达")

        authorized = service.authorize_issue(event_id=event_id, candidate_id=candidate_id, qty=20)
        self.assertTrue(authorized.accepted, authorized.to_dict())
        issued = service.record_issue(
            event_id=event_id, candidate_id=candidate_id, qty=20,
            actor=WAREHOUSE[0], role=WAREHOUSE[1],
        )
        self.assertTrue(issued.accepted)
        self.assertEqual(issued.data["state"], "履行中")
        closed = service.close(event_id=event_id, actor=PLANNER[0], role=PLANNER[1])
        self.assertTrue(closed.accepted)
        self.assertEqual(service.get_event(event_id).data["event"]["state"], "已关闭")

    def test_issue_gate_blocks_unconfirmed_substitute(self):
        service = make_service()
        event_id, candidate_id = ready_event(service)
        # 待确认但未发布：禁止进入领料
        result = service.authorize_issue(event_id=event_id, candidate_id=candidate_id, qty=5)
        self.assertFalse(result.accepted)
        state_item = item_of(result, "生效状态")
        self.assertIn("未正式生效", state_item.reasons[0])


class EvaluationRejectionTest(unittest.TestCase):
    def test_itemized_rejection_reasons(self):
        service = make_service()
        event_id = register_event(service, demand={"SUV-X": 10, "SED-Y": 10})
        candidate_id = add_candidate(service, event_id)
        service.set_customer_restriction(
            customer="客户A", vehicle_model="SUV-X", original_part="P-100",
            banned_substitutes=("P-200",), note="客户认证清单",
            actor=PLANNER[0], role=PLANNER[1],
        )
        # 不做工程适配、不登记证据、不确认供应
        assert service.submit(event_id=event_id, actor=PLANNER[0], role=PLANNER[1]).accepted
        for signer, role in (QUALITY, SUPPLIER, WAREHOUSE):
            service.sign(event_id=event_id, signer=signer, role=role)

        result = service.evaluate(event_id=event_id)
        self.assertFalse(result.accepted)
        candidate_report = result.data["report"]["candidates"][0]
        items = {i["item"]: i for i in candidate_report["items"]}
        self.assertEqual(items["工程适配"]["reasons"], ["工程适配未确认"])
        self.assertFalse(items["验证证据覆盖"]["accepted"])
        self.assertEqual(len(items["验证证据覆盖"]["reasons"]), 2)
        self.assertIn("客户 客户A 禁止替代料 P-200", items["客户限制"]["reasons"][0])
        self.assertFalse(items["供应可用量"]["accepted"])

    def test_quorum_shortage_blocks_event(self):
        service = make_service()
        event_id = register_event(service)
        candidate_id = add_candidate(service, event_id)
        confirm_candidate(service, candidate_id)
        service.submit(event_id=event_id, actor=PLANNER[0], role=PLANNER[1])
        service.sign(event_id=event_id, signer=QUALITY[0], role=QUALITY[1])

        result = service.evaluate(event_id=event_id)
        self.assertFalse(result.accepted)
        quorum = item_of(result, "审批法定人数")
        self.assertIn("供应商有效签署 0/1", quorum.reasons)
        self.assertIn("仓储管理员有效签署 0/1", quorum.reasons)

    def test_partial_applicability_is_reported_not_blocking(self):
        service = make_service()
        event_id = register_event(service, demand={"SUV-X": 10, "SED-Y": 30})
        candidate_id = add_candidate(service, event_id, models=("SUV-X",))
        confirm_candidate(service, candidate_id, supply=10, models=("SUV-X",))
        submit_and_sign(service, event_id)

        result = service.evaluate(event_id=event_id)
        self.assertTrue(result.accepted, result.to_dict())
        candidate_report = result.data["report"]["candidates"][0]
        self.assertEqual(candidate_report["needed_qty"], 10)
        self.assertEqual(candidate_report["risk_level"], "中")
        coverage = {i["item"]: i for i in candidate_report["items"]}["适用车型覆盖"]
        self.assertFalse(coverage["accepted"])
        self.assertFalse(coverage["blocking"])
        self.assertIn("SED-Y", coverage["reasons"][0])
        residual = item_of(result, "替代覆盖完整性")
        self.assertFalse(residual.accepted)
        self.assertIn("SED-Y 短缺 30 件", residual.reasons[0])


class ReevaluationTest(unittest.TestCase):
    def test_withdraw_signature_after_publish_requires_reevaluation(self):
        service = make_service()
        event_id, candidate_id = ready_event(service)
        first = service.publish(
            event_id=event_id, candidate_ids=[candidate_id], actor=PLANNER[0], role=PLANNER[1]
        )
        self.assertTrue(first.accepted)

        withdrawn = service.withdraw_signature(
            event_id=event_id, signer=SUPPLIER[0], actor=SUPPLIER[0], role=SUPPLIER[1]
        )
        self.assertTrue(withdrawn.accepted)
        event = service.get_event(event_id).data["event"]
        self.assertEqual(event["state"], "待确认")
        self.assertTrue(event["reevaluation_pending"])
        self.assertTrue(event["frozen_basis"]["superseded"])

        blocked = service.authorize_issue(event_id=event_id, candidate_id=candidate_id, qty=1)
        self.assertFalse(blocked.accepted)

        # 法定人数不足，试算不通过，发布被拒绝
        self.assertFalse(service.evaluate(event_id=event_id).accepted)
        rejected = service.publish(
            event_id=event_id, candidate_ids=[candidate_id], actor=PLANNER[0], role=PLANNER[1]
        )
        self.assertFalse(rejected.accepted)

        # 重新签署后再次试算、发布，生成新的冻结依据
        service.sign(event_id=event_id, signer=SUPPLIER[0], role=SUPPLIER[1])
        self.assertTrue(service.evaluate(event_id=event_id).accepted)
        second = service.publish(
            event_id=event_id, candidate_ids=[candidate_id], actor=PLANNER[0], role=PLANNER[1]
        )
        self.assertTrue(second.accepted, second.to_dict())
        self.assertNotEqual(first.data["basis"]["basis_id"], second.data["basis"]["basis_id"])
        self.assertEqual(service.get_event(event_id).data["event"]["state"], "已下达")

    def test_inventory_change_triggers_reevaluation(self):
        service = make_service()
        event_id, candidate_id = ready_event(service, supply=5)
        service.set_inventory(part="P-200", qty=25, actor=WAREHOUSE[0], role=WAREHOUSE[1])
        self.assertTrue(service.evaluate(event_id=event_id).accepted)
        self.assertTrue(
            service.publish(
                event_id=event_id, candidate_ids=[candidate_id], actor=PLANNER[0], role=PLANNER[1]
            ).accepted
        )

        service.set_inventory(part="P-200", qty=0, actor=WAREHOUSE[0], role=WAREHOUSE[1])
        service.set_supply(candidate_id=candidate_id, qty=0, actor=SUPPLIER[0], role=SUPPLIER[1])
        event = service.get_event(event_id).data["event"]
        self.assertEqual(event["state"], "待确认")
        self.assertTrue(event["reevaluation_pending"])

        result = service.evaluate(event_id=event_id)
        self.assertFalse(result.accepted)
        items = {i["item"]: i for i in result.data["report"]["candidates"][0]["items"]}
        supply_item = items["供应可用量"]
        self.assertIn("供应缺口 20 件", supply_item["reasons"][0])
        self.assertFalse(
            service.publish(
                event_id=event_id, candidate_ids=[candidate_id], actor=PLANNER[0], role=PLANNER[1]
            ).accepted
        )

    def test_evidence_invalidation_triggers_reevaluation(self):
        service = make_service()
        event_id, candidate_id = ready_event(service)
        self.assertTrue(
            service.publish(
                event_id=event_id, candidate_ids=[candidate_id], actor=PLANNER[0], role=PLANNER[1]
            ).accepted
        )
        evidence_id = service.get_event(event_id).data["event"]["candidates"][0]["evidences"][0]["evidence_id"]
        service.invalidate_evidence(evidence_id=evidence_id, actor=QUALITY[0], role=QUALITY[1])
        self.assertEqual(service.get_event(event_id).data["event"]["state"], "待确认")
        result = service.evaluate(event_id=event_id)
        self.assertFalse(result.accepted)
        items = {i["item"]: i for i in result.data["report"]["candidates"][0]["items"]}
        self.assertIn("SUV-X 缺少有效验证证据", items["验证证据覆盖"]["reasons"][0])

    def test_frozen_basis_keeps_snapshot_after_mutation(self):
        service = make_service()
        event_id, candidate_id = ready_event(service)
        published = service.publish(
            event_id=event_id, candidate_ids=[candidate_id], actor=PLANNER[0], role=PLANNER[1]
        )
        basis = published.data["basis"]

        service.set_inventory(part="P-200", qty=99, actor=WAREHOUSE[0], role=WAREHOUSE[1])
        service.update_applicability(
            candidate_id=candidate_id, applicable_models=(), actor=PLANNER[0], role=PLANNER[1]
        )
        frozen = service.get_basis(event_id).data["basis"]
        self.assertTrue(frozen["superseded"])
        self.assertEqual(frozen["basis_hash"], basis["basis_hash"])
        self.assertEqual(frozen["supplier_qtys"], basis["supplier_qtys"])
        self.assertEqual(frozen["demand_by_model"], {"SUV-X": 20})


class CompetitionTest(unittest.TestCase):
    def test_competing_shortages_share_inventory_by_priority(self):
        service = make_service()
        service.set_inventory(part="P-200", qty=10, actor=WAREHOUSE[0], role=WAREHOUSE[1])
        first_id = register_event(service, part="P-100", demand={"SUV-X": 10}, needed_by="2026-10-20")
        first_candidate = add_candidate(service, first_id)
        confirm_candidate(service, first_candidate, supply=0)
        submit_and_sign(service, first_id)
        second_id = register_event(service, part="P-101", demand={"SUV-X": 10}, needed_by="2026-10-25")
        second_candidate = add_candidate(service, second_id)
        confirm_candidate(service, second_candidate, supply=0)
        submit_and_sign(service, second_id)

        first_eval = service.evaluate(event_id=first_id)
        self.assertTrue(first_eval.accepted, first_eval.to_dict())
        self.assertEqual(first_eval.data["report"]["candidates"][0]["inventory_grant"], 10)

        second_eval = service.evaluate(event_id=second_id)
        self.assertFalse(second_eval.accepted)
        items = {i["item"]: i for i in second_eval.data["report"]["candidates"][0]["items"]}
        supply_item = items["供应可用量"]
        self.assertIn(f"被更高优先级短缺 {first_id} 占用", supply_item["reasons"][-1])
        self.assertIn(first_id, second_eval.data["report"]["competing_events"])
        self.assertIn(second_id, first_eval.data["report"]["competing_events"])

    def test_new_competitor_flags_released_event(self):
        service = make_service()
        service.set_inventory(part="P-200", qty=10, actor=WAREHOUSE[0], role=WAREHOUSE[1])
        first_id = register_event(service, part="P-100", demand={"SUV-X": 10}, needed_by="2026-10-20")
        first_candidate = add_candidate(service, first_id)
        confirm_candidate(service, first_candidate, supply=0)
        submit_and_sign(service, first_id)
        self.assertTrue(
            service.publish(
                event_id=first_id, candidate_ids=[first_candidate], actor=PLANNER[0], role=PLANNER[1]
            ).accepted
        )

        # 新的短缺竞争同一替代料：已下达事件被回退并要求重新评估
        second_id = register_event(service, part="P-101", demand={"SUV-X": 10}, needed_by="2026-10-25")
        second_candidate = add_candidate(service, second_id)
        confirm_candidate(service, second_candidate, supply=0)
        submit_and_sign(service, second_id)
        event = service.get_event(first_id).data["event"]
        self.assertEqual(event["state"], "待确认")
        self.assertTrue(event["reevaluation_pending"])
        self.assertIn("竞争", event["reevaluation_reasons"][-1])

        # 先到期者优先，先发布者重新试算仍获全部分配
        self.assertTrue(service.evaluate(event_id=first_id).accepted)
        self.assertTrue(
            service.publish(
                event_id=first_id, candidate_ids=[first_candidate], actor=PLANNER[0], role=PLANNER[1]
            ).accepted
        )
        second_eval = service.evaluate(event_id=second_id)
        self.assertFalse(second_eval.accepted)


class RoleAndValidationTest(unittest.TestCase):
    def test_role_enforcement(self):
        service = make_service()
        event_id = register_event(service)
        candidate_id = add_candidate(service, event_id)
        result = service.set_engineering_fit(
            candidate_id=candidate_id, fit="适配", actor=SUPPLIER[0], role=SUPPLIER[1]
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.items[0].item, "角色权限")
        self.assertIn("供应商无权执行该操作", result.items[0].reasons[0])

    def test_register_event_validation(self):
        service = make_service()
        result = service.register_event(
            part_number="", demand_by_model={"SUV-X": 0}, needed_by="2026/10/20",
            actor=PLANNER[0], role=PLANNER[1],
        )
        self.assertFalse(result.accepted)
        reasons = result.items[0].reasons
        self.assertTrue(any("零件号" in r for r in reasons))
        self.assertTrue(any("正整数" in r for r in reasons))
        self.assertTrue(any("YYYY-MM-DD" in r for r in reasons))


if __name__ == "__main__":
    unittest.main()
