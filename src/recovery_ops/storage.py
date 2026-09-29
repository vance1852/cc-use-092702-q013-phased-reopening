"""灾后分阶段恢复编排服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS recovery_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('coordinator','surveyor','reviewer','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery_plans (
    plan_id TEXT PRIMARY KEY,
    zone_id TEXT NOT NULL,
    title TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','active','retired')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    created_by TEXT NOT NULL REFERENCES recovery_users(user_id),
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_plan_per_zone
ON recovery_plans(zone_id) WHERE state='active';

CREATE TABLE IF NOT EXISTS recovery_phases (
    plan_id TEXT NOT NULL REFERENCES recovery_plans(plan_id),
    phase_key TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    name TEXT NOT NULL,
    scopes_json TEXT NOT NULL,
    evidence_requirements_json TEXT NOT NULL,
    reviewer_role TEXT NOT NULL,
    minimum_pass_items_json TEXT NOT NULL,
    PRIMARY KEY(plan_id, phase_key),
    UNIQUE(plan_id, sequence)
);

CREATE TABLE IF NOT EXISTS recovery_evidence (
    evidence_id TEXT PRIMARY KEY,
    zone_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
    note TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'valid' CHECK(state IN ('valid','invalidated')),
    submitted_by TEXT NOT NULL REFERENCES recovery_users(user_id),
    submitted_at TEXT NOT NULL,
    invalidated_by TEXT REFERENCES recovery_users(user_id),
    invalidated_at TEXT,
    invalidation_reason TEXT,
    UNIQUE(zone_id, kind, version)
);

CREATE TABLE IF NOT EXISTS phase_reviews (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES recovery_plans(plan_id),
    phase_key TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
    note TEXT NOT NULL DEFAULT '',
    reviewer_id TEXT NOT NULL REFERENCES recovery_users(user_id),
    reviewer_role TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_phase_reviews_phase
ON phase_reviews(plan_id, phase_key, review_id);

CREATE TABLE IF NOT EXISTS recovery_directives (
    directive_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES recovery_plans(plan_id),
    phase_key TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    effective_from TEXT NOT NULL,
    effective_until TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active'
        CHECK(state IN ('active','withdrawn','expired','tightened')),
    basis_json TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256) = 64),
    response_json TEXT NOT NULL,
    issued_by TEXT NOT NULL REFERENCES recovery_users(user_id),
    issued_at TEXT NOT NULL,
    closed_by TEXT,
    closed_at TEXT,
    close_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_directives_projection
ON recovery_directives(plan_id, state, effective_from, effective_until);

CREATE TABLE IF NOT EXISTS directive_evidence (
    directive_id INTEGER NOT NULL REFERENCES recovery_directives(directive_id),
    evidence_id TEXT NOT NULL REFERENCES recovery_evidence(evidence_id),
    kind TEXT NOT NULL,
    version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
    PRIMARY KEY(directive_id, evidence_id)
);

CREATE INDEX IF NOT EXISTS idx_directive_evidence_evidence
ON directive_evidence(evidence_id);

CREATE TABLE IF NOT EXISTS field_confirmations (
    confirmation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES recovery_plans(plan_id),
    phase_key TEXT NOT NULL,
    item_key TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    confirmed_by TEXT NOT NULL REFERENCES recovery_users(user_id),
    confirmed_at TEXT NOT NULL,
    UNIQUE(plan_id, phase_key, item_key)
);

CREATE TABLE IF NOT EXISTS recovery_audit_events (
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

CREATE INDEX IF NOT EXISTS idx_recovery_audit_entity
ON recovery_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # ThreadingHTTPServer 在工作线程中处理请求，连接需要跨线程共享；
    # 所有写操作都在 BEGIN IMMEDIATE 事务内串行执行。
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
