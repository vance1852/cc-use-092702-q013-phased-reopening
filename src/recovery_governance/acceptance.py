"""灾后分区域分阶段恢复治理的离线验收。

覆盖题目情景：
- 巡护道路先行恢复；游客步道依赖边坡复核；科研采样受烟气影响；
  住宿区卡在供水与通信检查。
- 前置不齐时激活被拒；指令带生效期；重复执行回放、证据失效立即收紧。
- 四类接口读取同一阶段投影；公众只见开放状态；审计可重建决定依据。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import RecoveryGovernanceService


def _evidence(evidence_id: str, version: int, title: str, evidence_type: str, payload: dict) -> dict:
    return {
        "evidence_id": evidence_id,
        "version": version,
        "title": title,
        "evidence_type": evidence_type,
        "payload": payload,
    }


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 9, 29, 1, 0, tzinfo=timezone.utc))
    service = RecoveryGovernanceService(connection, clock)
    service.bootstrap()

    # 四个受影响区域
    areas = [
        ("patrol-road-n2", "北部巡护道路 2 号线", "patrol"),
        ("visitor-trail-east", "东坡游客步道", "visitor"),
        ("research-plot-7", "7 号科研采样样地", "research"),
        ("lodge-west", "西坡住宿区", "lodging"),
    ]
    for area_id, name, scope in areas:
        service.register_area("coord", {"area_id": area_id, "name": name, "scope": scope})

    # 证据版本
    service.submit_evidence("rev", _evidence("road-struct-n2", 1, "巡护道路结构安全检测", "structural",
        {"sections": 12, "load_rating": "限载 10t", "cleared": True}))
    service.submit_evidence("rev", _evidence("slope-east", 1, "东坡边坡复核（初查，存疑）", "slope",
        {"slopes": 6, "netting_ok": 4, "rockfall_risk": "medium"}))
    service.submit_evidence("rev", _evidence("slope-east", 2, "东坡边坡复核（加固后）", "slope",
        {"slopes": 6, "netting_ok": 6, "rockfall_risk": "low"}))
    service.submit_evidence("rev", _evidence("air-quality-plot7", 1, "样地烟气 PM2.5 监测", "air",
        {"pm25_ugm3": 168, "threshold": 75}))
    service.submit_evidence("rev", _evidence("water-west", 1, "西坡供水水质检测", "water",
        {"coliform": "未检出", "turbidity_ntu": 1.2}))
    service.submit_evidence("rev", _evidence("comm-west", 1, "西坡通信链路检查", "comm",
        {"base_stations_online": 1, "total_stations": 3}))

    # 游客步道：两阶段（受控开放 -> 开放）
    visitor_plan = {
        "plan_id": "plan-visitor-east",
        "area_id": "visitor-trail-east",
        "title": "东坡游客步道分阶段恢复",
        "stages": [
            {
                "stage_id": "stg-visitor-limited",
                "ordinal": 0,
                "name": "巡检限流开放",
                "grants_state": "limited",
                "validity_hours": 48,
                "channels": ["visitor", "patrol"],
                "reviewer_role": "reviewer",
                "evidence": [
                    {"evidence_id": "slope-east", "version": 2,
                     "minimum_items": ["六处边坡防护网完好", "落石风险降至低"]},
                ],
                "requires_stages": [],
            },
            {
                "stage_id": "stg-visitor-open",
                "ordinal": 1,
                "name": "全面开放",
                "grants_state": "open",
                "validity_hours": 72,
                "channels": ["visitor", "operations"],
                "reviewer_role": "coordinator",
                "evidence": [
                    {"evidence_id": "slope-east", "version": 2,
                     "minimum_items": ["六处边坡防护网完好", "落石风险降至低"]},
                ],
                "requires_stages": [0],
            },
        ],
    }
    service.create_plan("coord", visitor_plan)
    service.issue_plan("coord", "plan-visitor-east", 1)

    # 巡护道路先恢复：证据齐、复核签字后激活
    patrol_plan = {
        "plan_id": "plan-patrol-n2",
        "area_id": "patrol-road-n2",
        "title": "北部巡护道路 2 号线恢复",
        "stages": [
            {
                "stage_id": "stg-patrol-resume",
                "ordinal": 0,
                "name": "巡护通行恢复",
                "grants_state": "limited",
                "validity_hours": 72,
                "channels": ["patrol"],
                "reviewer_role": "reviewer",
                "evidence": [
                    {"evidence_id": "road-struct-n2", "version": 1,
                     "minimum_items": ["12 个区段结构安全", "限载标识就位"]},
                ],
            },
        ],
    }
    service.create_plan("coord", patrol_plan)
    service.issue_plan("coord", "plan-patrol-n2", 1)
    service.submit_review("rev", "stg-patrol-resume", "road-struct-n2", 1,
                          ["12 个区段结构安全", "限载标识就位"], "全线检测通过")
    patrol_activation = service.activate_stage(
        "coord", "stg-patrol-resume", "idem-patrol-001", "巡护道路先行恢复")
    replay = service.activate_stage(
        "coord", "stg-patrol-resume", "idem-patrol-001", "巡护道路先行恢复")

    # 游客阶段：证据未复核，前置不允许放行
    blocked_before_review = None
    try:
        service.activate_stage("coord", "stg-visitor-limited", "idem-visitor-001")
    except Exception as exc:  # noqa: BLE001 - 验收脚本需捕获拒绝原因
        blocked_before_review = str(exc)
    service.submit_review("rev", "stg-visitor-limited", "slope-east", 2,
                          ["六处边坡防护网完好", "落石风险降至低"], "加固完成后复核通过")
    visitor_activation = service.activate_stage(
        "coord", "stg-visitor-limited", "idem-visitor-001", "限流开放")

    # 经营接口在受限阶段读取同一投影：仍未放行
    operations_view = service.channel_projection("coord", "operations")
    # 巡护队按生效指令执行现场通行
    patrol_exec = service.record_execution(
        "coord", patrol_activation["activation_id"], "patrol",
        {"team": "巡护二队", "vehicle": "豫A-002", "km": 18.4})
    # 游客通道不能借巡护指令提前放行
    cross_channel_blocked = None
    try:
        service.record_execution(
            "coord", patrol_activation["activation_id"], "visitor", {"team": "违规带客"})
    except Exception as exc:  # noqa: BLE001
        cross_channel_blocked = str(exc)

    # 第二阶段：前置阶段失效前不得跳级（此处前置有效，但需协调员复核）
    stage2_blocked = None
    try:
        service.activate_stage("coord", "stg-visitor-open", "idem-visitor-002")
    except Exception as exc:  # noqa: BLE001
        stage2_blocked = str(exc)

    # 证据失效：边坡新版本被撤回 → 游客阶段立即收紧，巡护不受影响
    invalidation = service.invalidate_evidence(
        "rev", "slope-east", 2, "加固段复查发现新裂缝")
    visitor_after_lapse = service.projection("coord", "visitor-trail-east")
    patrol_after_lapse = service.projection("coord", "patrol-road-n2")

    # 到期：时钟推进超过巡护指令生效期
    clock.advance(hours=80)
    expiry_sweep = service.sweep()
    public = service.public_status()

    # 审计重建：游客指令的依据、收紧与现场执行；巡护指令重建现场实际执行
    record = service.decision_record("audit", visitor_activation["activation_id"])
    patrol_record = service.decision_record("audit", patrol_activation["activation_id"])
    chain = service.audit_chain("audit")

    connection.close()
    return {
        "status": "ok",
        "patrol_activation": patrol_activation,
        "patrol_replay_replayed": replay["replayed"],
        "blocked_before_review": blocked_before_review,
        "visitor_activation": visitor_activation,
        "operations_granted_areas": [a["area_id"] for a in operations_view["areas"] if a["granted"]],
        "patrol_execution": patrol_exec,
        "cross_channel_blocked": cross_channel_blocked,
        "stage2_blocked_without_coordinator_review": stage2_blocked,
        "evidence_invalidation": invalidation,
        "visitor_state_after_lapse": visitor_after_lapse["public_state"],
        "visitor_channel_grants_after_lapse": {
            name: data["granted"] for name, data in visitor_after_lapse["channels"].items()
        },
        "patrol_state_after_evidence_lapse": patrol_after_lapse["public_state"],
        "expiry_sweep": expiry_sweep,
        "public_status": public,
        "decision_record_states": [event["event_type"] for event in record["timeline"]],
        "decision_evidence": [
            {"evidence": item["evidence"], "current_state": item["current_state"]}
            for item in record["evidence_basis"]
        ],
        "field_executions_seen": len(record["field_executions"]),
        "patrol_record": {
            "final_state": patrol_record["state"],
            "field_executions": len(patrol_record["field_executions"]),
            "timeline": [event["event_type"] for event in patrol_record["timeline"]],
        },
        "audit_chain": chain,
        "workspace": workspace.name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行灾后分阶段恢复治理服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
