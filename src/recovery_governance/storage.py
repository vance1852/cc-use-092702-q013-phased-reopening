"""灾后恢复治理服务的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 管理与复核人员
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('coordinator','reviewer','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL
);

-- 受影响区域：巡护道路、游客步道、科研样地、住宿区等
CREATE TABLE IF NOT EXISTS affected_areas (
    area_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('patrol','visitor','research','lodging')),
    -- 公众开放状态投影：closed 关闭 / limited 受控开放 / open 开放
    public_state TEXT NOT NULL DEFAULT 'closed'
        CHECK (public_state IN ('closed','limited','open')),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

-- 证据版本（检测报告、边坡复核、烟气监测、供水通信检查等）
CREATE TABLE IF NOT EXISTS evidence_versions (
    evidence_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    title TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    canonical_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    -- 证据生命周期：active 有效 / invalidated 失效（撤回、过期、新证据推翻）
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','invalidated')),
    submitted_by TEXT NOT NULL REFERENCES users(user_id),
    submitted_at TEXT NOT NULL,
    invalidated_at TEXT,
    invalidate_reason TEXT,
    PRIMARY KEY (evidence_id, version)
);

CREATE INDEX IF NOT EXISTS idx_evidence_state
ON evidence_versions(state, submitted_at);

-- 分阶段恢复计划（属于某个受影响区域）
CREATE TABLE IF NOT EXISTS recovery_plans (
    plan_id TEXT PRIMARY KEY,
    area_id TEXT NOT NULL REFERENCES affected_areas(area_id),
    title TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    -- draft 草稿 / issued 已发布生效 / revoked 已撤回
    state TEXT NOT NULL DEFAULT 'draft' CHECK (state IN ('draft','issued','revoked')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    issued_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_plans_area
ON recovery_plans(area_id, state);

-- 阶段复核确认：复核责任人对某个证据版本的最低通过项逐项签字
CREATE TABLE IF NOT EXISTS stage_reviews (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage_id TEXT NOT NULL REFERENCES recovery_stages(stage_id),
    evidence_id TEXT NOT NULL,
    evidence_version INTEGER NOT NULL,
    reviewer_id TEXT NOT NULL REFERENCES users(user_id),
    confirmed_items_json TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (stage_id, evidence_id, evidence_version),
    FOREIGN KEY (evidence_id, evidence_version) REFERENCES evidence_versions(evidence_id, version)
);

-- 恢复阶段定义
CREATE TABLE IF NOT EXISTS recovery_stages (
    stage_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES recovery_plans(plan_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    name TEXT NOT NULL,
    -- 该阶段达成后向公众呈现的开放级别
    grants_state TEXT NOT NULL CHECK (grants_state IN ('closed','limited','open')),
    -- 生效期（阶段激活后多久内有效，小时）；到期自动收紧
    validity_hours INTEGER NOT NULL CHECK (validity_hours > 0),
    -- 该阶段放行的业务接口：patrol 巡护 / research 科研 / visitor 游客 / operations 经营
    channels_json TEXT NOT NULL,
    -- 依赖的证据版本 + 最低通过项（JSON）
    requirements_json TEXT NOT NULL,
    -- 复核责任
    reviewer_role TEXT NOT NULL CHECK (reviewer_role IN ('reviewer','coordinator')),
    reviewer_id TEXT REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE (plan_id, ordinal)
);

-- 幂等键
CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);

-- 阶段生效决定（指令）。同一阶段同时只有一条 active 记录。
CREATE TABLE IF NOT EXISTS stage_activations (
    activation_id TEXT PRIMARY KEY,
    stage_id TEXT NOT NULL REFERENCES recovery_stages(stage_id),
    plan_revision INTEGER NOT NULL,
    -- active 生效中 / replayed 重复执行回放 / revoked 指令撤回 / expired 到期
    -- / evidence_lapsed 证据失效 / prerequisite_lapsed 前置阶段失效 / plan_revoked 计划撤回
    state TEXT NOT NULL
        CHECK (state IN ('active','replayed','revoked','expired','evidence_lapsed',
                         'prerequisite_lapsed','plan_revoked')),
    effective_from TEXT NOT NULL,
    effective_until TEXT NOT NULL,
    decided_by TEXT NOT NULL REFERENCES users(user_id),
    decided_at TEXT NOT NULL,
    review_note TEXT NOT NULL,
    -- 决定时核验通过的证据快照（evidence 键 -> sha256），供审计重建
    evidence_snapshot_json TEXT NOT NULL,
    idempotency_key TEXT,
    replay_of TEXT REFERENCES stage_activations(activation_id)
);

CREATE INDEX IF NOT EXISTS idx_activations_stage
ON stage_activations(stage_id, state, decided_at);

-- 现场执行记录（每个激活决定下各业务接口的实际执行）
CREATE TABLE IF NOT EXISTS field_executions (
    execution_id TEXT PRIMARY KEY,
    activation_id TEXT NOT NULL REFERENCES stage_activations(activation_id),
    channel TEXT NOT NULL CHECK (channel IN ('patrol','research','visitor','operations')),
    executed_by TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    executed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_executions_activation
ON field_executions(activation_id);

-- 哈希链审计事件
CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_entity
ON audit_events(entity_type, entity_id, event_id);
"""

REQUIRED_TABLES = frozenset({
    "schema_meta", "users", "affected_areas", "evidence_versions", "recovery_plans",
    "recovery_stages", "stage_reviews", "idempotency_keys", "stage_activations",
    "field_executions", "audit_events",
})


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接并启用严格的事务与外键设置。"""

    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    initialize(connection)
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    """显式事务；异常时保证回滚。"""

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize(connection: sqlite3.Connection) -> None:
    """初始化基础资料表，重复执行不改变已有数据。"""

    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    """返回适合机器检查的数据库结构摘要。"""

    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    missing = sorted(REQUIRED_TABLES - set(tables))
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    return {
        "tables": tables,
        "missing_tables": missing,
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(foreign_keys),
    }
