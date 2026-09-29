"""灾后恢复治理 HTTP JSON 接口测试。"""

from __future__ import annotations

import json
import sqlite3
import unittest

from recovery_governance.api import JsonApplication
from recovery_governance.service import RecoveryGovernanceService


def _body(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class RecoveryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        service = RecoveryGovernanceService(self.connection)
        service.bootstrap()
        self.app = JsonApplication(service)
        self.coord = {"X-Actor-Id": "coord"}
        self.rev = {"X-Actor-Id": "rev"}
        self.audit_h = {"X-Actor-Id": "audit"}

    def tearDown(self) -> None:
        self.connection.close()

    def _request(self, method: str, path: str, payload: dict | None = None, headers=None):
        return self.app.handle(
            method, path, headers or {}, _body(payload) if payload is not None else b""
        )

    def _seed_area_plan(self) -> None:
        self._request("POST", "/areas", {
            "area_id": "area-1", "name": "测试步道", "scope": "visitor"}, self.coord)
        self._request("POST", "/evidence", {
            "evidence_id": "ev-a", "version": 1, "title": "边坡复核",
            "evidence_type": "slope", "payload": {"risk": "low"}}, self.rev)
        self._request("POST", "/plans", {
            "plan_id": "plan-1", "area_id": "area-1", "title": "恢复计划",
            "stages": [{
                "stage_id": "stg-0", "ordinal": 0, "name": "限流",
                "grants_state": "limited", "validity_hours": 24,
                "channels": ["visitor"], "reviewer_role": "reviewer",
                "evidence": [{"evidence_id": "ev-a", "version": 1,
                              "minimum_items": ["边坡稳定"]}],
            }]}, self.coord)
        self._request("POST", "/plans/plan-1/issue", {"expected_revision": 1}, self.coord)

    def test_health_and_public_status_anonymous(self) -> None:
        health = self.app.handle("GET", "/health")
        self.assertEqual(health.status, 200)
        status = self.app.handle("GET", "/public/status")
        self.assertEqual(status.status, 200)
        self.assertEqual(status.body["areas"], [])

    def test_full_flow_and_error_codes(self) -> None:
        self._seed_area_plan()
        # 激活前无身份读取投影会被要求鉴权
        denied = self.app.handle("GET", "/areas/area-1/projection")
        self.assertEqual(denied.status, 403)
        # 未复核时激活失败
        blocked = self._request("POST", "/stages/stg-0/activate", {"note": ""},
                                {**self.coord, "Idempotency-Key": "k-1"})
        self.assertEqual(blocked.status, 409)
        self.assertEqual(blocked.body["error"]["code"], "invalid_state")
        # 复核
        review = self._request("POST", "/stages/stg-0/reviews", {
            "evidence_id": "ev-a", "evidence_version": 1,
            "confirmed_items": ["边坡稳定"]}, self.rev)
        self.assertEqual(review.status, 201)
        # 缺少幂等头
        missing_key = self._request("POST", "/stages/stg-0/activate", {}, self.coord)
        self.assertEqual(missing_key.status, 422)
        activated = self._request("POST", "/stages/stg-0/activate", {"note": "放行"},
                                  {**self.coord, "Idempotency-Key": "k-1"})
        self.assertEqual(activated.status, 200)
        activation_id = activated.body["activation_id"]
        # 公众视图只显示开放状态
        public = self.app.handle("GET", "/public/status")
        self.assertEqual(public.body["areas"][0]["public_state"], "limited")
        self.assertEqual(set(public.body["areas"][0]),
                         {"area_id", "name", "scope", "scope_label",
                          "public_state", "public_state_label"})
        # 现场执行
        execution = self._request(
            "POST", f"/activations/{activation_id}/executions",
            {"channel": "visitor", "detail": {"visitors": 50}}, self.coord)
        self.assertEqual(execution.status, 201)
        # 证据失效收紧
        invalidated = self._request(
            "POST", "/evidence/ev-a/1/invalidate", {"reason": "复查异常"}, self.rev)
        self.assertEqual(invalidated.status, 200)
        self.assertEqual(invalidated.body["tightened_activations"][0]["state"],
                         "evidence_lapsed")
        # 审计重建
        record = self._request("GET", f"/activations/{activation_id}/record", None, self.audit_h)
        self.assertEqual(record.status, 200)
        self.assertEqual(record.body["state"], "evidence_lapsed")
        self.assertEqual(len(record.body["field_executions"]), 1)
        chain = self._request("GET", "/audit/chain", None, self.audit_h)
        self.assertTrue(chain.body["valid"])

    def test_unknown_route(self) -> None:
        response = self.app.handle("GET", "/nope")
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
