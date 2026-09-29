"""灾后分区域分阶段恢复治理的领域用例。

核心约束：
- 恢复指令按受影响区域编排为有序阶段，每阶段固定依赖的证据版本、
  复核责任人与最低通过项；前置条件不齐不得激活、不得扩大业务范围。
- 指令带生效期；重复执行、撤回、到期、证据失效与前置阶段失效都会
  留痕并立即收紧权限。
- 巡护、科研、游客、经营四类接口读取同一个阶段投影，无法各自提前放行。
- 公众只能看到最小开放状态；审计可以重建每次决定的依据与现场执行。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import timedelta
from typing import Any, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, digest
from .models import (
    PUBLIC_STATES,
    SCOPE_LABELS,
    affected_area_from_dict,
    evidence_from_dict,
    plan_from_dict,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "coordinator": {
        "area.write", "evidence.write", "plan.write", "plan.issue",
        "activation.write", "execution.write", "review.write", "report.read",
    },
    "reviewer": {"evidence.write", "review.write", "report.read"},
    "auditor": {"report.read", "audit.read"},
}

STATE_RANK = {"closed": 0, "limited": 1, "open": 2}
STATE_NAMES = {"closed": "关闭", "limited": "受控开放", "open": "开放"}
SYSTEM_ACTOR = "system"


class RecoveryGovernanceService:
    """在单个 SQLite 连接上提供全部恢复治理操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"用户不存在: {user_id}")
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
            "SELECT event_hash FROM audit_events ORDER BY event_id DESC LIMIT 1"
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
        event_hash = digest(body)
        self.connection.execute(
            "INSERT INTO audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
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

    def bootstrap(self) -> None:
        """创建演示账户与系统账户；重复执行无副作用。"""

        with transaction(self.connection, immediate=True):
            existing = self.connection.execute(
                "SELECT user_id FROM users WHERE user_id=?", (SYSTEM_ACTOR,)
            ).fetchone()
            if existing is None:
                # 系统账户停用，仅供到期/失效等自动事件署名。
                self.connection.execute(
                    "INSERT INTO users(user_id,display_name,role,active,created_at) "
                    "VALUES(?,?,?,0,?)",
                    (SYSTEM_ACTOR, "系统", "auditor", self._now()),
                )
            for user_id, display_name, role in (
                ("coord", "联席会协调员", "coordinator"),
                ("rev", "现场复核员", "reviewer"),
                ("audit", "审计员", "auditor"),
            ):
                if self.connection.execute(
                    "SELECT 1 FROM users WHERE user_id=?", (user_id,)
                ).fetchone() is None:
                    self.connection.execute(
                        "INSERT INTO users(user_id,display_name,role,active,created_at) "
                        "VALUES(?,?,?,1,?)",
                        (user_id, display_name, role, self._now()),
                    )

    def create_user(self, actor_id: str, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        self._require(actor_id, "plan.write")
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
                self._audit("user", user_id.strip(), "user.created", actor_id, {"role": role})
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ------------------------------------------------------------- 区域与证据

    def register_area(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "area.write")
        area = affected_area_from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO affected_areas(area_id,name,scope,created_by,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (area["area_id"], area["name"], area["scope"], actor_id, self._now()),
                )
                self._audit("area", area["area_id"], "area.registered", actor_id, area)
        except sqlite3.IntegrityError as exc:
            raise Conflict("受影响区域编号已经存在") from exc
        return {**area, "public_state": "closed"}

    def submit_evidence(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "evidence.write")
        evidence = evidence_from_dict(raw)
        body = canonical_json(evidence["payload"])
        content_sha256 = digest(evidence["payload"])
        with transaction(self.connection, immediate=True):
            latest = self.connection.execute(
                "SELECT max(version) AS version FROM evidence_versions WHERE evidence_id=?",
                (evidence["evidence_id"],),
            ).fetchone()
            if latest["version"] is not None and evidence["version"] <= latest["version"]:
                raise Conflict("证据版本必须严格递增，请使用更高版本号")
            try:
                self.connection.execute(
                    "INSERT INTO evidence_versions(evidence_id,version,title,evidence_type,"
                    "canonical_json,content_sha256,submitted_by,submitted_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        evidence["evidence_id"], evidence["version"], evidence["title"],
                        evidence["evidence_type"], body, content_sha256, actor_id, self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("证据版本已经存在") from exc
            self._audit(
                "evidence",
                f"{evidence['evidence_id']}:{evidence['version']}",
                "evidence.submitted",
                actor_id,
                {
                    "evidence_id": evidence["evidence_id"],
                    "version": evidence["version"],
                    "title": evidence["title"],
                    "content_sha256": content_sha256,
                },
            )
        return {
            "evidence_id": evidence["evidence_id"],
            "version": evidence["version"],
            "state": "active",
            "content_sha256": content_sha256,
        }

    def invalidate_evidence(
        self, actor_id: str, evidence_id: str, version: int, reason: str
    ) -> dict[str, Any]:
        """证据失效：标记版本并立即收紧引用它的全部生效指令。"""

        self._require(actor_id, "evidence.write")
        if not reason.strip():
            raise ValidationFailed("失效原因不能为空")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT state FROM evidence_versions WHERE evidence_id=? AND version=?",
                (evidence_id, version),
            ).fetchone()
            if row is None:
                raise NotFound("证据版本不存在")
            if row["state"] != "active":
                raise InvalidState("证据版本已经失效")
            self.connection.execute(
                "UPDATE evidence_versions SET state='invalidated',invalidated_at=?,"
                "invalidate_reason=? WHERE evidence_id=? AND version=?",
                (self._now(), reason.strip(), evidence_id, version),
            )
            self._audit(
                "evidence", f"{evidence_id}:{version}", "evidence.invalidated",
                actor_id, {"reason": reason.strip()},
            )
            tightened = self._sweep_locked(actor=actor_id)
        return {
            "evidence_id": evidence_id,
            "version": version,
            "state": "invalidated",
            "tightened_activations": tightened,
        }

    # ----------------------------------------------------------------- 计划

    def _get_plan(self, plan_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM recovery_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFound("恢复计划不存在")
        return row

    def _get_stage(self, stage_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM recovery_stages WHERE stage_id=?", (stage_id,)
        ).fetchone()
        if row is None:
            raise NotFound("恢复阶段不存在")
        return row

    def create_plan(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "plan.write")
        plan = plan_from_dict(raw)
        area = self.connection.execute(
            "SELECT area_id FROM affected_areas WHERE area_id=?", (plan["area_id"],)
        ).fetchone()
        if area is None:
            raise NotFound("受影响区域不存在")
        for stage in plan["stages"]:
            designated = stage["reviewer_id"]
            if designated is not None:
                designee = self.connection.execute(
                    "SELECT role FROM users WHERE user_id=? AND active=1", (designated,)
                ).fetchone()
                if designee is None:
                    raise ValidationFailed(f"阶段 {stage['stage_id']} 指定的复核责任人不存在")
                if stage["reviewer_role"] == "coordinator" and designee["role"] != "coordinator":
                    raise ValidationFailed(f"阶段 {stage['stage_id']} 必须由协调员复核")
                if designee["role"] not in {"reviewer", "coordinator"}:
                    raise ValidationFailed(f"阶段 {stage['stage_id']} 指定人员不承担复核责任")
        content_sha256 = digest(plan)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO recovery_plans(plan_id,area_id,title,content_sha256,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (plan["plan_id"], plan["area_id"], plan["title"], content_sha256,
                     actor_id, self._now()),
                )
                for stage in plan["stages"]:
                    self.connection.execute(
                        "INSERT INTO recovery_stages(stage_id,plan_id,ordinal,name,grants_state,"
                        "validity_hours,channels_json,requirements_json,reviewer_role,"
                        "reviewer_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            stage["stage_id"], plan["plan_id"], stage["ordinal"], stage["name"],
                            stage["grants_state"], stage["validity_hours"],
                            canonical_json(stage["channels"]),
                            canonical_json(stage["requirements"]),
                            stage["reviewer_role"], stage["reviewer_id"], self._now(),
                        ),
                    )
                self._audit(
                    "plan", plan["plan_id"], "plan.created", actor_id,
                    {"area_id": plan["area_id"], "stages": len(plan["stages"]),
                     "content_sha256": content_sha256},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("恢复计划编号或阶段编号冲突") from exc
        return {"plan_id": plan["plan_id"], "state": "draft", "revision": 1,
                "stages": len(plan["stages"]), "content_sha256": content_sha256}

    def issue_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "plan.issue")
        with transaction(self.connection, immediate=True):
            plan = self._get_plan(plan_id)
            if plan["state"] != "draft" or plan["revision"] != expected_revision:
                raise InvalidState("计划不是可发布的当前草稿版本")
            other = self.connection.execute(
                "SELECT plan_id FROM recovery_plans WHERE area_id=? AND state='issued'",
                (plan["area_id"],),
            ).fetchone()
            if other is not None:
                raise InvalidState("该区域已有生效计划，需先撤回旧计划")
            self.connection.execute(
                "UPDATE recovery_plans SET state='issued',issued_at=?,revision=revision+1 "
                "WHERE plan_id=? AND revision=?",
                (self._now(), plan_id, expected_revision),
            )
            self._audit("plan", plan_id, "plan.issued", actor_id, {"revision": expected_revision + 1})
        return {"plan_id": plan_id, "state": "issued", "revision": expected_revision + 1}

    def revoke_plan(self, actor_id: str, plan_id: str, reason: str) -> dict[str, Any]:
        """撤回计划：立即收紧其下全部生效阶段。"""

        self._require(actor_id, "plan.issue")
        if not reason.strip():
            raise ValidationFailed("撤回原因不能为空")
        with transaction(self.connection, immediate=True):
            plan = self._get_plan(plan_id)
            if plan["state"] != "issued":
                raise InvalidState("只有生效中的计划可以撤回")
            self.connection.execute(
                "UPDATE recovery_plans SET state='revoked' WHERE plan_id=?", (plan_id,)
            )
            self._audit("plan", plan_id, "plan.revoked", actor_id, {"reason": reason.strip()})
            tightened = self._sweep_locked(actor=actor_id)
        return {"plan_id": plan_id, "state": "revoked", "tightened_activations": tightened}

    # ----------------------------------------------------------------- 复核

    def submit_review(
        self,
        actor_id: str,
        stage_id: str,
        evidence_id: str,
        evidence_version: int,
        confirmed_items: list[str],
        note: str = "",
    ) -> dict[str, Any]:
        """复核责任人对某证据版本的最低通过项逐项签字确认。"""

        user = self._require(actor_id, "review.write")
        stage = self._get_stage(stage_id)
        requirements = json.loads(stage["requirements_json"])
        target = next(
            (
                item for item in requirements["evidence"]
                if item["evidence_id"] == evidence_id and item["version"] == evidence_version
            ),
            None,
        )
        if target is None:
            raise ValidationFailed("该阶段不依赖此证据版本")
        if stage["reviewer_role"] == "coordinator" and user["role"] != "coordinator":
            raise Forbidden("该阶段必须由协调员复核")
        if stage["reviewer_id"] is not None and stage["reviewer_id"] != actor_id:
            raise Forbidden("该阶段指定了其他复核责任人")
        evidence = self.connection.execute(
            "SELECT state FROM evidence_versions WHERE evidence_id=? AND version=?",
            (evidence_id, evidence_version),
        ).fetchone()
        if evidence is None:
            raise NotFound("证据版本不存在")
        if evidence["state"] != "active":
            raise InvalidState("证据版本已失效，不能复核")
        minimum = list(target["minimum_items"])
        if not isinstance(confirmed_items, list) or not confirmed_items:
            raise ValidationFailed("confirmed_items 不能为空")
        cleaned = [str(item).strip() for item in confirmed_items if str(item).strip()]
        if set(cleaned) != set(minimum):
            raise ValidationFailed("必须对全部且仅最低通过项逐项确认")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO stage_reviews(stage_id,evidence_id,evidence_version,reviewer_id,"
                    "confirmed_items_json,note,created_at) VALUES(?,?,?,?,?,?,?)",
                    (stage_id, evidence_id, evidence_version, actor_id,
                     canonical_json(cleaned), note.strip(), self._now()),
                )
                review_id = int(cursor.lastrowid)
                self._audit(
                    "stage_review", str(review_id), "review.submitted", actor_id,
                    {"stage_id": stage_id, "evidence_id": evidence_id,
                     "evidence_version": evidence_version, "items": cleaned},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("该证据版本已完成复核") from exc
        return {"review_id": review_id, "stage_id": stage_id,
                "evidence_id": evidence_id, "evidence_version": evidence_version}

    # ----------------------------------------------------------- 生效与收紧

    def _sweep_locked(self, *, actor: str = SYSTEM_ACTOR) -> list[dict[str, Any]]:
        """在事务内把到期、证据失效、计划撤回、前置失效的生效指令收紧。

        反复扫描直到不动点，因为前置阶段收紧会级联到后续阶段。
        返回本次收紧的指令摘要列表，并逐条写入审计历史。
        """

        tightened: list[dict[str, Any]] = []
        now_text = self._now()
        while True:
            changed = False
            actives = self.connection.execute(
                "SELECT a.*, s.plan_id, s.ordinal, s.requirements_json, p.state AS plan_state "
                "FROM stage_activations a JOIN recovery_stages s ON s.stage_id=a.stage_id "
                "JOIN recovery_plans p ON p.plan_id=s.plan_id "
                "WHERE a.state='active' ORDER BY a.decided_at"
            ).fetchall()
            for activation in actives:
                reason = None
                if activation["plan_state"] == "revoked":
                    reason = "plan_revoked"
                elif activation["effective_until"] <= now_text:
                    reason = "expired"
                else:
                    snapshot = json.loads(activation["evidence_snapshot_json"])
                    for key in snapshot:
                        evidence_id, version_text = key.rsplit(":", 1)
                        evidence = self.connection.execute(
                            "SELECT state FROM evidence_versions WHERE evidence_id=? AND version=?",
                            (evidence_id, int(version_text)),
                        ).fetchone()
                        if evidence is None or evidence["state"] != "active":
                            reason = "evidence_lapsed"
                            break
                    if reason is None:
                        requirements = json.loads(activation["requirements_json"])
                        for prerequisite in requirements["requires_stages"]:
                            held = self.connection.execute(
                                "SELECT 1 FROM stage_activations a "
                                "JOIN recovery_stages s ON s.stage_id=a.stage_id "
                                "WHERE s.plan_id=? AND s.ordinal=? AND a.state='active' "
                                "AND a.effective_from<=? AND a.effective_until>? LIMIT 1",
                                (activation["plan_id"], prerequisite, now_text, now_text),
                            ).fetchone()
                            if held is None:
                                reason = "prerequisite_lapsed"
                                break
                if reason is None:
                    continue
                self.connection.execute(
                    "UPDATE stage_activations SET state=? WHERE activation_id=?",
                    (reason, activation["activation_id"]),
                )
                self._audit(
                    "activation", activation["activation_id"],
                    f"activation.{reason}", actor,
                    {"stage_id": activation["stage_id"], "decided_by": activation["decided_by"]},
                )
                tightened.append({
                    "activation_id": activation["activation_id"],
                    "stage_id": activation["stage_id"],
                    "state": reason,
                })
                changed = True
            if not changed:
                break
        return tightened

    def sweep(self, actor_id: str | None = None) -> dict[str, Any]:
        """显式执行一次到期/失效巡检。"""

        if actor_id is not None:
            self._require(actor_id, "report.read")
        with transaction(self.connection, immediate=True):
            tightened = self._sweep_locked(actor=actor_id or SYSTEM_ACTOR)
        return {"tightened_activations": tightened, "as_of": self._now()}

    def activate_stage(
        self,
        actor_id: str,
        stage_id: str,
        idempotency_key: str,
        note: str = "",
    ) -> dict[str, Any]:
        """发布阶段生效指令。前置条件不齐时拒绝扩大任何业务范围。"""

        self._require(actor_id, "activation.write")
        if not idempotency_key.strip():
            raise ValidationFailed("idempotency_key 不能为空")
        stage = self._get_stage(stage_id)
        plan = self._get_plan(stage["plan_id"])
        request_payload = {"stage_id": stage_id, "note": note.strip()}
        request_sha = digest(request_payload)
        with transaction(self.connection, immediate=True):
            self._sweep_locked()
            stored = self.connection.execute(
                "SELECT request_sha256,response_json FROM idempotency_keys "
                "WHERE scope='activation' AND key=?",
                (idempotency_key.strip(),),
            ).fetchone()
            if stored is not None:
                if stored["request_sha256"] != request_sha:
                    raise Conflict("幂等键对应不同的生效指令")
                original = json.loads(stored["response_json"])
                replay_id = "act-" + uuid.uuid4().hex[:18]
                self.connection.execute(
                    "INSERT INTO stage_activations(activation_id,stage_id,plan_revision,state,"
                    "effective_from,effective_until,decided_by,decided_at,review_note,"
                    "evidence_snapshot_json,idempotency_key,replay_of) "
                    "SELECT ?,stage_id,plan_revision,'replayed',effective_from,effective_until,"
                    "?,?,review_note,evidence_snapshot_json,idempotency_key,activation_id "
                    "FROM stage_activations WHERE activation_id=?",
                    (replay_id, actor_id, self._now(), original["activation_id"]),
                )
                self._audit(
                    "activation", replay_id, "activation.replayed", actor_id,
                    {"stage_id": stage_id, "replay_of": original["activation_id"],
                     "idempotency_key": idempotency_key.strip()},
                )
                return {**original, "replayed": True, "replay_activation_id": replay_id}

            if plan["state"] != "issued":
                raise InvalidState("计划尚未生效，不能激活阶段")
            requirements = json.loads(stage["requirements_json"])
            channels = json.loads(stage["channels_json"])
            now_text = self._now()
            # 前置阶段必须在当前时刻仍处生效期内
            for prerequisite in requirements["requires_stages"]:
                held = self.connection.execute(
                    "SELECT a.activation_id FROM stage_activations a "
                    "JOIN recovery_stages s ON s.stage_id=a.stage_id "
                    "WHERE s.plan_id=? AND s.ordinal=? AND a.state='active' "
                    "AND a.effective_from<=? AND a.effective_until>? LIMIT 1",
                    (plan["plan_id"], prerequisite, now_text, now_text),
                ).fetchone()
                if held is None:
                    raise InvalidState(f"前置阶段 {prerequisite} 未生效或已失效，不得扩大业务范围")
            # 证据版本有效，且最低通过项由指定复核责任人逐项签字
            evidence_snapshot: dict[str, str] = {}
            review_rows: list[dict[str, Any]] = []
            for requirement in requirements["evidence"]:
                evidence = self.connection.execute(
                    "SELECT content_sha256,state FROM evidence_versions "
                    "WHERE evidence_id=? AND version=?",
                    (requirement["evidence_id"], requirement["version"]),
                ).fetchone()
                if evidence is None:
                    raise InvalidState(
                        f"证据版本 {requirement['evidence_id']}:{requirement['version']} 不存在"
                    )
                if evidence["state"] != "active":
                    raise InvalidState(
                        f"证据版本 {requirement['evidence_id']}:{requirement['version']} 已失效"
                    )
                review = self.connection.execute(
                    "SELECT * FROM stage_reviews WHERE stage_id=? AND evidence_id=? "
                    "AND evidence_version=?",
                    (stage_id, requirement["evidence_id"], requirement["version"]),
                ).fetchone()
                if review is None:
                    raise InvalidState(
                        f"证据 {requirement['evidence_id']}:{requirement['version']} "
                        "尚未完成最低通过项复核"
                    )
                confirmed = set(json.loads(review["confirmed_items_json"]))
                if confirmed != set(requirement["minimum_items"]):
                    raise InvalidState("复核确认项与最低通过项不一致")
                key = f"{requirement['evidence_id']}:{requirement['version']}"
                evidence_snapshot[key] = evidence["content_sha256"]
                review_rows.append({
                    "review_id": review["review_id"],
                    "evidence": key,
                    "reviewer_id": review["reviewer_id"],
                })
            # 同一阶段同时只允许一条生效指令（到期/撤回后可重新发布）
            duplicate = self.connection.execute(
                "SELECT activation_id FROM stage_activations WHERE stage_id=? AND state='active'",
                (stage_id,),
            ).fetchone()
            if duplicate is not None:
                raise Conflict("该阶段已有生效指令，重复执行请使用同一幂等键")

            activation_id = "act-" + uuid.uuid4().hex[:18]
            effective_from = self.clock.now()
            effective_until = effective_from + timedelta(hours=int(stage["validity_hours"]))
            from_text, until_text = utc_text(effective_from), utc_text(effective_until)
            self.connection.execute(
                "INSERT INTO stage_activations(activation_id,stage_id,plan_revision,state,"
                "effective_from,effective_until,decided_by,decided_at,review_note,"
                "evidence_snapshot_json,idempotency_key) VALUES(?,?,?,'active',?,?,?,?,?,?,?)",
                (activation_id, stage_id, plan["revision"], from_text, until_text,
                 actor_id, now_text, note.strip(),
                 canonical_json(evidence_snapshot), idempotency_key.strip()),
            )
            self.connection.execute(
                "INSERT INTO idempotency_keys(scope,key,request_sha256,response_json,created_at) "
                "VALUES('activation',?,?,?,?)",
                (idempotency_key.strip(), request_sha, "", self._now()),
            )
            response = {
                "activation_id": activation_id,
                "stage_id": stage_id,
                "plan_id": plan["plan_id"],
                "state": "active",
                "grants_state": stage["grants_state"],
                "channels": channels,
                "effective_from": from_text,
                "effective_until": until_text,
                "evidence_snapshot": evidence_snapshot,
            }
            self.connection.execute(
                "UPDATE idempotency_keys SET response_json=? WHERE scope='activation' AND key=?",
                (canonical_json(response), idempotency_key.strip()),
            )
            self._audit(
                "activation", activation_id, "activation.issued", actor_id,
                {"stage_id": stage_id, "plan_revision": plan["revision"],
                 "effective_from": from_text, "effective_until": until_text,
                 "channels": channels, "grants_state": stage["grants_state"],
                 "evidence_snapshot": evidence_snapshot, "reviews": review_rows,
                 "idempotency_key": idempotency_key.strip()},
            )
        return {**response, "replayed": False}

    def revoke_activation(self, actor_id: str, activation_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "activation.write")
        if not reason.strip():
            raise ValidationFailed("撤回原因不能为空")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT state,stage_id FROM stage_activations WHERE activation_id=?",
                (activation_id,),
            ).fetchone()
            if row is None:
                raise NotFound("生效指令不存在")
            if row["state"] != "active":
                raise InvalidState("只有生效中的指令可以撤回")
            self.connection.execute(
                "UPDATE stage_activations SET state='revoked' WHERE activation_id=?",
                (activation_id,),
            )
            self._audit(
                "activation", activation_id, "activation.manual_revoked", actor_id,
                {"stage_id": row["stage_id"], "reason": reason.strip()},
            )
            self._sweep_locked(actor=actor_id)
        return {"activation_id": activation_id, "state": "revoked"}

    # ------------------------------------------------------------- 现场执行

    def record_execution(
        self,
        actor_id: str,
        activation_id: str,
        channel: str,
        detail: Mapping[str, Any],
    ) -> dict[str, Any]:
        """登记现场实际执行；通道此刻未被阶段投影放行则拒绝。"""

        self._require(actor_id, "execution.write")
        if channel not in {"patrol", "research", "visitor", "operations"}:
            raise ValidationFailed("channel 必须是 patrol、research、visitor、operations")
        if not isinstance(detail, Mapping) or not detail:
            raise ValidationFailed("detail 必须是非空执行详情对象")
        with transaction(self.connection, immediate=True):
            self._sweep_locked()
            activation = self.connection.execute(
                "SELECT a.*,s.channels_json FROM stage_activations a "
                "JOIN recovery_stages s ON s.stage_id=a.stage_id "
                "WHERE a.activation_id=?",
                (activation_id,),
            ).fetchone()
            if activation is None:
                raise NotFound("生效指令不存在")
            if activation["state"] != "active":
                raise InvalidState("指令已收紧，不能据此开展现场业务")
            now_text = self._now()
            if not (activation["effective_from"] <= now_text < activation["effective_until"]):
                raise InvalidState("不在指令生效期内")
            if channel not in json.loads(activation["channels_json"]):
                raise Forbidden("该业务接口未获本阶段放行")
            execution_id = "exe-" + uuid.uuid4().hex[:18]
            self.connection.execute(
                "INSERT INTO field_executions(execution_id,activation_id,channel,executed_by,"
                "detail_json,executed_at) VALUES(?,?,?,?,?,?)",
                (execution_id, activation_id, channel, actor_id,
                 canonical_json(dict(detail)), now_text),
            )
            self._audit(
                "field_execution", execution_id, "execution.recorded", actor_id,
                {"activation_id": activation_id, "channel": channel},
            )
        return {"execution_id": execution_id, "activation_id": activation_id, "channel": channel}

    # ----------------------------------------------------------------- 投影

    def _active_activations(self, plan_id: str, now_text: str) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT a.*,s.ordinal,s.name AS stage_name,s.grants_state,s.channels_json "
            "FROM stage_activations a JOIN recovery_stages s ON s.stage_id=a.stage_id "
            "WHERE s.plan_id=? AND a.state='active' AND a.effective_from<=? "
            "AND a.effective_until>? ORDER BY s.ordinal",
            (plan_id, now_text, now_text),
        ).fetchall())

    def _projection_locked(self, area: sqlite3.Row, now_text: str) -> dict[str, Any]:
        """调用方必须已开启事务并完成本轮收紧巡检。"""

        plan = self.connection.execute(
            "SELECT * FROM recovery_plans WHERE area_id=? ORDER BY state='issued' DESC,"
            " issued_at DESC LIMIT 1",
            (area["area_id"],),
        ).fetchone()
        channels = {name: {"granted": False} for name in
                   ("patrol", "research", "visitor", "operations")}
        stages_view: list[dict[str, Any]] = []
        public_state = "closed"
        plan_view = None
        if plan is not None:
            plan_view = {"plan_id": plan["plan_id"], "state": plan["state"],
                         "revision": plan["revision"]}
            actives = self._active_activations(plan["plan_id"], now_text)
            for activation in actives:
                if STATE_RANK[activation["grants_state"]] > STATE_RANK[public_state]:
                    public_state = activation["grants_state"]
                for channel in json.loads(activation["channels_json"]):
                    channels[channel] = {
                        "granted": True,
                        "stage_id": activation["stage_id"],
                        "stage_name": activation["stage_name"],
                        "activation_id": activation["activation_id"],
                        "effective_from": activation["effective_from"],
                        "effective_until": activation["effective_until"],
                    }
            stage_rows = self.connection.execute(
                "SELECT * FROM recovery_stages WHERE plan_id=? ORDER BY ordinal",
                (plan["plan_id"],),
            ).fetchall()
            for stage in stage_rows:
                latest = self.connection.execute(
                    "SELECT * FROM stage_activations WHERE stage_id=? "
                    "ORDER BY decided_at DESC, rowid DESC LIMIT 1",
                    (stage["stage_id"],),
                ).fetchone()
                stages_view.append({
                    "ordinal": stage["ordinal"],
                    "stage_id": stage["stage_id"],
                    "name": stage["name"],
                    "grants_state": stage["grants_state"],
                    "channels": json.loads(stage["channels_json"]),
                    "activation_state": None if latest is None else latest["state"],
                    "effective_until": None if latest is None or latest["state"] != "active"
                    else latest["effective_until"],
                })
        self.connection.execute(
            "UPDATE affected_areas SET public_state=? WHERE area_id=?",
            (public_state, area["area_id"]),
        )
        return {
            "area_id": area["area_id"],
            "scope": area["scope"],
            "as_of": now_text,
            "plan": plan_view,
            "public_state": public_state,
            "channels": channels,
            "stages": stages_view,
        }

    def projection(self, actor_id: str, area_id: str) -> dict[str, Any]:
        """巡护、科研、游客、经营四类接口共享的唯一阶段投影。"""

        self._require(actor_id, "report.read")
        area = self.connection.execute(
            "SELECT * FROM affected_areas WHERE area_id=?", (area_id,)
        ).fetchone()
        if area is None:
            raise NotFound("受影响区域不存在")
        with transaction(self.connection, immediate=True):
            self._sweep_locked()
            return self._projection_locked(area, self._now())

    def channel_projection(self, actor_id: str, channel: str) -> dict[str, Any]:
        """某一业务接口（巡护/科研/游客/经营）读取的跨区域投影。"""

        self._require(actor_id, "report.read")
        if channel not in {"patrol", "research", "visitor", "operations"}:
            raise ValidationFailed("channel 必须是 patrol、research、visitor、operations")
        with transaction(self.connection, immediate=True):
            self._sweep_locked()
            now_text = self._now()
            areas = self.connection.execute(
                "SELECT * FROM affected_areas ORDER BY area_id"
            ).fetchall()
            entries = []
            for area in areas:
                view = self._projection_locked(area, now_text)
                grant = view["channels"][channel]
                entries.append({
                    "area_id": area["area_id"],
                    "granted": grant["granted"],
                    **({"activation_id": grant["activation_id"],
                        "effective_until": grant["effective_until"]} if grant["granted"] else {}),
                })
        return {"channel": channel, "as_of": now_text, "areas": entries}

    def public_status(self) -> dict[str, Any]:
        """公众视图：仅返回必要的开放状态，不含证据、阶段与审计细节。"""

        with transaction(self.connection, immediate=True):
            self._sweep_locked()
            now_text = self._now()
            rows = self.connection.execute(
                "SELECT * FROM affected_areas ORDER BY area_id"
            ).fetchall()
            areas = []
            for row in rows:
                view = self._projection_locked(row, now_text)
                areas.append({
                    "area_id": row["area_id"],
                    "name": row["name"],
                    "scope": row["scope"],
                    "scope_label": SCOPE_LABELS[row["scope"]],
                    "public_state": view["public_state"],
                    "public_state_label": STATE_NAMES[view["public_state"]],
                })
        return {"as_of": now_text, "areas": areas}

    # ----------------------------------------------------------------- 审计

    def decision_record(self, actor_id: str, activation_id: str) -> dict[str, Any]:
        """审计重建：某次决定依赖的证据版本、复核、收紧原因与现场执行。"""

        self._require(actor_id, "audit.read")
        activation = self.connection.execute(
            "SELECT a.*,s.ordinal,s.name AS stage_name,s.grants_state,s.channels_json,"
            "s.requirements_json,s.reviewer_role,s.reviewer_id,p.area_id,p.plan_id "
            "FROM stage_activations a JOIN recovery_stages s ON s.stage_id=a.stage_id "
            "JOIN recovery_plans p ON p.plan_id=s.plan_id WHERE a.activation_id=?",
            (activation_id,),
        ).fetchone()
        if activation is None:
            raise NotFound("生效指令不存在")
        requirements = json.loads(activation["requirements_json"])
        evidence_basis = []
        for key, sha in json.loads(activation["evidence_snapshot_json"]).items():
            evidence_id, version_text = key.rsplit(":", 1)
            row = self.connection.execute(
                "SELECT version,title,state,submitted_by,submitted_at,invalidate_reason "
                "FROM evidence_versions WHERE evidence_id=? AND version=?",
                (evidence_id, int(version_text)),
            ).fetchone()
            review = self.connection.execute(
                "SELECT review_id,reviewer_id,confirmed_items_json,created_at "
                "FROM stage_reviews WHERE stage_id=? AND evidence_id=? AND evidence_version=?",
                (activation["stage_id"], evidence_id, int(version_text)),
            ).fetchone()
            evidence_basis.append({
                "evidence": key,
                "decided_sha256": sha,
                "current_state": None if row is None else row["state"],
                "title": None if row is None else row["title"],
                "invalidate_reason": None if row is None else row["invalidate_reason"],
                "review": None if review is None else {
                    "review_id": review["review_id"],
                    "reviewer_id": review["reviewer_id"],
                    "confirmed_items": json.loads(review["confirmed_items_json"]),
                    "reviewed_at": review["created_at"],
                },
            })
        executions = [
            {
                "execution_id": row["execution_id"],
                "channel": row["channel"],
                "executed_by": row["executed_by"],
                "detail": json.loads(row["detail_json"]),
                "executed_at": row["executed_at"],
            }
            for row in self.connection.execute(
                "SELECT * FROM field_executions WHERE activation_id=? ORDER BY executed_at,execution_id",
                (activation_id,),
            ).fetchall()
        ]
        timeline = [
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in self.connection.execute(
                "SELECT * FROM audit_events WHERE entity_type='activation' AND entity_id=? "
                "ORDER BY event_id",
                (activation_id,),
            ).fetchall()
        ]
        return {
            "activation_id": activation_id,
            "area_id": activation["area_id"],
            "plan_id": activation["plan_id"],
            "plan_revision_at_decision": activation["plan_revision"],
            "stage": {
                "stage_id": activation["stage_id"],
                "ordinal": activation["ordinal"],
                "name": activation["stage_name"],
                "grants_state": activation["grants_state"],
                "channels": json.loads(activation["channels_json"]),
                "reviewer_role": activation["reviewer_role"],
                "reviewer_id": activation["reviewer_id"],
            },
            "requirements": requirements,
            "state": activation["state"],
            "effective_from": activation["effective_from"],
            "effective_until": activation["effective_until"],
            "decided_by": activation["decided_by"],
            "decided_at": activation["decided_at"],
            "review_note": activation["review_note"],
            "idempotency_key": activation["idempotency_key"],
            "replay_of": activation["replay_of"],
            "evidence_basis": evidence_basis,
            "field_executions": executions,
            "timeline": timeline,
        }

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM audit_events ORDER BY event_id").fetchall()
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
            calculated = digest(body)
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
