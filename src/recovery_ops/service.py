"""灾后分阶段恢复编排的事务用例。

联席会按受影响区域编排恢复阶段：每个阶段声明依赖的证据版本、复核责任和
最低通过项；恢复指令带生效期，签发时把证据版本、复核记录和前置通过项快照
进决定依据。重复执行、撤回、到期或证据失效都会留下哈希链历史并立即收紧
权限。巡护、科研、游客、经营四类接口读取同一阶段投影，公众只看到必要的
开放状态，审计人员可重建每次决定依据以及现场实际执行到哪里。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Iterable, Mapping

from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    BUSINESS_LINES,
    REVIEW_DECISIONS,
    DirectiveInput,
    EvidenceInput,
    PlanInput,
    optional_text,
    required_text,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "coordinator": {
        "plan.write",
        "directive.issue",
        "directive.withdraw",
        "directive.expire",
        "evidence.invalidate",
        "review.write",
        "projection.read",
    },
    "surveyor": {"evidence.submit", "field.confirm", "projection.read"},
    "reviewer": {"review.write", "projection.read"},
    "auditor": {"audit.read", "basis.read", "projection.read"},
}

CLOSED_SCOPES = {line: "closed" for line in BUSINESS_LINES}


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class RecoveryService:
    """在单个 SQLite 连接上提供灾后分阶段恢复编排的全部业务操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM recovery_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM recovery_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO recovery_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO recovery_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ------------------------------------------------------------------
    # 内部查询与收紧辅助
    # ------------------------------------------------------------------

    def _plan(self, plan_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM recovery_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFound("恢复计划不存在")
        return row

    def _phase(self, plan_id: str, phase_key: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM recovery_phases WHERE plan_id=? AND phase_key=?",
            (plan_id, phase_key),
        ).fetchone()
        if row is None:
            raise NotFound("恢复阶段不存在")
        return row

    def _sweep_expired(self, actor_id: str) -> list[int]:
        """把已过生效期的指令转为 expired 并留下历史；必须在事务内调用。"""

        now = self._now()
        rows = self.connection.execute(
            "SELECT directive_id,plan_id,phase_key,effective_until FROM recovery_directives "
            "WHERE state='active' AND effective_until<=? ORDER BY directive_id",
            (now,),
        ).fetchall()
        for row in rows:
            self.connection.execute(
                "UPDATE recovery_directives SET state='expired',closed_by=?,closed_at=?,"
                "close_reason='effective_until_reached' WHERE directive_id=? AND state='active'",
                (actor_id, now, row["directive_id"]),
            )
            self._audit(
                "directive",
                str(row["directive_id"]),
                "directive.expired",
                actor_id,
                {
                    "plan_id": row["plan_id"],
                    "phase_key": row["phase_key"],
                    "effective_until": row["effective_until"],
                },
            )
        return [int(row["directive_id"]) for row in rows]

    def _tighten(
        self,
        actor_id: str,
        rows: Iterable[sqlite3.Row],
        close_reason: str,
        trigger: Mapping[str, Any],
    ) -> list[int]:
        """把生效中的指令收紧为 tightened 并留下历史；必须在事务内调用。"""

        now = self._now()
        tightened: list[int] = []
        for row in rows:
            cursor = self.connection.execute(
                "UPDATE recovery_directives SET state='tightened',closed_by=?,closed_at=?,close_reason=? "
                "WHERE directive_id=? AND state='active'",
                (actor_id, now, close_reason, row["directive_id"]),
            )
            if cursor.rowcount != 1:
                continue
            tightened.append(int(row["directive_id"]))
            self._audit(
                "directive",
                str(row["directive_id"]),
                "directive.tightened",
                actor_id,
                {"plan_id": row["plan_id"], "phase_key": row["phase_key"], **trigger},
            )
        return tightened

    # ------------------------------------------------------------------
    # 恢复计划与阶段编排
    # ------------------------------------------------------------------

    def create_plan(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "plan.write")
        plan = PlanInput.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO recovery_plans(plan_id,zone_id,title,state,created_by,created_at) "
                    "VALUES(?,?,?,'draft',?,?)",
                    (plan.plan_id, plan.zone_id, plan.title, actor_id, self._now()),
                )
                for phase in plan.phases:
                    self.connection.execute(
                        "INSERT INTO recovery_phases(plan_id,phase_key,sequence,name,scopes_json,"
                        "evidence_requirements_json,reviewer_role,minimum_pass_items_json) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (
                            plan.plan_id,
                            phase.phase_key,
                            phase.sequence,
                            phase.name,
                            canonical_json(phase.scopes),
                            canonical_json([item.as_dict() for item in phase.evidence_requirements]),
                            phase.reviewer_role,
                            canonical_json(list(phase.minimum_pass_items)),
                        ),
                    )
                self._audit(
                    "plan",
                    plan.plan_id,
                    "plan.created",
                    actor_id,
                    {
                        "zone_id": plan.zone_id,
                        "title": plan.title,
                        "phases": [phase.phase_key for phase in plan.phases],
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("恢复计划编号已经存在") from exc
        return {
            "plan_id": plan.plan_id,
            "zone_id": plan.zone_id,
            "state": "draft",
            "revision": 1,
            "phases": len(plan.phases),
        }

    def activate_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "plan.write")
        with transaction(self.connection, immediate=True):
            plan = self._plan(plan_id)
            try:
                cursor = self.connection.execute(
                    "UPDATE recovery_plans SET state='active',revision=revision+1 "
                    "WHERE plan_id=? AND state='draft' AND revision=?",
                    (plan_id, expected_revision),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("该区域已有生效中的恢复计划") from exc
            if cursor.rowcount != 1:
                raise InvalidState("恢复计划不是当前草稿版本")
            self._audit(
                "plan", plan_id, "plan.activated", actor_id, {"zone_id": plan["zone_id"]}
            )
        return {"plan_id": plan_id, "state": "active", "revision": expected_revision + 1}

    def retire_plan(self, actor_id: str, plan_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "plan.write")
        reason = required_text(reason, "reason")
        with transaction(self.connection, immediate=True):
            self._sweep_expired("system")
            plan = self._plan(plan_id)
            if plan["state"] != "active":
                raise InvalidState("只有生效中的恢复计划可以停用")
            now = self._now()
            self.connection.execute(
                "UPDATE recovery_plans SET state='retired',revision=revision+1 "
                "WHERE plan_id=? AND state='active'",
                (plan_id,),
            )
            affected = self.connection.execute(
                "SELECT directive_id,plan_id,phase_key FROM recovery_directives "
                "WHERE plan_id=? AND state='active' ORDER BY directive_id",
                (plan_id,),
            ).fetchall()
            tightened = self._tighten(
                actor_id, affected, "plan_retired", {"trigger": "plan_retired"}
            )
            self._audit(
                "plan",
                plan_id,
                "plan.retired",
                actor_id,
                {"reason": reason, "tightened_directives": tightened, "retired_at": now},
            )
        return {"plan_id": plan_id, "state": "retired", "tightened_directives": tightened}

    # ------------------------------------------------------------------
    # 证据版本
    # ------------------------------------------------------------------

    def submit_evidence(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "evidence.submit")
        evidence = EvidenceInput.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO recovery_evidence(evidence_id,zone_id,kind,version,content_sha256,"
                    "note,state,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        evidence.evidence_id,
                        evidence.zone_id,
                        evidence.kind,
                        evidence.version,
                        evidence.content_sha256,
                        evidence.note,
                        "valid",
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "evidence",
                    evidence.evidence_id,
                    "evidence.submitted",
                    actor_id,
                    {
                        "zone_id": evidence.zone_id,
                        "kind": evidence.kind,
                        "version": evidence.version,
                        "content_sha256": evidence.content_sha256,
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("证据编号或该区域同类型版本已经存在") from exc
        return {
            "evidence_id": evidence.evidence_id,
            "zone_id": evidence.zone_id,
            "kind": evidence.kind,
            "version": evidence.version,
            "state": "valid",
        }

    def invalidate_evidence(self, actor_id: str, evidence_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "evidence.invalidate")
        reason = required_text(reason, "reason")
        with transaction(self.connection, immediate=True):
            self._sweep_expired("system")
            row = self.connection.execute(
                "SELECT * FROM recovery_evidence WHERE evidence_id=?", (evidence_id,)
            ).fetchone()
            if row is None:
                raise NotFound("证据不存在")
            if row["state"] != "valid":
                raise InvalidState("证据已经失效")
            now = self._now()
            self.connection.execute(
                "UPDATE recovery_evidence SET state='invalidated',invalidated_by=?,invalidated_at=?,"
                "invalidation_reason=? WHERE evidence_id=? AND state='valid'",
                (actor_id, now, reason, evidence_id),
            )
            self._audit(
                "evidence",
                evidence_id,
                "evidence.invalidated",
                actor_id,
                {"kind": row["kind"], "version": row["version"], "reason": reason},
            )
            affected = self.connection.execute(
                "SELECT d.directive_id,d.plan_id,d.phase_key FROM recovery_directives d "
                "JOIN directive_evidence e ON e.directive_id=d.directive_id "
                "WHERE e.evidence_id=? AND d.state='active' ORDER BY d.directive_id",
                (evidence_id,),
            ).fetchall()
            tightened = self._tighten(
                actor_id,
                affected,
                f"evidence_invalidated:{evidence_id}",
                {"trigger": "evidence_invalidated", "evidence_id": evidence_id},
            )
        return {
            "evidence_id": evidence_id,
            "state": "invalidated",
            "tightened_directives": tightened,
        }

    # ------------------------------------------------------------------
    # 阶段复核
    # ------------------------------------------------------------------

    def review_phase(
        self,
        actor_id: str,
        plan_id: str,
        phase_key: str,
        decision: str,
        note: str = "",
    ) -> dict[str, Any]:
        user = self._require(actor_id, "review.write")
        if decision not in REVIEW_DECISIONS:
            raise ValidationFailed("未知复核结论")
        note = optional_text(note, "note")
        with transaction(self.connection, immediate=True):
            plan = self._plan(plan_id)
            if plan["state"] == "retired":
                raise InvalidState("已停用的恢复计划不能复核")
            phase = self._phase(plan_id, phase_key)
            if user["role"] != phase["reviewer_role"]:
                raise Forbidden(f"该阶段复核责任属于角色 {phase['reviewer_role']}")
            if plan["created_by"] == actor_id:
                raise Forbidden("计划编制人不能复核自己的计划")
            cursor = self.connection.execute(
                "INSERT INTO phase_reviews(plan_id,phase_key,decision,note,reviewer_id,reviewer_role,"
                "created_at) VALUES(?,?,?,?,?,?,?)",
                (plan_id, phase_key, decision, note, actor_id, user["role"], self._now()),
            )
            review_id = int(cursor.lastrowid)
            self._audit(
                "phase",
                f"{plan_id}/{phase_key}",
                "phase.reviewed",
                actor_id,
                {"review_id": review_id, "decision": decision, "reviewer_role": user["role"]},
            )
        return {
            "review_id": review_id,
            "plan_id": plan_id,
            "phase_key": phase_key,
            "decision": decision,
        }

    # ------------------------------------------------------------------
    # 恢复指令
    # ------------------------------------------------------------------

    def issue_directive(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "directive.issue")
        directive = DirectiveInput.from_dict(raw)
        request_sha256 = digest(
            {
                "plan_id": directive.plan_id,
                "phase_key": directive.phase_key,
                "effective_from": directive.effective_from,
                "effective_until": directive.effective_until,
            }
        )
        with transaction(self.connection, immediate=True):
            self._sweep_expired("system")
            stored = self.connection.execute(
                "SELECT * FROM recovery_directives WHERE idempotency_key=?",
                (directive.idempotency_key,),
            ).fetchone()
            if stored is not None:
                if stored["request_sha256"] != request_sha256:
                    raise Conflict("同一幂等键对应了不同的恢复指令内容")
                self._audit(
                    "directive",
                    str(stored["directive_id"]),
                    "directive.replayed",
                    actor_id,
                    {"idempotency_key": directive.idempotency_key},
                )
                return json.loads(stored["response_json"]) | {"replayed": True}
            plan = self._plan(directive.plan_id)
            if plan["state"] != "active":
                raise InvalidState("恢复计划未生效，不能签发恢复指令")
            phase = self._phase(directive.plan_id, directive.phase_key)
            missing: list[str] = []
            review = self.connection.execute(
                "SELECT * FROM phase_reviews WHERE plan_id=? AND phase_key=? "
                "ORDER BY review_id DESC LIMIT 1",
                (plan["plan_id"], phase["phase_key"]),
            ).fetchone()
            if review is None or review["decision"] != "approved":
                missing.append(
                    f"阶段 {phase['phase_key']} 缺少角色 {phase['reviewer_role']} 的复核通过记录"
                )
            requirements = json.loads(phase["evidence_requirements_json"])
            evidence_rows: list[sqlite3.Row] = []
            for requirement in requirements:
                row = self.connection.execute(
                    "SELECT * FROM recovery_evidence WHERE zone_id=? AND kind=? AND version=? "
                    "AND state='valid'",
                    (plan["zone_id"], requirement["kind"], requirement["version"]),
                ).fetchone()
                if row is None:
                    missing.append(
                        f"证据 {requirement['kind']}@{requirement['version']} 缺失或已失效"
                    )
                else:
                    evidence_rows.append(row)
            prior_phases = self.connection.execute(
                "SELECT * FROM recovery_phases WHERE plan_id=? AND sequence<? ORDER BY sequence",
                (plan["plan_id"], phase["sequence"]),
            ).fetchall()
            confirmed = {
                row["item_key"]
                for row in self.connection.execute(
                    "SELECT f.item_key FROM field_confirmations f "
                    "JOIN recovery_phases p ON p.plan_id=f.plan_id AND p.phase_key=f.phase_key "
                    "WHERE f.plan_id=? AND p.sequence<?",
                    (plan["plan_id"], phase["sequence"]),
                ).fetchall()
            }
            for prior in prior_phases:
                for item_key in json.loads(prior["minimum_pass_items_json"]):
                    if item_key not in confirmed:
                        missing.append(
                            f"前置阶段 {prior['phase_key']} 的最低通过项 {item_key} 未现场确认"
                        )
            if missing:
                raise InvalidState("前置条件不齐，不得扩大任何业务范围: " + "；".join(missing))
            basis = {
                "plan_id": plan["plan_id"],
                "zone_id": plan["zone_id"],
                "phase_key": phase["phase_key"],
                "sequence": phase["sequence"],
                "evidence": [
                    {
                        "evidence_id": row["evidence_id"],
                        "kind": row["kind"],
                        "version": row["version"],
                        "content_sha256": row["content_sha256"],
                    }
                    for row in evidence_rows
                ],
                "review": {
                    "review_id": review["review_id"],
                    "reviewer_id": review["reviewer_id"],
                    "reviewer_role": review["reviewer_role"],
                    "decided_at": review["created_at"],
                },
                "prior_pass_items_confirmed": sorted(confirmed),
            }
            now = self._now()
            try:
                cursor = self.connection.execute(
                    "INSERT INTO recovery_directives(plan_id,phase_key,idempotency_key,effective_from,"
                    "effective_until,state,basis_json,request_sha256,response_json,issued_by,issued_at) "
                    "VALUES(?,?,?,?,?,'active',?,?,?,?,?)",
                    (
                        plan["plan_id"],
                        phase["phase_key"],
                        directive.idempotency_key,
                        directive.effective_from,
                        directive.effective_until,
                        canonical_json(basis),
                        request_sha256,
                        "{}",
                        actor_id,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("恢复指令幂等键冲突") from exc
            directive_id = int(cursor.lastrowid)
            for row in evidence_rows:
                self.connection.execute(
                    "INSERT INTO directive_evidence(directive_id,evidence_id,kind,version,"
                    "content_sha256) VALUES(?,?,?,?,?)",
                    (
                        directive_id,
                        row["evidence_id"],
                        row["kind"],
                        row["version"],
                        row["content_sha256"],
                    ),
                )
            stored_response = {
                "directive_id": directive_id,
                "plan_id": plan["plan_id"],
                "zone_id": plan["zone_id"],
                "phase_key": phase["phase_key"],
                "state": "active",
                "effective_from": directive.effective_from,
                "effective_until": directive.effective_until,
                "scopes": json.loads(phase["scopes_json"]),
                "evidence": [item["evidence_id"] for item in basis["evidence"]],
            }
            self.connection.execute(
                "UPDATE recovery_directives SET response_json=? WHERE directive_id=?",
                (canonical_json(stored_response), directive_id),
            )
            self._audit(
                "directive",
                str(directive_id),
                "directive.issued",
                actor_id,
                {
                    "plan_id": plan["plan_id"],
                    "phase_key": phase["phase_key"],
                    "effective_from": directive.effective_from,
                    "effective_until": directive.effective_until,
                    "basis": basis,
                },
            )
            return stored_response | {"replayed": False}

    def withdraw_directive(self, actor_id: str, directive_id: int, reason: str) -> dict[str, Any]:
        self._require(actor_id, "directive.withdraw")
        reason = required_text(reason, "reason")
        with transaction(self.connection, immediate=True):
            self._sweep_expired("system")
            row = self.connection.execute(
                "SELECT * FROM recovery_directives WHERE directive_id=?", (directive_id,)
            ).fetchone()
            if row is None:
                raise NotFound("恢复指令不存在")
            if row["state"] != "active":
                raise InvalidState("只有生效中的恢复指令可以撤回")
            now = self._now()
            cursor = self.connection.execute(
                "UPDATE recovery_directives SET state='withdrawn',closed_by=?,closed_at=?,"
                "close_reason=? WHERE directive_id=? AND state='active'",
                (actor_id, now, reason, directive_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("恢复指令状态已变化")
            self._audit(
                "directive",
                str(directive_id),
                "directive.withdrawn",
                actor_id,
                {"plan_id": row["plan_id"], "phase_key": row["phase_key"], "reason": reason},
            )
        return {"directive_id": directive_id, "state": "withdrawn"}

    def expire_directives(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "directive.expire")
        with transaction(self.connection, immediate=True):
            expired = self._sweep_expired(actor_id)
            return {"expired": expired, "evaluated_at": self._now()}

    # ------------------------------------------------------------------
    # 现场执行回执
    # ------------------------------------------------------------------

    def confirm_pass_item(
        self,
        actor_id: str,
        plan_id: str,
        phase_key: str,
        item_key: str,
        note: str = "",
    ) -> dict[str, Any]:
        self._require(actor_id, "field.confirm")
        note = optional_text(note, "note")
        with transaction(self.connection, immediate=True):
            self._sweep_expired("system")
            plan = self._plan(plan_id)
            if plan["state"] != "active":
                raise InvalidState("恢复计划未生效，不能登记现场执行")
            phase = self._phase(plan_id, phase_key)
            items = json.loads(phase["minimum_pass_items_json"])
            if item_key not in items:
                raise ValidationFailed("通过项不在该阶段最低通过项中")
            now = self._now()
            effective = self.connection.execute(
                "SELECT 1 FROM recovery_directives WHERE plan_id=? AND phase_key=? AND state='active' "
                "AND effective_from<=? AND effective_until>? LIMIT 1",
                (plan_id, phase_key, now, now),
            ).fetchone()
            if effective is None:
                raise InvalidState("当前阶段没有生效中的恢复指令，不能登记现场执行")
            try:
                cursor = self.connection.execute(
                    "INSERT INTO field_confirmations(plan_id,phase_key,item_key,note,confirmed_by,"
                    "confirmed_at) VALUES(?,?,?,?,?,?)",
                    (plan_id, phase_key, item_key, note, actor_id, now),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("该最低通过项已现场确认") from exc
            confirmation_id = int(cursor.lastrowid)
            self._audit(
                "field",
                f"{plan_id}/{phase_key}/{item_key}",
                "field.item_confirmed",
                actor_id,
                {"confirmation_id": confirmation_id, "plan_id": plan_id, "phase_key": phase_key},
            )
        return {
            "confirmation_id": confirmation_id,
            "plan_id": plan_id,
            "phase_key": phase_key,
            "item_key": item_key,
        }

    # ------------------------------------------------------------------
    # 阶段投影：巡护、科研、游客、经营读取同一投影
    # ------------------------------------------------------------------

    def _projection_locked(self, zone_id: str) -> dict[str, Any]:
        now = self._now()
        plan = self.connection.execute(
            "SELECT * FROM recovery_plans WHERE zone_id=? AND state='active'", (zone_id,)
        ).fetchone()
        result: dict[str, Any] = {
            "zone_id": zone_id,
            "evaluated_at": now,
            "plan_id": None,
            "phase_key": None,
            "phase_name": None,
            "sequence": 0,
            "directive_id": None,
            "scopes": dict(CLOSED_SCOPES),
            "field_progress": None,
        }
        if plan is None:
            return result
        result["plan_id"] = plan["plan_id"]
        row = self.connection.execute(
            "SELECT d.directive_id,d.phase_key,p.sequence,p.name,p.scopes_json,"
            "p.minimum_pass_items_json FROM recovery_directives d "
            "JOIN recovery_phases p ON p.plan_id=d.plan_id AND p.phase_key=d.phase_key "
            "WHERE d.plan_id=? AND d.state='active' AND d.effective_from<=? AND d.effective_until>? "
            "ORDER BY d.directive_id DESC LIMIT 1",
            (plan["plan_id"], now, now),
        ).fetchone()
        if row is None:
            return result
        scopes = dict(CLOSED_SCOPES)
        scopes.update(json.loads(row["scopes_json"]))
        items = json.loads(row["minimum_pass_items_json"])
        confirmed_rows = self.connection.execute(
            "SELECT item_key,confirmed_by,confirmed_at FROM field_confirmations "
            "WHERE plan_id=? AND phase_key=? ORDER BY confirmation_id",
            (plan["plan_id"], row["phase_key"]),
        ).fetchall()
        confirmed_keys = {entry["item_key"] for entry in confirmed_rows}
        result.update(
            {
                "phase_key": row["phase_key"],
                "phase_name": row["name"],
                "sequence": row["sequence"],
                "directive_id": row["directive_id"],
                "scopes": scopes,
                "field_progress": {
                    "required": items,
                    "confirmed": [dict(entry) for entry in confirmed_rows],
                    "outstanding": [key for key in items if key not in confirmed_keys],
                },
            }
        )
        return result

    def zone_projection(self, actor_id: str, zone_id: str) -> dict[str, Any]:
        self._require(actor_id, "projection.read")
        with transaction(self.connection, immediate=True):
            self._sweep_expired("system")
            return self._projection_locked(zone_id)

    def interface_status(self, actor_id: str, zone_id: str, line: str) -> dict[str, Any]:
        if line not in BUSINESS_LINES:
            raise ValidationFailed("未知业务接口")
        projection = self.zone_projection(actor_id, zone_id)
        return {
            "zone_id": zone_id,
            "interface": line,
            "status": projection["scopes"][line],
            "phase_key": projection["phase_key"],
            "directive_id": projection["directive_id"],
            "evaluated_at": projection["evaluated_at"],
        }

    def public_status(self) -> dict[str, Any]:
        """公众视图：只暴露每个区域必要的开放状态。"""

        with transaction(self.connection, immediate=True):
            self._sweep_expired("system")
            rows = self.connection.execute(
                "SELECT zone_id FROM recovery_plans WHERE state='active' ORDER BY zone_id"
            ).fetchall()
            zones: list[dict[str, str]] = []
            for row in rows:
                projection = self._projection_locked(row["zone_id"])
                scopes = projection["scopes"]
                if scopes["visitor"] == "full":
                    status = "open"
                elif any(scopes[line] != "closed" for line in BUSINESS_LINES):
                    status = "partial"
                else:
                    status = "closed"
                zones.append({"zone_id": row["zone_id"], "status": status})
            return {"zones": zones, "evaluated_at": self._now()}

    # ------------------------------------------------------------------
    # 审计重建
    # ------------------------------------------------------------------

    def decision_basis(self, actor_id: str, directive_id: int) -> dict[str, Any]:
        self._require(actor_id, "basis.read")
        with transaction(self.connection, immediate=True):
            self._sweep_expired("system")
            row = self.connection.execute(
                "SELECT * FROM recovery_directives WHERE directive_id=?", (directive_id,)
            ).fetchone()
            if row is None:
                raise NotFound("恢复指令不存在")
            directive = dict(row)
            directive["basis"] = json.loads(directive.pop("basis_json"))
            directive.pop("response_json")
            phase = self._phase(row["plan_id"], row["phase_key"])
            evidence = self.connection.execute(
                "SELECT e.evidence_id,e.kind,e.version,e.content_sha256,v.state AS current_state,"
                "v.invalidated_at,v.invalidation_reason FROM directive_evidence e "
                "JOIN recovery_evidence v ON v.evidence_id=e.evidence_id "
                "WHERE e.directive_id=? ORDER BY e.kind",
                (directive_id,),
            ).fetchall()
            review = self.connection.execute(
                "SELECT * FROM phase_reviews WHERE plan_id=? AND phase_key=? "
                "ORDER BY review_id DESC LIMIT 1",
                (row["plan_id"], row["phase_key"]),
            ).fetchone()
            items = json.loads(phase["minimum_pass_items_json"])
            confirmations = self.connection.execute(
                "SELECT confirmation_id,item_key,note,confirmed_by,confirmed_at "
                "FROM field_confirmations WHERE plan_id=? AND phase_key=? "
                "ORDER BY confirmation_id",
                (row["plan_id"], row["phase_key"]),
            ).fetchall()
            confirmed_keys = {entry["item_key"] for entry in confirmations}
            events = self.connection.execute(
                "SELECT event_type,actor_id,payload_json,created_at FROM recovery_audit_events "
                "WHERE entity_type='directive' AND entity_id=? ORDER BY event_id",
                (str(directive_id),),
            ).fetchall()
            return {
                "directive": directive,
                "phase": {
                    "phase_key": phase["phase_key"],
                    "sequence": phase["sequence"],
                    "name": phase["name"],
                    "scopes": json.loads(phase["scopes_json"]),
                    "reviewer_role": phase["reviewer_role"],
                    "evidence_requirements": json.loads(phase["evidence_requirements_json"]),
                    "minimum_pass_items": items,
                },
                "evidence": [dict(entry) for entry in evidence],
                "review": None if review is None else dict(review),
                "field_execution": {
                    "confirmed": [dict(entry) for entry in confirmations],
                    "outstanding": [key for key in items if key not in confirmed_keys],
                },
                "events": [
                    {
                        "event_type": entry["event_type"],
                        "actor_id": entry["actor_id"],
                        "payload": json.loads(entry["payload_json"]),
                        "created_at": entry["created_at"],
                    }
                    for entry in events
                ],
            }

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute(
            "SELECT * FROM recovery_audit_events ORDER BY event_id"
        ).fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
