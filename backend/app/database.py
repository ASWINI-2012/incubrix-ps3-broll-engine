from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def database_path() -> Path:
    return Path(os.getenv("DATABASE_PATH", "./data/jobs.sqlite3")).resolve()


def connect() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def initialize() -> None:
    with connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                progress INTEGER NOT NULL,
                current_stage TEXT NOT NULL,
                input_json TEXT NOT NULL,
                result_json TEXT,
                error_json TEXT,
                timeline_json TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS idempotency (
                idempotency_key TEXT PRIMARY KEY,
                request_hash TEXT NOT NULL,
                job_id TEXT NOT NULL REFERENCES jobs(job_id)
            );
            CREATE TABLE IF NOT EXISTS provider_search_cache (
                cache_key TEXT PRIMARY KEY,
                response_json TEXT NOT NULL,
                expires_at REAL NOT NULL
            );
            """
        )
        connection.execute(
            """UPDATE jobs
               SET status = 'failed', current_stage = 'interrupted',
                   error_json = ?, completed_at = ?
               WHERE status IN ('queued', 'processing')""",
            (
                json.dumps(
                    {
                        "code": "worker_interrupted",
                        "message": "The server restarted before this job completed.",
                    }
                ),
                utc_now(),
            ),
        )


def create_job(
    job_id: str,
    request_hash: str,
    input_data: dict[str, Any],
    idempotency_key: str | None,
) -> tuple[dict[str, Any], bool]:
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if idempotency_key:
            existing = connection.execute(
                """SELECT i.request_hash, j.* FROM idempotency i
                   JOIN jobs j ON j.job_id = i.job_id WHERE i.idempotency_key = ?""",
                (idempotency_key,),
            ).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise ValueError(
                        "This Idempotency-Key was already used with a different request."
                    )
                return row_to_dict(existing), False

        created_at = utc_now()
        connection.execute(
            """INSERT INTO jobs
               (job_id, status, progress, current_stage, input_json, created_at)
               VALUES (?, 'queued', 0, 'queued', ?, ?)""",
            (job_id, json.dumps(input_data), created_at),
        )
        if idempotency_key:
            connection.execute(
                """INSERT INTO idempotency (idempotency_key, request_hash, job_id)
                   VALUES (?, ?, ?)""",
                (idempotency_key, request_hash, job_id),
            )
        record = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return row_to_dict(record), True


def row_to_dict(record: sqlite3.Row) -> dict[str, Any]:
    return {
        "job_id": record["job_id"],
        "status": record["status"],
        "progress": record["progress"],
        "current_stage": record["current_stage"],
        "input": json.loads(record["input_json"]),
        "result": json.loads(record["result_json"]) if record["result_json"] else None,
        "error": json.loads(record["error_json"]) if record["error_json"] else None,
        "timeline": json.loads(record["timeline_json"]) if record["timeline_json"] else None,
        "created_at": record["created_at"],
        "completed_at": record["completed_at"],
    }


def get_job(job_id: str) -> dict[str, Any] | None:
    with connect() as connection:
        record = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return row_to_dict(record) if record else None


def update_job(job_id: str, **fields: Any) -> None:
    allowed = {
        "status",
        "progress",
        "current_stage",
        "result_json",
        "error_json",
        "timeline_json",
        "completed_at",
    }
    if not fields or not set(fields).issubset(allowed):
        raise ValueError("Unsupported job update fields")
    assignments = ", ".join(f"{field} = ?" for field in fields)
    with connect() as connection:
        connection.execute(
            f"UPDATE jobs SET {assignments} WHERE job_id = ?",
            (*fields.values(), job_id),
        )


def encode_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def get_provider_cache(cache_key: str) -> Any | None:
    with connect() as connection:
        record = connection.execute(
            "SELECT response_json FROM provider_search_cache WHERE cache_key = ? AND expires_at > ?",
            (cache_key, time.time()),
        ).fetchone()
    return json.loads(record["response_json"]) if record else None


def set_provider_cache(cache_key: str, response: Any, lifetime_seconds: int) -> None:
    with connect() as connection:
        connection.execute(
            """INSERT INTO provider_search_cache (cache_key, response_json, expires_at)
               VALUES (?, ?, ?)
               ON CONFLICT(cache_key) DO UPDATE SET
                   response_json = excluded.response_json,
                   expires_at = excluded.expires_at""",
            (cache_key, json.dumps(response), time.time() + lifetime_seconds),
        )
