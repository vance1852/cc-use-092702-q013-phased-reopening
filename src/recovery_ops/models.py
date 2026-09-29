"""灾后分阶段恢复编排的领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .clock import parse_utc, utc_text
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")
BUSINESS_LINES = ("patrol", "research", "visitor", "operations")
SCOPE_LEVELS = ("closed", "limited", "full")
REVIEWER_ROLES = ("reviewer", "coordinator")
REVIEW_DECISIONS = ("approved", "rejected")


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def optional_text(value: object, field: str, maximum: int = 512) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValidationFailed(f"{field} 必须是字符串")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationFailed(f"{field} 必须是对象")
    return value


def _sequence(value: object, field: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValidationFailed(f"{field} 必须是数组")
    return value


@dataclass(frozen=True, slots=True)
class EvidenceRequirement:
    """阶段依赖的一个明确证据版本。"""

    kind: str
    version: str

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "EvidenceRequirement":
        data = _mapping(raw, path)
        return cls(
            kind=required_text(data.get("kind"), f"{path}.kind", 64),
            version=required_text(data.get("version"), f"{path}.version", 64),
        )

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "version": self.version}


@dataclass(frozen=True, slots=True)
class PhaseInput:
    """恢复计划中的一个阶段定义。"""

    phase_key: str
    sequence: int
    name: str
    scopes: Mapping[str, str]
    evidence_requirements: tuple[EvidenceRequirement, ...]
    reviewer_role: str
    minimum_pass_items: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "PhaseInput":
        data = _mapping(raw, path)
        sequence = data.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise ValidationFailed(f"{path}.sequence 必须是正整数")
        scopes = _mapping(data.get("scopes"), f"{path}.scopes")
        if set(scopes) != set(BUSINESS_LINES):
            raise ValidationFailed(f"{path}.scopes 必须且只能包含 {list(BUSINESS_LINES)}")
        parsed_scopes: dict[str, str] = {}
        for line in BUSINESS_LINES:
            level = required_text(scopes.get(line), f"{path}.scopes.{line}", 16)
            if level not in SCOPE_LEVELS:
                raise ValidationFailed(f"{path}.scopes.{line} 必须是 closed、limited 或 full")
            parsed_scopes[line] = level
        requirements = tuple(
            EvidenceRequirement.from_dict(item, f"{path}.evidence_requirements[{index}]")
            for index, item in enumerate(
                _sequence(data.get("evidence_requirements", ()), f"{path}.evidence_requirements")
            )
        )
        if len({(item.kind, item.version) for item in requirements}) != len(requirements):
            raise ValidationFailed(f"{path}.evidence_requirements 存在重复证据版本")
        reviewer_role = required_text(data.get("reviewer_role"), f"{path}.reviewer_role", 32)
        if reviewer_role not in REVIEWER_ROLES:
            raise ValidationFailed(f"{path}.reviewer_role 必须是 {list(REVIEWER_ROLES)} 之一")
        items = tuple(
            identifier(item, f"{path}.minimum_pass_items[{index}]")
            for index, item in enumerate(
                _sequence(data.get("minimum_pass_items", ()), f"{path}.minimum_pass_items")
            )
        )
        if len(set(items)) != len(items):
            raise ValidationFailed(f"{path}.minimum_pass_items 不能重复")
        return cls(
            phase_key=identifier(data.get("phase_key"), f"{path}.phase_key"),
            sequence=sequence,
            name=required_text(data.get("name"), f"{path}.name"),
            scopes=parsed_scopes,
            evidence_requirements=requirements,
            reviewer_role=reviewer_role,
            minimum_pass_items=items,
        )


@dataclass(frozen=True, slots=True)
class PlanInput:
    """一个受影响区域的分阶段恢复计划。"""

    plan_id: str
    zone_id: str
    title: str
    phases: tuple[PhaseInput, ...]

    @classmethod
    def from_dict(cls, raw: object) -> "PlanInput":
        data = _mapping(raw, "plan")
        phases = tuple(
            PhaseInput.from_dict(item, f"plan.phases[{index}]")
            for index, item in enumerate(_sequence(data.get("phases"), "plan.phases"))
        )
        if not phases:
            raise ValidationFailed("plan.phases 不能为空")
        if len({phase.phase_key for phase in phases}) != len(phases):
            raise ValidationFailed("plan.phases.phase_key 不能重复")
        if len({phase.sequence for phase in phases}) != len(phases):
            raise ValidationFailed("plan.phases.sequence 不能重复")
        return cls(
            plan_id=identifier(data.get("plan_id"), "plan.plan_id"),
            zone_id=identifier(data.get("zone_id"), "plan.zone_id"),
            title=required_text(data.get("title"), "plan.title"),
            phases=phases,
        )


@dataclass(frozen=True, slots=True)
class EvidenceInput:
    """现场提交的一个版本化证据。"""

    evidence_id: str
    zone_id: str
    kind: str
    version: str
    content_sha256: str
    note: str

    @classmethod
    def from_dict(cls, raw: object) -> "EvidenceInput":
        data = _mapping(raw, "evidence")
        sha256 = required_text(data.get("content_sha256"), "evidence.content_sha256", 64)
        if not SHA256_HEX.fullmatch(sha256):
            raise ValidationFailed("evidence.content_sha256 必须是 64 位十六进制摘要")
        return cls(
            evidence_id=identifier(data.get("evidence_id"), "evidence.evidence_id"),
            zone_id=identifier(data.get("zone_id"), "evidence.zone_id"),
            kind=required_text(data.get("kind"), "evidence.kind", 64),
            version=required_text(data.get("version"), "evidence.version", 64),
            content_sha256=sha256.lower(),
            note=optional_text(data.get("note"), "evidence.note"),
        )


@dataclass(frozen=True, slots=True)
class DirectiveInput:
    """带生效期的恢复指令；开放范围只能来自阶段定义，指令自身不携带。"""

    plan_id: str
    phase_key: str
    effective_from: str
    effective_until: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: object) -> "DirectiveInput":
        data = _mapping(raw, "directive")
        try:
            start = parse_utc(
                required_text(data.get("effective_from"), "directive.effective_from", 40),
                "effective_from",
            )
            end = parse_utc(
                required_text(data.get("effective_until"), "directive.effective_until", 40),
                "effective_until",
            )
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end <= start:
            raise ValidationFailed("directive.effective_until 必须晚于 effective_from")
        return cls(
            plan_id=identifier(data.get("plan_id"), "directive.plan_id"),
            phase_key=identifier(data.get("phase_key"), "directive.phase_key"),
            effective_from=utc_text(start),
            effective_until=utc_text(end),
            idempotency_key=identifier(data.get("idempotency_key"), "directive.idempotency_key"),
        )
