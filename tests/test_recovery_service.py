"""灾后恢复治理领域规则、事务边界与收紧机制测试。"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from recovery_governance.clock import FrozenClock
from recovery_governance.errors import (
    Conflict,
    Forbidden,
    InvalidState,
    NotFound,
    ValidationFailed,
)
from recovery_governance.service import RecoveryGovernanceService


def evidence(evidence_id: str, version: int, payload: dict | None = None) -> dict:
    return {
        "evidence_id": evidence_id,
        "version": version,
        "title": f"{evidence_id} 第 {version} 版",
        "evidence_type": "inspection",
        "payload": payload or {"ok": True, "v": version},
    }


def stage(stage_id: str, ordinal: int, *, grants="limited", channels=None,
          reviewer_role="reviewer", reviewer_id=None, requires=None, evidence_spec=None) -> dict:
    return {
        "stage_id": stage_id,
        "ordinal": ordinal,
        "name": f"阶段 {ordinal}",
        "grants_state": grants,
        "validity_hours": 24,
        "channels": channels or ["patrol"],
        "reviewer_role": reviewer_role,
        "reviewer_id": reviewer_id,
        "requires_stages": requires or [],
        "evidence": evidence_spec or [
            {"evidence_id": "ev-a", "version": 1, "minimum_items": ["项一", "项二"]}
        ],
    }


class RecoveryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 29, 2, 0, tzinfo=timezone.utc))
        self.service = RecoveryGovernanceService(self.connection, self.clock)
        self.service.bootstrap()
        self.service.register_area("coord", {"area_id": "area-1", "name": "测试区域", "scope": "patrol"})
        self.service.submit_evidence("rev", evidence("ev-a", 1))

    def tearDown(self) -> None:
        self.connection.close()

    def _plan(self, plan_id: str = "plan-1", stages: list[dict] | None = None, area_id: str = "area-1") -> dict:
        return {
            "plan_id": plan_id,
            "area_id": area_id,
            "title": "测试恢复计划",
            "stages": stages or [stage("stg-0", 0)],
        }

    def test_evidence_versions_must_be_sequential(self) -> None:
        self.service.submit_evidence("rev", evidence("ev-a", 2, {"ok": True, "v": 2}))
        with self.assertRaises(Conflict):
            self.service.submit_evidence("rev", evidence("ev-a", 2))

    def test_plan_validation(self) -> None:
        # 前置阶段必须存在
        bad = self._plan(stages=[stage("stg-0", 0, requires=[3])])
        with self.assertRaises(ValidationFailed):
            self.service.create_plan("coord", bad)
        # 开放级别不能倒退
        descending = self._plan(stages=[
            stage("stg-open", 0, grants="open", channels=["visitor"]),
            stage("stg-limited", 1, grants="limited", channels=["visitor"], requires=[0]),
        ])
        with self.assertRaises(ValidationFailed):
            self.service.create_plan("coord", descending)
        # closed 阶段不能放行游客/经营
        with self.assertRaises(ValidationFailed):
            self.service.create_plan("coord", self._plan(stages=[
                stage("stg-closed", 0, grants="closed", channels=["visitor"])
            ]))

    def test_activation_requires_issued_plan_and_review(self) -> None:
        self.service.create_plan("coord", self._plan())
        with self.assertRaises(InvalidState):
            self.service.activate_stage("coord", "stg-0", "k-1")
        self.service.issue_plan("coord", "plan-1", 1)
        # 未复核最低通过项
        with self.assertRaises(InvalidState):
            self.service.activate_stage("coord", "stg-0", "k-1")
        with self.assertRaises(ValidationFailed):
            self.service.submit_review("rev", "stg-0", "ev-a", 1, ["项一"])
        self.service.submit_review("rev", "stg-0", "ev-a", 1, ["项二", "项一"])
        activated = self.service.activate_stage("coord", "stg-0", "k-1")
        self.assertEqual(activated["state"], "active")
        self.assertEqual(activated["channels"], ["patrol"])
        # 同阶段重复激活必须走幂等键
        with self.assertRaises(Conflict):
            self.service.activate_stage("coord", "stg-0", "k-other")

    def test_reviewer_assignment_is_enforced(self) -> None:
        plan = self._plan(stages=[
            stage("stg-0", 0, reviewer_role="coordinator", reviewer_id="coord")
        ])
        self.service.create_plan("coord", plan)
        self.service.issue_plan("coord", "plan-1", 1)
        with self.assertRaises(Forbidden):
            self.service.submit_review("rev", "stg-0", "ev-a", 1, ["项一", "项二"])
        self.service.submit_review("coord", "stg-0", "ev-a", 1, ["项一", "项二"])
        activated = self.service.activate_stage("coord", "stg-0", "k-1")
        self.assertTrue(activated["state"] == "active")

    def test_prerequisite_stage_must_hold(self) -> None:
        self.service.submit_evidence("rev", evidence("ev-b", 1, {"air": 30}))
        self.service.create_plan("coord", self._plan(stages=[
            stage("stg-0", 0, evidence_spec=[
                {"evidence_id": "ev-a", "version": 1, "minimum_items": ["项一", "项二"]}]),
            stage("stg-1", 1, grants="open", channels=["patrol", "visitor"], requires=[0],
                  evidence_spec=[
                      {"evidence_id": "ev-b", "version": 1, "minimum_items": ["空气达标"]}]),
        ]))
        self.service.issue_plan("coord", "plan-1", 1)
        self.service.submit_review("rev", "stg-0", "ev-a", 1, ["项一", "项二"])
        self.service.submit_review("rev", "stg-1", "ev-b", 1, ["空气达标"])
        # 前置阶段未生效，拒绝扩大业务范围
        with self.assertRaises(InvalidState):
            self.service.activate_stage("coord", "stg-1", "k-2")
        self.service.activate_stage("coord", "stg-0", "k-1")
        activated = self.service.activate_stage("coord", "stg-1", "k-2")
        self.assertEqual(activated["grants_state"], "open")

    def test_idempotent_replay_and_conflict(self) -> None:
        self.service.create_plan("coord", self._plan())
        self.service.issue_plan("coord", "plan-1", 1)
        self.service.submit_review("rev", "stg-0", "ev-a", 1, ["项一", "项二"])
        first = self.service.activate_stage("coord", "stg-0", "idem-1", "首次")
        second = self.service.activate_stage("coord", "stg-0", "idem-1", "首次")
        self.assertTrue(second["replayed"])
        self.assertEqual(second["activation_id"], first["activation_id"])
        self.assertNotEqual(second["replay_activation_id"], first["activation_id"])
        with self.assertRaises(Conflict):
            self.service.activate_stage("coord", "stg-0", "idem-1", "不同备注")
        # 回放记录不产生新的放行效果
        projection = self.service.projection("coord", "area-1")
        self.assertEqual(projection["public_state"], "limited")

    def test_evidence_invalidation_tightens_and_cascades(self) -> None:
        self.service.submit_evidence("rev", evidence("ev-b", 1))
        self.service.create_plan("coord", self._plan(stages=[
            stage("stg-0", 0),
            stage("stg-1", 1, requires=[0],
                  evidence_spec=[{"evidence_id": "ev-b", "version": 1, "minimum_items": ["乙"]}]),
        ]))
        self.service.issue_plan("coord", "plan-1", 1)
        self.service.submit_review("rev", "stg-0", "ev-a", 1, ["项一", "项二"])
        self.service.submit_review("rev", "stg-1", "ev-b", 1, ["乙"])
        self.service.activate_stage("coord", "stg-0", "k-1")
        second = self.service.activate_stage("coord", "stg-1", "k-2")
        self.assertEqual(second["state"], "active")
        # 第一阶段证据失效：第一阶段立即收紧，第二阶段因前置失效级联收紧
        result = self.service.invalidate_evidence("rev", "ev-a", 1, "报告作废")
        states = {item["stage_id"]: item["state"] for item in result["tightened_activations"]}
        self.assertEqual(states, {"stg-0": "evidence_lapsed", "stg-1": "prerequisite_lapsed"})
        projection = self.service.projection("coord", "area-1")
        self.assertEqual(projection["public_state"], "closed")
        self.assertFalse(any(data["granted"] for data in projection["channels"].values()))
        # 已失效证据不能重复作废
        with self.assertRaises(InvalidState):
            self.service.invalidate_evidence("rev", "ev-a", 1, "再次作废")

    def test_expiry_tightens_automatically(self) -> None:
        self.service.create_plan("coord", self._plan())
        self.service.issue_plan("coord", "plan-1", 1)
        self.service.submit_review("rev", "stg-0", "ev-a", 1, ["项一", "项二"])
        activated = self.service.activate_stage("coord", "stg-0", "k-1")
        self.clock.advance(hours=25)
        result = self.service.sweep()
        self.assertEqual(
            [(item["activation_id"], item["state"]) for item in result["tightened_activations"]],
            [(activated["activation_id"], "expired")],
        )
        # 到期后同阶段可凭新幂等键重新发布
        self.clock.advance(hours=-25)
        reactivated = self.service.activate_stage("coord", "stg-0", "k-2")
        self.assertEqual(reactivated["state"], "active")

    def test_plan_revocation_tightens_all(self) -> None:
        self.service.create_plan("coord", self._plan())
        self.service.issue_plan("coord", "plan-1", 1)
        self.service.submit_review("rev", "stg-0", "ev-a", 1, ["项一", "项二"])
        self.service.activate_stage("coord", "stg-0", "k-1")
        result = self.service.revoke_plan("coord", "plan-1", "火情反复")
        self.assertEqual(result["tightened_activations"][0]["state"], "plan_revoked")
        with self.assertRaises(InvalidState):
            self.service.activate_stage("coord", "stg-0", "k-2")

    def test_execution_reads_single_projection(self) -> None:
        self.service.create_plan("coord", self._plan(stages=[
            stage("stg-0", 0, channels=["patrol", "research"])
        ]))
        self.service.issue_plan("coord", "plan-1", 1)
        self.service.submit_review("rev", "stg-0", "ev-a", 1, ["项一", "项二"])
        activated = self.service.activate_stage("coord", "stg-0", "k-1")
        self.service.record_execution("coord", activated["activation_id"], "patrol", {"km": 3})
        # 游客接口未获本阶段放行
        with self.assertRaises(Forbidden):
            self.service.record_execution("coord", activated["activation_id"], "visitor", {"n": 1})
        # 指令撤回后现场业务立即被拒
        self.service.revoke_activation("coord", activated["activation_id"], "临时封控")
        with self.assertRaises(InvalidState):
            self.service.record_execution("coord", activated["activation_id"], "patrol", {"km": 4})

    def test_public_status_is_minimal(self) -> None:
        before = self.service.public_status()
        self.assertEqual(before["areas"][0]["public_state"], "closed")
        self.assertNotIn("channels", before["areas"][0])
        self.service.create_plan("coord", self._plan())
        self.service.issue_plan("coord", "plan-1", 1)
        self.service.submit_review("rev", "stg-0", "ev-a", 1, ["项一", "项二"])
        self.service.activate_stage("coord", "stg-0", "k-1")
        after = self.service.public_status()
        self.assertEqual(after["areas"][0]["public_state"], "limited")
        self.assertEqual(set(after["areas"][0]),
                         {"area_id", "name", "scope", "scope_label", "public_state", "public_state_label"})

    def test_audit_rebuild_and_chain(self) -> None:
        self.service.create_plan("coord", self._plan())
        self.service.issue_plan("coord", "plan-1", 1)
        self.service.submit_review("rev", "stg-0", "ev-a", 1, ["项一", "项二"], "复核备注")
        activated = self.service.activate_stage("coord", "stg-0", "k-1", "生效备注")
        self.service.record_execution("coord", activated["activation_id"], "patrol", {"km": 9})
        self.service.invalidate_evidence("rev", "ev-a", 1, "撤回")
        record = self.service.decision_record("audit", activated["activation_id"])
        self.assertEqual(record["state"], "evidence_lapsed")
        self.assertEqual(record["evidence_basis"][0]["review"]["confirmed_items"], ["项一", "项二"])
        self.assertEqual(record["evidence_basis"][0]["current_state"], "invalidated")
        self.assertEqual(len(record["field_executions"]), 1)
        kinds = [event["event_type"] for event in record["timeline"]]
        self.assertEqual(kinds, ["activation.issued", "activation.evidence_lapsed"])
        chain = self.service.audit_chain("audit")
        self.assertTrue(chain["valid"])
        self.assertGreater(chain["events"], 5)

    def test_permissions(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.register_area("rev", {"area_id": "x", "name": "x", "scope": "patrol"})
        # 审计员不能修改业务
        with self.assertRaises(Forbidden):
            self.service.submit_evidence("audit", evidence("ev-x", 1))
        # 复核员不能发布计划
        with self.assertRaises(Forbidden):
            self.service.create_plan("rev", self._plan(plan_id="plan-z"))
        # 未知用户
        with self.assertRaises(NotFound):
            self.service.projection("nobody", "area-1")

    def test_channel_projection_is_shared(self) -> None:
        self.service.create_plan("coord", self._plan(stages=[
            stage("stg-0", 0, channels=["patrol", "research"])
        ]))
        self.service.issue_plan("coord", "plan-1", 1)
        self.service.submit_review("rev", "stg-0", "ev-a", 1, ["项一", "项二"])
        self.service.activate_stage("coord", "stg-0", "k-1")
        patrol = self.service.channel_projection("coord", "patrol")
        visitor = self.service.channel_projection("coord", "visitor")
        self.assertTrue(patrol["areas"][0]["granted"])
        self.assertFalse(visitor["areas"][0]["granted"])


if __name__ == "__main__":
    unittest.main()
