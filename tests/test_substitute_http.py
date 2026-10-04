"""短缺替代料审批 HTTP 接口测试。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from substitute_approval.http_api import build_server, dispatch
from substitute_approval.service import SubstituteService

NOW = "2026-10-04T08:00:00+00:00"


def make_service() -> SubstituteService:
    return SubstituteService(clock=lambda: NOW)


def post(service, path, body):
    return dispatch(service, "POST", path, body)


class DispatchFlowTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()

    def test_full_http_flow_with_itemized_results(self):
        status, payload = post(self.service, "/events", {
            "part_number": "P-100", "demand_by_model": {"SUV-X": 20},
            "needed_by": "2026-10-20", "customers": ["客户A"],
            "actor": "张计划", "role": "采购计划员",
        })
        self.assertEqual(status, 200)
        self.assertTrue(payload["accepted"])
        event_id = payload["data"]["event_id"]

        status, payload = post(self.service, f"/events/{event_id}/candidates", {
            "substitute_part": "P-200", "actor": "张计划", "role": "采购计划员",
        })
        self.assertTrue(payload["accepted"])
        candidate_id = payload["data"]["candidate_id"]

        for path, body in [
            (f"/candidates/{candidate_id}/fit", {"fit": "适配", "actor": "李质量", "role": "质量工程师"}),
            (f"/candidates/{candidate_id}/evidence", {"kind": "台架试验", "reference": "RPT-1", "actor": "李质量", "role": "质量工程师"}),
            (f"/candidates/{candidate_id}/supply", {"qty": 30, "actor": "王供应", "role": "供应商"}),
            (f"/events/{event_id}/submit", {"actor": "张计划", "role": "采购计划员"}),
            (f"/events/{event_id}/sign", {"signer": "李质量", "role": "质量工程师"}),
            (f"/events/{event_id}/sign", {"signer": "王供应", "role": "供应商"}),
            (f"/events/{event_id}/sign", {"signer": "赵仓储", "role": "仓储管理员"}),
        ]:
            status, payload = post(self.service, path, body)
            self.assertTrue(payload["accepted"], (path, payload))

        status, payload = post(self.service, f"/events/{event_id}/evaluate", {})
        self.assertTrue(payload["accepted"])
        candidate_items = payload["data"]["report"]["candidates"][0]["items"]
        self.assertTrue(all(i["accepted"] for i in candidate_items if i["blocking"]))

        status, payload = post(self.service, f"/events/{event_id}/publish", {
            "candidate_ids": [candidate_id], "actor": "张计划", "role": "采购计划员",
        })
        self.assertTrue(payload["accepted"], payload)
        self.assertTrue(payload["data"]["basis"]["basis_hash"])

        status, payload = dispatch(self.service, "GET", f"/events/{event_id}", {})
        self.assertEqual(payload["data"]["event"]["state"], "已下达")

        status, payload = post(self.service, f"/events/{event_id}/issue/authorize", {
            "candidate_id": candidate_id, "qty": 20,
        })
        self.assertTrue(payload["accepted"])

        # 撤回签署后重新评估，领料被拦截并给出逐项原因
        status, payload = post(self.service, f"/events/{event_id}/withdraw", {
            "signer": "王供应", "actor": "王供应", "role": "供应商",
        })
        self.assertTrue(payload["accepted"])
        status, payload = post(self.service, f"/events/{event_id}/issue/authorize", {
            "candidate_id": candidate_id, "qty": 1,
        })
        self.assertFalse(payload["accepted"])
        items = {i["item"]: i for i in payload["items"]}
        self.assertIn("未正式生效", items["生效状态"]["reasons"][0])

    def test_unknown_event_returns_404(self):
        status, payload = dispatch(self.service, "GET", "/events/SH-9999", {})
        self.assertEqual(status, 404)
        self.assertFalse(payload["accepted"])
        self.assertEqual(payload["items"][0]["item"], "对象存在性")

    def test_missing_parameter_returns_400(self):
        status, payload = post(self.service, "/events", {"part_number": "P-100"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["items"][0]["item"], "请求参数")
        self.assertTrue(any("demand_by_model" in r for r in payload["items"][0]["reasons"]))

    def test_unknown_route_returns_404(self):
        status, payload = dispatch(self.service, "POST", "/nothing", {})
        self.assertEqual(status, 404)
        self.assertEqual(payload["items"][0]["item"], "路由")


class LiveServerTest(unittest.TestCase):
    def test_server_roundtrip_and_bad_json(self):
        server = build_server(make_service(), "127.0.0.1", 0)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as resp:
                self.assertEqual(resp.status, 200)

            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/events",
                data=json.dumps({
                    "part_number": "P-100", "demand_by_model": {"SUV-X": 5},
                    "needed_by": "2026-10-20", "actor": "张计划", "role": "采购计划员",
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(payload["accepted"])
            self.assertEqual(payload["data"]["state"], "草拟")

            bad = urllib.request.Request(
                f"http://127.0.0.1:{port}/events", data=b"not-json", method="POST"
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(bad)
            self.assertEqual(ctx.exception.code, 400)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
