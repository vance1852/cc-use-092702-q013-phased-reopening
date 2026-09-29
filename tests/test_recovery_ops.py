from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone

from recovery_ops.api import JsonApplication
from recovery_ops.clock import FrozenClock
from recovery_ops.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from recovery_ops.service import RecoveryService


def plan_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "plan_id": "plan-north",
        "zone_id": "north-trails",
        "title": "北线灾后分阶段恢复",
        "phases": [
            {
                "phase_key": "P1",
                "sequence": 1,
                "name": "巡护道路抢通",
                "scopes": {
                    "patrol": "limited",
                    "research": "closed",
                    "visitor": "closed",
                    "operations": "closed",
                },
                "evidence_requirements": [{"kind": "slope-assessment", "version": "rev-2"}],
                "reviewer_role": "reviewer",
                "minimum_pass_items": ["debris-cleared", "drainage-checked"],
            },
            {
                "phase_key": "P2",
                "sequence": 2,
                "name": "科研采样恢复",
                "scopes": {
                    "patrol": "limited",
                    "research": "limited",
                    "visitor": "closed",
                    "operations": "closed",
                },
                "evidence_requirements": [
                    {"kind": "slope-assessment", "version": "rev-2"},
                    {"kind": "smoke-monitoring", "version": "rev-1"},
                ],
                "reviewer_role": "reviewer",
                "minimum_pass_items": ["sampling-route-marked"],
            },
        ],
    }
    payload.update(overrides)
    return payload


class RecoveryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc))
        self.service = RecoveryService(self.connection, self.clock)
        for user_id, role in (
            ("coord", "coordinator"),
            ("coord-2", "coordinator"),
            ("survey", "surveyor"),
            ("review", "reviewer"),
            ("audit", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.create_plan("coord", plan_payload())
        self.service.activate_plan("coord", "plan-north", 1)

    def tearDown(self) -> None:
        self.connection.close()

    def prepare_evidence_and_reviews(self) -> None:
        self.service.submit_evidence(
            "survey",
            {
                "evidence_id": "ev-slope-r2",
                "zone_id": "north-trails",
                "kind": "slope-assessment",
                "version": "rev-2",
                "content_sha256": "a" * 64,
            },
        )
        self.service.submit_evidence(
            "survey",
            {
                "evidence_id": "ev-smoke-r1",
                "zone_id": "north-trails",
                "kind": "smoke-monitoring",
                "version": "rev-1",
                "content_sha256": "b" * 64,
            },
        )
        for phase_key in ("P1", "P2"):
            self.service.review_phase("review", "plan-north", phase_key, "approved", "复核通过")

    def issue(self, phase_key: str, key: str, start: str, until: str) -> dict[str, object]:
        return self.service.issue_directive(
            "coord",
            {
                "plan_id": "plan-north",
                "phase_key": phase_key,
                "effective_from": start,
                "effective_until": until,
                "idempotency_key": key,
            },
        )

    def confirm_p1_items(self) -> None:
        self.service.confirm_pass_item("survey", "plan-north", "P1", "debris-cleared", "清理完成")
        self.service.confirm_pass_item("survey", "plan-north", "P1", "drainage-checked", "排水完成")

    # ------------------------------------------------------------------
    # 计划编排与校验
    # ------------------------------------------------------------------

    def test_plan_validation_rejects_bad_scopes(self) -> None:
        payload = plan_payload(plan_id="plan-bad")
        payload["phases"][0]["scopes"] = {"patrol": "limited"}  # type: ignore[index]
        with self.assertRaises(ValidationFailed):
            self.service.create_plan("coord", payload)
        payload = plan_payload(plan_id="plan-bad-2")
        payload["phases"][0]["scopes"]["visitor"] = "half"  # type: ignore[index]
        with self.assertRaises(ValidationFailed):
            self.service.create_plan("coord", payload)

    def test_plan_validation_rejects_duplicate_keys_and_unknown_reviewer(self) -> None:
        payload = plan_payload(plan_id="plan-dup")
        payload["phases"][1]["phase_key"] = "P1"  # type: ignore[index]
        with self.assertRaises(ValidationFailed):
            self.service.create_plan("coord", payload)
        payload = plan_payload(plan_id="plan-dup-seq")
        payload["phases"][1]["sequence"] = 1  # type: ignore[index]
        with self.assertRaises(ValidationFailed):
            self.service.create_plan("coord", payload)
        payload = plan_payload(plan_id="plan-role")
        payload["phases"][0]["reviewer_role"] = "surveyor"  # type: ignore[index]
        with self.assertRaises(ValidationFailed):
            self.service.create_plan("coord", payload)

    def test_single_active_plan_per_zone(self) -> None:
        self.service.create_plan("coord", plan_payload(plan_id="plan-second"))
        with self.assertRaises(Conflict):
            self.service.activate_plan("coord", "plan-second", 1)
        with self.assertRaises(InvalidState):
            self.service.activate_plan("coord", "plan-north", 1)

    def test_review_responsibility_is_enforced(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.review_phase("survey", "plan-north", "P1", "approved", "")
        payload = plan_payload(plan_id="plan-coord-review", zone_id="south-trails")
        payload["phases"][0]["reviewer_role"] = "coordinator"  # type: ignore[index]
        self.service.create_plan("coord", payload)
        with self.assertRaises(Forbidden):
            self.service.review_phase("review", "plan-coord-review", "P1", "approved", "")
        with self.assertRaises(Forbidden):
            self.service.review_phase("coord", "plan-coord-review", "P1", "approved", "")
        reviewed = self.service.review_phase("coord-2", "plan-coord-review", "P1", "approved", "联席会复核")
        self.assertEqual(reviewed["decision"], "approved")

    # ------------------------------------------------------------------
    # 前置条件：复核、证据版本、前置阶段最低通过项
    # ------------------------------------------------------------------

    def test_directive_blocked_until_review_and_evidence_ready(self) -> None:
        with self.assertRaises(InvalidState) as caught:
            self.issue("P1", "k-1", "2026-09-29T08:00:00Z", "2026-09-30T08:00:00Z")
        self.assertIn("复核", str(caught.exception))
        self.service.review_phase("review", "plan-north", "P1", "approved", "")
        with self.assertRaises(InvalidState) as caught:
            self.issue("P1", "k-1", "2026-09-29T08:00:00Z", "2026-09-30T08:00:00Z")
        self.assertIn("slope-assessment@rev-2", str(caught.exception))
        self.assertIn("不得扩大任何业务范围", str(caught.exception))

    def test_directive_blocked_until_prior_pass_items_confirmed(self) -> None:
        self.prepare_evidence_and_reviews()
        self.issue("P1", "k-p1", "2026-09-29T08:00:00Z", "2026-10-06T08:00:00Z")
        with self.assertRaises(InvalidState) as caught:
            self.issue("P2", "k-p2", "2026-09-29T08:00:00Z", "2026-10-06T08:00:00Z")
        self.assertIn("debris-cleared", str(caught.exception))
        self.service.confirm_pass_item("survey", "plan-north", "P1", "debris-cleared", "")
        with self.assertRaises(InvalidState):
            self.issue("P2", "k-p2", "2026-09-29T08:00:00Z", "2026-10-06T08:00:00Z")
        self.service.confirm_pass_item("survey", "plan-north", "P1", "drainage-checked", "")
        issued = self.issue("P2", "k-p2", "2026-09-29T08:00:00Z", "2026-10-06T08:00:00Z")
        self.assertEqual(issued["state"], "active")

    def test_directive_requires_effective_window_and_valid_times(self) -> None:
        self.prepare_evidence_and_reviews()
        with self.assertRaises(ValidationFailed):
            self.issue("P1", "k-bad", "2026-09-30T08:00:00Z", "2026-09-29T08:00:00Z")
        issued = self.issue("P1", "k-future", "2026-09-29T10:00:00Z", "2026-10-06T08:00:00Z")
        self.assertFalse(issued["replayed"])
        projection = self.service.zone_projection("coord", "north-trails")
        self.assertEqual(projection["scopes"]["patrol"], "closed")
        self.clock.advance(hours=3)
        projection = self.service.zone_projection("coord", "north-trails")
        self.assertEqual(projection["scopes"]["patrol"], "limited")

    # ------------------------------------------------------------------
    # 同一阶段投影与公众视图
    # ------------------------------------------------------------------

    def test_interfaces_read_the_same_projection(self) -> None:
        self.prepare_evidence_and_reviews()
        self.issue("P1", "k-p1", "2026-09-29T08:00:00Z", "2026-10-06T08:00:00Z")
        projection = self.service.zone_projection("coord", "north-trails")
        for line in ("patrol", "research", "visitor", "operations"):
            status = self.service.interface_status("coord", "north-trails", line)
            self.assertEqual(status["status"], projection["scopes"][line])
            self.assertEqual(status["directive_id"], projection["directive_id"])
        with self.assertRaises(ValidationFailed):
            self.service.interface_status("coord", "north-trails", "vip")

    def test_public_status_is_minimal_and_follows_projection(self) -> None:
        self.assertEqual(
            self.service.public_status()["zones"], [{"zone_id": "north-trails", "status": "closed"}]
        )
        self.prepare_evidence_and_reviews()
        self.issue("P1", "k-p1", "2026-09-29T08:00:00Z", "2026-10-06T08:00:00Z")
        self.assertEqual(
            self.service.public_status()["zones"], [{"zone_id": "north-trails", "status": "partial"}]
        )
        payload = plan_payload(plan_id="plan-lake", zone_id="lake-side")
        payload["phases"] = [
            {
                "phase_key": "F1",
                "sequence": 1,
                "name": "全面对外开放",
                "scopes": {
                    "patrol": "full",
                    "research": "full",
                    "visitor": "full",
                    "operations": "full",
                },
                "evidence_requirements": [],
                "reviewer_role": "reviewer",
                "minimum_pass_items": [],
            }
        ]
        self.service.create_plan("coord", payload)
        self.service.activate_plan("coord", "plan-lake", 1)
        self.service.review_phase("review", "plan-lake", "F1", "approved", "")
        self.service.issue_directive(
            "coord",
            {
                "plan_id": "plan-lake",
                "phase_key": "F1",
                "effective_from": "2026-09-29T08:00:00Z",
                "effective_until": "2026-10-06T08:00:00Z",
                "idempotency_key": "k-lake",
            },
        )
        zones = {zone["zone_id"]: zone["status"] for zone in self.service.public_status()["zones"]}
        self.assertEqual(zones, {"lake-side": "open", "north-trails": "partial"})

    # ------------------------------------------------------------------
    # 重复执行、撤回、到期、证据失效
    # ------------------------------------------------------------------

    def test_repeated_execution_replays_and_leaves_history(self) -> None:
        self.prepare_evidence_and_reviews()
        first = self.issue("P1", "k-p1", "2026-09-29T08:00:00Z", "2026-10-06T08:00:00Z")
        replay = self.issue("P1", "k-p1", "2026-09-29T08:00:00Z", "2026-10-06T08:00:00Z")
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["directive_id"], replay["directive_id"])
        count = self.connection.execute(
            "SELECT count(*) FROM recovery_directives"
        ).fetchone()[0]
        self.assertEqual(count, 1)
        with self.assertRaises(Conflict):
            self.issue("P1", "k-p1", "2026-09-29T08:00:00Z", "2026-10-07T08:00:00Z")
        events = self.connection.execute(
            "SELECT event_type FROM recovery_audit_events WHERE entity_type='directive' "
            "AND entity_id=? ORDER BY event_id",
            (str(first["directive_id"]),),
        ).fetchall()
        self.assertEqual([row[0] for row in events], ["directive.issued", "directive.replayed"])

    def test_expiry_tightens_immediately_and_records_history(self) -> None:
        self.prepare_evidence_and_reviews()
        issued = self.issue("P1", "k-p1", "2026-09-29T08:00:00Z", "2026-09-29T09:00:00Z")
        self.clock.advance(hours=2)
        projection = self.service.zone_projection("coord", "north-trails")
        self.assertEqual(projection["scopes"]["patrol"], "closed")
        self.assertIsNone(projection["directive_id"])
        events = self.connection.execute(
            "SELECT event_type FROM recovery_audit_events WHERE entity_type='directive' "
            "AND entity_id=? ORDER BY event_id",
            (str(issued["directive_id"]),),
        ).fetchall()
        self.assertEqual([row[0] for row in events], ["directive.issued", "directive.expired"])
        self.assertEqual(self.service.expire_directives("coord")["expired"], [])
        with self.assertRaises(InvalidState):
            self.service.withdraw_directive("coord", issued["directive_id"], "已到期")

    def test_explicit_sweep_records_expiry(self) -> None:
        self.prepare_evidence_and_reviews()
        issued = self.issue("P1", "k-p1", "2026-09-29T08:00:00Z", "2026-09-29T09:00:00Z")
        self.clock.advance(hours=2)
        swept = self.service.expire_directives("coord")
        self.assertEqual(swept["expired"], [issued["directive_id"]])

    def test_withdraw_tightens_immediately(self) -> None:
        self.prepare_evidence_and_reviews()
        issued = self.issue("P1", "k-p1", "2026-09-29T08:00:00Z", "2026-10-06T08:00:00Z")
        withdrawn = self.service.withdraw_directive("coord", issued["directive_id"], "余震风险")
        self.assertEqual(withdrawn["state"], "withdrawn")
        projection = self.service.zone_projection("coord", "north-trails")
        self.assertEqual(projection["scopes"]["patrol"], "closed")
        with self.assertRaises(InvalidState):
            self.service.withdraw_directive("coord", issued["directive_id"], "重复撤回")

    def test_evidence_invalidation_tightens_and_blocks_reissue(self) -> None:
        self.prepare_evidence_and_reviews()
        self.issue("P1", "k-p1", "2026-09-29T08:00:00Z", "2026-10-06T08:00:00Z")
        self.confirm_p1_items()
        p2 = self.issue("P2", "k-p2", "2026-09-29T08:00:00Z", "2026-10-06T08:00:00Z")
        projection = self.service.zone_projection("coord", "north-trails")
        self.assertEqual(projection["scopes"]["research"], "limited")
        result = self.service.invalidate_evidence("coord", "ev-smoke-r1", "数据溯源异常")
        self.assertEqual(result["tightened_directives"], [p2["directive_id"]])
        projection = self.service.zone_projection("coord", "north-trails")
        self.assertEqual(projection["scopes"]["research"], "closed")
        self.assertEqual(projection["scopes"]["patrol"], "limited")
        with self.assertRaises(InvalidState) as caught:
            self.issue("P2", "k-p2-new", "2026-09-29T08:00:00Z", "2026-10-06T08:00:00Z")
        self.assertIn("smoke-monitoring@rev-1", str(caught.exception))
        with self.assertRaises(InvalidState):
            self.service.invalidate_evidence("coord", "ev-smoke-r1", "再次失效")

    def test_retire_plan_tightens_all_directives(self) -> None:
        self.prepare_evidence_and_reviews()
        self.issue("P1", "k-p1", "2026-09-29T08:00:00Z", "2026-10-06T08:00:00Z")
        retired = self.service.retire_plan("coord", "plan-north", "整体转为长期封控")
        self.assertEqual(len(retired["tightened_directives"]), 1)
        projection = self.service.zone_projection("coord", "north-trails")
        self.assertIsNone(projection["plan_id"])
        self.assertEqual(self.service.public_status()["zones"], [])

    # ------------------------------------------------------------------
    # 现场执行回执
    # ------------------------------------------------------------------

    def test_field_confirmation_requires_effective_directive(self) -> None:
        self.prepare_evidence_and_reviews()
        with self.assertRaises(InvalidState):
            self.service.confirm_pass_item("survey", "plan-north", "P1", "debris-cleared", "")
        self.issue("P1", "k-p1", "2026-09-29T08:00:00Z", "2026-09-29T09:00:00Z")
        confirmed = self.service.confirm_pass_item("survey", "plan-north", "P1", "debris-cleared", "完成")
        self.assertEqual(confirmed["item_key"], "debris-cleared")
        with self.assertRaises(Conflict):
            self.service.confirm_pass_item("survey", "plan-north", "P1", "debris-cleared", "")
        with self.assertRaises(ValidationFailed):
            self.service.confirm_pass_item("survey", "plan-north", "P1", "unknown-item", "")
        self.clock.advance(hours=2)
        with self.assertRaises(InvalidState):
            self.service.confirm_pass_item("survey", "plan-north", "P1", "drainage-checked", "")

    # ------------------------------------------------------------------
    # 审计重建
    # ------------------------------------------------------------------

    def test_auditor_reconstructs_basis_and_field_execution(self) -> None:
        self.prepare_evidence_and_reviews()
        self.issue("P1", "k-p1", "2026-09-29T08:00:00Z", "2026-10-06T08:00:00Z")
        self.confirm_p1_items()
        p2 = self.issue("P2", "k-p2", "2026-09-29T08:00:00Z", "2026-10-06T08:00:00Z")
        self.service.invalidate_evidence("coord", "ev-smoke-r1", "数据溯源异常")
        basis = self.service.decision_basis("audit", p2["directive_id"])
        self.assertEqual(basis["directive"]["state"], "tightened")
        self.assertEqual(
            [item["evidence_id"] for item in basis["evidence"]],
            ["ev-slope-r2", "ev-smoke-r1"],
        )
        states = {item["evidence_id"]: item["current_state"] for item in basis["evidence"]}
        self.assertEqual(states, {"ev-slope-r2": "valid", "ev-smoke-r1": "invalidated"})
        self.assertEqual(basis["review"]["reviewer_id"], "review")
        self.assertEqual(basis["directive"]["basis"]["review"]["reviewer_role"], "reviewer")
        self.assertEqual(
            basis["directive"]["basis"]["prior_pass_items_confirmed"],
            ["debris-cleared", "drainage-checked"],
        )
        self.assertEqual(basis["field_execution"]["outstanding"], ["sampling-route-marked"])
        self.assertEqual(
            [event["event_type"] for event in basis["events"]],
            ["directive.issued", "directive.tightened"],
        )
        with self.assertRaises(Forbidden):
            self.service.decision_basis("survey", p2["directive_id"])

    def test_audit_chain_detects_tampering(self) -> None:
        self.prepare_evidence_and_reviews()
        self.issue("P1", "k-p1", "2026-09-29T08:00:00Z", "2026-10-06T08:00:00Z")
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE recovery_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])

    def test_permissions_are_role_scoped(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.create_plan("survey", plan_payload(plan_id="plan-x"))
        with self.assertRaises(Forbidden):
            self.service.submit_evidence("coord", {"evidence_id": "e", "zone_id": "z"})
        with self.assertRaises(Forbidden):
            self.service.issue_directive("survey", {})
        with self.assertRaises(NotFound):
            self.service.zone_projection("nobody", "north-trails")


class RecoveryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        clock = FrozenClock(datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc))
        self.service = RecoveryService(self.connection, clock)
        self.app = JsonApplication(self.service)

    def tearDown(self) -> None:
        self.connection.close()

    def post(self, path: str, payload: dict[str, object], actor: str = "coord"):
        return self.app.handle(
            "POST", path, {"X-Actor-Id": actor}, json.dumps(payload).encode("utf-8")
        )

    def seed_via_api(self) -> None:
        for user_id, role in (
            ("coord", "coordinator"),
            ("survey", "surveyor"),
            ("review", "reviewer"),
            ("audit", "auditor"),
        ):
            response = self.post("/users", {"user_id": user_id, "display_name": user_id, "role": role})
            self.assertEqual(response.status, 201)
        response = self.post("/plans", plan_payload())
        self.assertEqual(response.status, 201)
        response = self.post("/plans/plan-north/activate", {"expected_revision": 1})
        self.assertEqual(response.status, 200)
        for evidence_id, kind, version, sha in (
            ("ev-slope-r2", "slope-assessment", "rev-2", "a" * 64),
            ("ev-smoke-r1", "smoke-monitoring", "rev-1", "b" * 64),
        ):
            response = self.post(
                "/evidence",
                {
                    "evidence_id": evidence_id,
                    "zone_id": "north-trails",
                    "kind": kind,
                    "version": version,
                    "content_sha256": sha,
                },
                actor="survey",
            )
            self.assertEqual(response.status, 201)
        for phase_key in ("P1", "P2"):
            response = self.post(
                f"/plans/plan-north/phases/{phase_key}/reviews",
                {"decision": "approved", "note": "复核通过"},
                actor="review",
            )
            self.assertEqual(response.status, 201)

    def test_end_to_end_over_http(self) -> None:
        self.seed_via_api()
        directive = {
            "plan_id": "plan-north",
            "phase_key": "P1",
            "effective_from": "2026-09-29T08:00:00Z",
            "effective_until": "2026-10-06T08:00:00Z",
            "idempotency_key": "k-p1",
        }
        response = self.post("/directives", directive)
        self.assertEqual(response.status, 201)
        directive_id = response.body["directive_id"]
        replay = self.post("/directives", directive)
        self.assertEqual(replay.status, 201)
        self.assertTrue(replay.body["replayed"])
        response = self.app.handle("GET", "/zones/north-trails/projection", {"X-Actor-Id": "audit"})
        self.assertEqual(response.body["scopes"]["patrol"], "limited")
        response = self.app.handle(
            "GET", "/zones/north-trails/interfaces/visitor", {"X-Actor-Id": "audit"}
        )
        self.assertEqual(response.body["status"], "closed")
        response = self.app.handle("GET", "/public/status")
        self.assertEqual(response.body["zones"], [{"zone_id": "north-trails", "status": "partial"}])
        response = self.post(f"/directives/{directive_id}/withdraw", {"reason": "余震风险"})
        self.assertEqual(response.body["state"], "withdrawn")
        response = self.app.handle("GET", "/public/status")
        self.assertEqual(response.body["zones"], [{"zone_id": "north-trails", "status": "closed"}])
        response = self.app.handle("GET", f"/directives/{directive_id}/basis", {"X-Actor-Id": "audit"})
        self.assertEqual(response.status, 200)
        self.assertEqual(
            [event["event_type"] for event in response.body["events"]],
            ["directive.issued", "directive.replayed", "directive.withdrawn"],
        )
        response = self.app.handle("GET", "/audit/chain", {"X-Actor-Id": "audit"})
        self.assertTrue(response.body["valid"])

    def test_http_boundaries(self) -> None:
        self.assertEqual(self.app.handle("GET", "/health").status, 200)
        response = self.app.handle("GET", "/zones/north-trails/projection")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")
        response = self.app.handle("POST", "/users", body=b"not-json")
        self.assertEqual(response.status, 422)
        response = self.app.handle("GET", "/unknown", {"X-Actor-Id": "coord"})
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "route_not_found")
        self.seed_via_api()
        response = self.post("/directives", {"plan_id": "plan-north", "phase_key": "P1"}, actor="survey")
        self.assertEqual(response.status, 403)
        self.assertEqual(response.body["error"]["code"], "forbidden")


if __name__ == "__main__":
    unittest.main()
