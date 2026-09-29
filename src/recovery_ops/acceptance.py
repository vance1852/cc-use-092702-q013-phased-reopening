"""贯通计划编排、证据版本、复核、恢复指令、投影与审计的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import RecoveryService


def run(workspace: Path | None = None) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc))
    service = RecoveryService(connection, clock)
    service.create_user("coord-1", "联席会管理人员", "coordinator")
    service.create_user("survey-1", "现场调查员", "surveyor")
    service.create_user("review-1", "复核工程师", "reviewer")
    service.create_user("audit-1", "审计人员", "auditor")

    service.create_plan(
        "coord-1",
        {
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
                    "evidence_requirements": [
                        {"kind": "slope-assessment", "version": "rev-2"}
                    ],
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
                {
                    "phase_key": "P3",
                    "sequence": 3,
                    "name": "游客步道与住宿开放",
                    "scopes": {
                        "patrol": "full",
                        "research": "limited",
                        "visitor": "limited",
                        "operations": "limited",
                    },
                    "evidence_requirements": [
                        {"kind": "smoke-monitoring", "version": "rev-1"},
                        {"kind": "water-comms-check", "version": "rev-1"},
                    ],
                    "reviewer_role": "reviewer",
                    "minimum_pass_items": ["visitor-signage", "lodging-safety-drill"],
                },
            ],
        },
    )
    service.activate_plan("coord-1", "plan-north", 1)
    service.submit_evidence(
        "survey-1",
        {
            "evidence_id": "ev-slope-r2",
            "zone_id": "north-trails",
            "kind": "slope-assessment",
            "version": "rev-2",
            "content_sha256": "a" * 64,
            "note": "边坡复核第二版",
        },
    )
    service.submit_evidence(
        "survey-1",
        {
            "evidence_id": "ev-smoke-r1",
            "zone_id": "north-trails",
            "kind": "smoke-monitoring",
            "version": "rev-1",
            "content_sha256": "b" * 64,
            "note": "烟气监测第一版",
        },
    )
    service.submit_evidence(
        "survey-1",
        {
            "evidence_id": "ev-utility-r1",
            "zone_id": "north-trails",
            "kind": "water-comms-check",
            "version": "rev-1",
            "content_sha256": "c" * 64,
            "note": "供水与通信检查",
        },
    )
    for phase_key in ("P1", "P2", "P3"):
        service.review_phase("review-1", "plan-north", phase_key, "approved", "复核通过")

    clock.advance(hours=1)  # 09:00
    directive_p1 = {
        "plan_id": "plan-north",
        "phase_key": "P1",
        "effective_from": "2026-09-29T09:00:00Z",
        "effective_until": "2026-10-06T09:00:00Z",
        "idempotency_key": "dir-p1-001",
    }
    first = service.issue_directive("coord-1", directive_p1)
    replay = service.issue_directive("coord-1", directive_p1)
    projection_p1 = service.zone_projection("coord-1", "north-trails")
    interfaces_p1 = {
        line: service.interface_status("coord-1", "north-trails", line)["status"]
        for line in ("patrol", "research", "visitor", "operations")
    }
    service.confirm_pass_item("survey-1", "plan-north", "P1", "debris-cleared", "塌方清理完成")
    service.confirm_pass_item("survey-1", "plan-north", "P1", "drainage-checked", "排水检查完成")

    clock.advance(hours=1)  # 10:00
    directive_p2 = service.issue_directive(
        "coord-1",
        {
            "plan_id": "plan-north",
            "phase_key": "P2",
            "effective_from": "2026-09-29T10:00:00Z",
            "effective_until": "2026-10-06T09:00:00Z",
            "idempotency_key": "dir-p2-001",
        },
    )
    projection_p2 = service.zone_projection("coord-1", "north-trails")
    public_partial = service.public_status()

    invalidated = service.invalidate_evidence("coord-1", "ev-smoke-r1", "烟气监测数据溯源异常")
    projection_tightened = service.zone_projection("coord-1", "north-trails")

    service.withdraw_directive("coord-1", first["directive_id"], "余震风险复核期间暂停")
    projection_withdrawn = service.zone_projection("coord-1", "north-trails")
    public_closed = service.public_status()

    clock.advance(hours=1)  # 11:00
    renewed = service.issue_directive(
        "coord-1",
        {
            "plan_id": "plan-north",
            "phase_key": "P1",
            "effective_from": "2026-09-29T11:00:00Z",
            "effective_until": "2026-09-29T12:00:00Z",
            "idempotency_key": "dir-p1-002",
        },
    )
    clock.advance(hours=2)  # 13:00，续发指令已过生效期
    projection_expired = service.zone_projection("coord-1", "north-trails")
    sweep = service.expire_directives("coord-1")

    basis = service.decision_basis("audit-1", directive_p2["directive_id"])
    chain = service.audit_chain("audit-1")
    connection.close()
    return {
        "status": "ok",
        "workspace": None if workspace is None else workspace.resolve().name,
        "plan_id": "plan-north",
        "directives": {
            "p1": first["directive_id"],
            "p2": directive_p2["directive_id"],
            "p1_renewed": renewed["directive_id"],
        },
        "replayed": replay["replayed"],
        "projection_p1": projection_p1["scopes"],
        "interfaces_p1": interfaces_p1,
        "projection_p2": projection_p2["scopes"],
        "public_partial": public_partial["zones"],
        "tightened_by_evidence": invalidated["tightened_directives"],
        "projection_tightened": projection_tightened["scopes"],
        "projection_withdrawn": projection_withdrawn["scopes"],
        "public_closed": public_closed["zones"],
        "projection_expired": projection_expired["scopes"],
        "sweep_after_lazy_expiry": sweep["expired"],
        "basis_events": [event["event_type"] for event in basis["events"]],
        "basis_evidence_state": [item["current_state"] for item in basis["evidence"]],
        "audit_valid": chain["valid"],
        "audit_events": chain["events"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行灾后分阶段恢复编排离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
