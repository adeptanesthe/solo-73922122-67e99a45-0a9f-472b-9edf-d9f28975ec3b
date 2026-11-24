"""Data-access helpers.  All functions take the caller's connection so the
service layer can keep checks and writes in one immediate transaction."""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Sequence

from .codes import chunk_signatures, query_chunk_signatures


# ---------------------------------------------------------------- boxes

def insert_box(
    conn: sqlite3.Connection,
    box_number: str,
    flat: Sequence[int | None],
) -> None:
    from .codes import grid_key

    conn.execute(
        "INSERT INTO boxes (box_number, grid_key, grid_json) VALUES (?, ?, ?)",
        (box_number, grid_key(flat), json.dumps(list(flat))),
    )


def box_number_exists(conn: sqlite3.Connection, box_number: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM boxes WHERE box_number = ?", (box_number,)
    ).fetchone() is not None


def get_box(conn: sqlite3.Connection, box_number: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM boxes WHERE box_number = ?", (box_number,)).fetchone()


def list_boxes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM boxes ORDER BY box_number").fetchall())


def count_boxes(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM boxes").fetchone()[0])


def next_box_number(conn: sqlite3.Connection) -> str:
    n = conn.execute("SELECT COALESCE(MAX(rowid), 0) + 1 FROM boxes").fetchone()[0]
    while True:
        candidate = f"BX-{n:04d}"
        if not box_number_exists(conn, candidate):
            return candidate
        n += 1


def flat_of_box(row: sqlite3.Row) -> tuple[int | None, ...]:
    return tuple(v if v is not None else None for v in json.loads(row["grid_json"]))


# ------------------------------------------------------------- readings

def find_reading_by_grid_key(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM readings WHERE grid_key = ?", (key,)).fetchone()


def get_reading_by_id(conn: sqlite3.Connection, reading_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM readings WHERE id = ?", (reading_id,)).fetchone()


def count_readings(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0])


def get_readings_by_ids(
    conn: sqlite3.Connection, reading_ids: set[int] | list[int]
) -> list[sqlite3.Row]:
    """Batch primary-key fetch of index candidates (never a full table scan)."""
    ids = list(reading_ids)
    rows: list[sqlite3.Row] = []
    for start in range(0, len(ids), 400):
        batch = ids[start:start + 400]
        placeholders = ",".join("?" for _ in batch)
        rows.extend(
            conn.execute(
                f"SELECT * FROM readings WHERE id IN ({placeholders})", batch
            ).fetchall()
        )
    return rows


def get_reading_by_no(conn: sqlite3.Connection, reading_no: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM readings WHERE reading_no = ?", (reading_no,)).fetchone()


def list_readings(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM readings ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    )


def readings_for_box(conn: sqlite3.Connection, box_number: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM readings WHERE box_number = ? ORDER BY id", (box_number,)
        ).fetchall()
    )


def next_reading_number(conn: sqlite3.Connection) -> tuple[int, str]:
    next_id = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM readings").fetchone()[0]
    return int(next_id), f"RD-{int(next_id):04d}"


def insert_reading(
    conn: sqlite3.Connection,
    *,
    reading_no: str,
    grid_key: str,
    flat: Sequence[int | None],
    box_number: str,
    orientation: int,
    mismatches: int,
    visible: int,
    scanner_id: str | None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO readings
            (reading_no, grid_key, grid_json, box_number,
             orientation, mismatches, visible, scanner_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            reading_no,
            grid_key,
            json.dumps(list(flat)),
            box_number,
            orientation,
            mismatches,
            visible,
            scanner_id,
        ),
    )
    reading_id = int(cur.lastrowid)
    rows = chunk_signatures(flat)
    conn.executemany(
        "INSERT OR IGNORE INTO reading_chunks (chunk_index, bits, reading_id) VALUES (?, ?, ?)",
        [(ci, bits, reading_id) for ci, bits in rows],
    )
    return reading_id


def flat_of_reading(row: sqlite3.Row) -> tuple[int | None, ...]:
    return tuple(json.loads(row["grid_json"]))


# ------------------------------------------------------ chunk index query

def candidate_reading_ids_for_tag(
    conn: sqlite3.Connection, tag_flat: Sequence[int | None]
) -> set[int]:
    """Indexed retrieval of readings that *might* hit the complete tag.

    Runs one index-backed query per chunk (six at most).  It never scans the
    readings table; callers must exact-verify returned ids with
    :func:`app.codes.match_grid`.
    """
    ids: set[int] = set()
    for chunk_index, bits in query_chunk_signatures(tag_flat):
        rows = conn.execute(
            """
            SELECT reading_id FROM reading_chunks
            WHERE chunk_index = ? AND bits = ?
            """,
            (chunk_index, bits),
        ).fetchall()
        ids.update(int(r[0]) for r in rows)
    return ids


def chunk_index_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    readings = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    chunks = conn.execute("SELECT COUNT(*) FROM reading_chunks").fetchone()[0]
    boxes = conn.execute("SELECT COUNT(*) FROM boxes").fetchone()[0]
    return {"boxes": int(boxes), "confirmed_readings": int(readings), "chunk_rows": int(chunks)}


# -------------------------------------------------------- fixture plans

def next_plan_number(conn: sqlite3.Connection) -> str:
    next_id = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM fixture_plans").fetchone()[0]
    return f"PLAN-{int(next_id):04d}"


def plan_number_exists(conn: sqlite3.Connection, plan_no: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM fixture_plans WHERE plan_no = ?", (plan_no,)
    ).fetchone() is not None


def insert_plan(
    conn: sqlite3.Connection,
    *,
    plan_no: str,
    status: str,
    feasible: bool,
    mode: str,
    blocked: Sequence[int],
    openable: Sequence[int],
    holes: Sequence[int],
    remaining: Sequence[int],
    min_holes: int | None,
    reason: str | None,
    report: dict[str, Any],
    request: dict[str, Any] | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO fixture_plans
            (plan_no, status, feasible, mode, blocked_json, openable_json,
             holes_json, remaining_json, min_holes, reason, report_json, request_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            plan_no, status, 1 if feasible else 0, mode,
            json.dumps(list(blocked)), json.dumps(list(openable)),
            json.dumps(list(holes)), json.dumps(list(remaining)),
            min_holes, reason, json.dumps(report, ensure_ascii=False),
            json.dumps(request or {}, ensure_ascii=False),
        ),
    )
    return int(cur.lastrowid)


def get_plan_by_no(conn: sqlite3.Connection, plan_no: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM fixture_plans WHERE plan_no = ?", (plan_no,)).fetchone()


def get_plan_by_id(conn: sqlite3.Connection, plan_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM fixture_plans WHERE id = ?", (plan_id,)).fetchone()


def find_plan(conn: sqlite3.Connection, identifier: str | int) -> sqlite3.Row | None:
    if isinstance(identifier, int) or (isinstance(identifier, str) and identifier.isdigit()):
        row = get_plan_by_id(conn, int(identifier))
        if row is not None:
            return row
    return get_plan_by_no(conn, str(identifier))


def list_plans(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM fixture_plans ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    )


def latest_plan(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM fixture_plans ORDER BY id DESC LIMIT 1").fetchone()


def get_active_plan_no(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT active_plan_no FROM fixture_state WHERE id = 1").fetchone()
    return row[0] if row is not None else None


def get_active_plan_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    no = get_active_plan_no(conn)
    return get_plan_by_no(conn, no) if no else None


def set_active_plan(conn: sqlite3.Connection, plan_no: str | None) -> None:
    conn.execute(
        """
        INSERT INTO fixture_state (id, active_plan_no, updated_at)
        VALUES (1, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        ON CONFLICT(id) DO UPDATE SET
            active_plan_no = excluded.active_plan_no,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
        """,
        (plan_no,),
    )


def update_plan_status(
    conn: sqlite3.Connection,
    plan_no: str,
    status: str,
    *,
    adopt: bool = False,
    withdraw_reason: str | None = None,
    withdraw_json: dict[str, Any] | None = None,
    report: dict[str, Any] | None = None,
) -> None:
    sets = ["status = ?"]
    params: list[Any] = [status]
    if adopt:
        sets.append("adopted_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')")
    if status == "withdrawn":
        sets.append("withdrawn_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')")
    if withdraw_reason is not None:
        sets.append("withdraw_reason = ?")
        params.append(withdraw_reason)
    if withdraw_json is not None:
        sets.append("withdraw_json = ?")
        params.append(json.dumps(withdraw_json, ensure_ascii=False))
    if report is not None:
        sets.append("report_json = ?")
        params.append(json.dumps(report, ensure_ascii=False))
    params.append(plan_no)
    conn.execute(f"UPDATE fixture_plans SET {', '.join(sets)} WHERE plan_no = ?", params)
