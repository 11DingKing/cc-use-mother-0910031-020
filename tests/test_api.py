"""HTTP/JSON 接口的端到端测试（标准库，真实起服）。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from substitution import Service
from substitution.api import make_server
from substitution.models import QUALITY, SUPPLIER, WAREHOUSE


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        self.httpd = make_server("127.0.0.1", 0, service=self.service)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def _call(self, method: str, path: str, payload=None):
        url = f"http://127.0.0.1:{self.port}/{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health_and_404(self) -> None:
        status, body = self._call("GET", "health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        status, _ = self._call("GET", "nope")
        self.assertEqual(status, 404)

    def test_full_workflow_over_http_with_itemized_reasons(self) -> None:
        # 登记短缺（含两个车型）与库存
        status, body = self._call("POST", "shortages", {
            "shortage_id": "S1", "material": "OLD", "plant": "P1", "raised_by": "计划员",
            "demand": [{"vehicle_model": "V-A", "shortage_qty": 100},
                       {"vehicle_model": "V-B", "shortage_qty": 40}],
        })
        self.assertEqual(status, 200)
        self.assertTrue(body["accepted"])
        self._call("POST", "stock/on-hand", {"material": "SUB", "qty": 200})

        # 候选只覆盖 V-A（局部适用），首次发布应逐项给出多个拒绝原因
        self._call("POST", "candidates", {
            "candidate_id": "C1", "shortage_id": "S1", "substitute_material": "SUB",
            "applicability": [{"vehicle_model": "V-A", "conversion_ratio": 1.2}],
        })
        self._call("POST", "candidates/C1/submit", {})
        self._call("POST", "candidates/C1/trial", {})
        _, body = self._call("POST", "candidates/C1/release", {})
        self.assertFalse(body["accepted"])
        codes = {i["code"] for i in body["items"]}
        self.assertIn("APPLICABILITY_GAP", codes)
        self.assertIn("QUORUM_MISSING", codes)
        self.assertIn("ENGINEERING_UNCONFIRMED", codes)

        # 逐项补齐：补矩阵、三类证据、三角色签署、重新试算
        self._call("POST", "candidates/C1/applicability", {"applicability": [
            {"vehicle_model": "V-A", "conversion_ratio": 1.2},
            {"vehicle_model": "V-B", "conversion_ratio": 1.0}]})
        for kind in ("engineering", "supply", "customer"):
            self._call("POST", "candidates/C1/evidence",
                       {"kind": kind, "doc_id": f"D-{kind}", "note": kind})
            self._call("POST", "candidates/C1/evidence/confirm",
                       {"kind": kind, "value": True})
        for role, name in ((QUALITY, "质量"), (SUPPLIER, "供应"), (WAREHOUSE, "仓储")):
            self._call("POST", "candidates/C1/sign", {"role": role, "signer": name})
        self._call("POST", "candidates/C1/trial", {})
        _, body = self._call("POST", "candidates/C1/release", {})
        self.assertTrue(body["accepted"], body)

        # 生效冻结、查询冻结指纹
        _, body = self._call("POST", "candidates/C1/effectuate", {})
        self.assertTrue(body["accepted"])
        _, body = self._call("GET", "candidates/C1")
        self.assertEqual(body["data"]["state"], "履行中")
        self.assertTrue(body["data"]["freeze"]["basis_hash"])

    def test_invalid_json_returns_400(self) -> None:
        url = f"http://127.0.0.1:{self.port}/shortages"
        req = urllib.request.Request(url, data=b"{not-json", method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("应当返回 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)


if __name__ == "__main__":
    unittest.main()
