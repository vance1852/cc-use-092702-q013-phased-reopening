"""恢复治理领域输入契约。"""

from __future__ import annotations

import re
from typing import Any, Mapping

from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")

AREA_SCOPES = {"patrol", "visitor", "research", "lodging"}
SCOPE_LABELS = {
    "patrol": "巡护道路",
    "visitor": "游客步道",
    "research": "科研样地",
    "lodging": "住宿区",
}
PUBLIC_STATES = {"closed", "limited", "open"}
PUBLIC_STATE_LABELS = {"closed": "关闭", "limited": "受控开放", "open": "开放"}
CHANNELS = {"patrol", "research", "visitor", "operations"}
REVIEWER_ROLES = {"reviewer", "coordinator"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field} 必须是正整数")
    return value


def non_negative_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationFailed(f"{field} 必须是非负整数")
    return value


def affected_area_from_dict(raw: Mapping[str, Any]) -> dict[str, str]:
    scope = required_text(raw.get("scope"), "scope", 16)
    if scope not in AREA_SCOPES:
        raise ValidationFailed("scope 必须是 patrol、visitor、research 或 lodging")
    return {
        "area_id": identifier(raw.get("area_id"), "area_id"),
        "name": required_text(raw.get("name"), "name"),
        "scope": scope,
    }


def evidence_from_dict(raw: Mapping[str, Any]) -> dict[str, Any]:
    """证据版本登记载荷。

    version 必须显式给出且在同一 evidence_id 下严格递增；payload 为证据正文
    （监测数值、检查项、签发人等），规范化后计算内容摘要。
    """

    evidence_id = identifier(raw.get("evidence_id"), "evidence_id")
    version = positive_integer(raw.get("version"), "version")
    title = required_text(raw.get("title"), "title")
    evidence_type = required_text(raw.get("evidence_type"), "evidence_type", 48)
    payload = raw.get("payload")
    if not isinstance(payload, Mapping):
        raise ValidationFailed("payload 必须是证据正文对象")
    if not payload:
        raise ValidationFailed("payload 不能为空")
    return {
        "evidence_id": evidence_id,
        "version": version,
        "title": title,
        "evidence_type": evidence_type,
        "payload": dict(payload),
    }


def _evidence_requirement(raw: Any, field: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValidationFailed(f"{field} 的每一项必须是对象")
    minimum_items = raw.get("minimum_items", [])
    if not isinstance(minimum_items, list) or not minimum_items:
        raise ValidationFailed(f"{field}.minimum_items 至少包含一个最低通过项")
    items: list[str] = []
    for item in minimum_items:
        text = required_text(item, f"{field}.minimum_items 项", 128)
        if text in items:
            raise ValidationFailed(f"{field}.minimum_items 存在重复通过项: {text}")
        items.append(text)
    return {
        "evidence_id": identifier(raw.get("evidence_id"), f"{field}.evidence_id"),
        "version": positive_integer(raw.get("version"), f"{field}.version"),
        "minimum_items": items,
    }


def stage_from_dict(raw: Mapping[str, Any]) -> dict[str, Any]:
    """阶段定义：依赖证据版本、前置阶段、复核责任与最低通过项。"""

    name = required_text(raw.get("name"), "stage.name")
    ordinal = non_negative_integer(raw.get("ordinal"), "stage.ordinal")
    grants_state = required_text(raw.get("grants_state"), "stage.grants_state", 16)
    if grants_state not in PUBLIC_STATES:
        raise ValidationFailed("stage.grants_state 必须是 closed、limited 或 open")
    validity_hours = positive_integer(raw.get("validity_hours"), "stage.validity_hours")
    reviewer_role = required_text(raw.get("reviewer_role"), "stage.reviewer_role", 16)
    if reviewer_role not in REVIEWER_ROLES:
        raise ValidationFailed("stage.reviewer_role 必须是 reviewer 或 coordinator")
    requires_stages = raw.get("requires_stages", [])
    if not isinstance(requires_stages, list):
        raise ValidationFailed("stage.requires_stages 必须是数组")
    prerequisites: list[int] = []
    for value in requires_stages:
        prerequisite = non_negative_integer(value, "stage.requires_stages 项")
        if prerequisite >= ordinal:
            raise ValidationFailed("stage.requires_stages 只能依赖序号更小的阶段")
        if prerequisite in prerequisites:
            raise ValidationFailed(f"stage.requires_stages 存在重复阶段: {prerequisite}")
        prerequisites.append(prerequisite)
    evidence_requirements = raw.get("evidence", [])
    if not isinstance(evidence_requirements, list) or not evidence_requirements:
        raise ValidationFailed("stage.evidence 至少依赖一个证据版本")
    requirements = [
        _evidence_requirement(item, "stage.evidence") for item in evidence_requirements
    ]
    seen_keys: set[tuple[str, int]] = set()
    for requirement in requirements:
        key = (requirement["evidence_id"], requirement["version"])
        if key in seen_keys:
            raise ValidationFailed(f"证据版本重复依赖: {key[0]}:{key[1]}")
        seen_keys.add(key)
    reviewer_id = raw.get("reviewer_id")
    if reviewer_id is not None:
        reviewer_id = identifier(reviewer_id, "stage.reviewer_id")
    channels_raw = raw.get("channels", [])
    if not isinstance(channels_raw, list) or not channels_raw:
        raise ValidationFailed("stage.channels 至少放行一个业务接口")
    channels: list[str] = []
    for value in channels_raw:
        channel = required_text(value, "stage.channels 项", 16)
        if channel not in CHANNELS:
            raise ValidationFailed("stage.channels 必须是 patrol、research、visitor、operations")
        if channel in channels:
            raise ValidationFailed(f"stage.channels 存在重复接口: {channel}")
        channels.append(channel)
    # 公众与经营接口不得在未达到受控开放级别时提前放行
    if grants_state == "closed" and ("visitor" in channels or "operations" in channels):
        raise ValidationFailed("closed 阶段不得放行游客或经营接口")
    return {
        "stage_id": identifier(raw.get("stage_id"), "stage.stage_id"),
        "ordinal": ordinal,
        "name": name,
        "grants_state": grants_state,
        "validity_hours": validity_hours,
        "channels": channels,
        "reviewer_role": reviewer_role,
        "reviewer_id": reviewer_id,
        "requirements": {
            "requires_stages": prerequisites,
            "evidence": requirements,
        },
    }


def plan_from_dict(raw: Mapping[str, Any]) -> dict[str, Any]:
    plan_id = identifier(raw.get("plan_id"), "plan_id")
    area_id = identifier(raw.get("area_id"), "area_id")
    title = required_text(raw.get("title"), "title")
    stages_raw = raw.get("stages")
    if not isinstance(stages_raw, list) or not stages_raw:
        raise ValidationFailed("计划必须至少包含一个恢复阶段")
    stages = [stage_from_dict(item) for item in stages_raw]
    ordinals = [stage["ordinal"] for stage in stages]
    if len(set(ordinals)) != len(ordinals):
        raise ValidationFailed("阶段 ordinal 不能重复")
    stage_ids = [stage["stage_id"] for stage in stages]
    if len(set(stage_ids)) != len(stage_ids):
        raise ValidationFailed("stage_id 不能重复")
    known_ordinals = set(ordinals)
    for stage in stages:
        unknown = [value for value in stage["requirements"]["requires_stages"] if value not in known_ordinals]
        if unknown:
            raise ValidationFailed(f"阶段 {stage['stage_id']} 依赖了不存在的阶段序号 {unknown}")
    # 开放级别只能沿阶段递进，不能靠后置阶段突然收紧以外的跳跃；
    # 但允许同级别延续（例如多轮 limited）。
    order = {"closed": 0, "limited": 1, "open": 2}
    for earlier, later in zip(stages, stages[1:]):
        if order[later["grants_state"]] < order[earlier["grants_state"]]:
            raise ValidationFailed("开放级别只能沿阶段递进，收紧应通过撤回或到期实现")
    return {"plan_id": plan_id, "area_id": area_id, "title": title, "stages": stages}
