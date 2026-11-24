"""Fixture hole-plan orchestration: evaluate, propose, adopt, withdraw.

Lifecycle / concurrency
-----------------------
* :func:`evaluate_survey` is read-only and takes no lock: planning proceeds
  while boxes are registered ("规划期间照常挂牌").
* Creating a proposal, adopting and withdrawing take the process-wide
  :data:`app.locking.OPERATION_LOCK` and run inside ``BEGIN IMMEDIATE``.
* Adoption **re-verifies** the stored remaining mask against the current
  registry.  If it is no longer safe the call fails with
  :class:`PlanConflictError` (HTTP 409) and the previous active plan is left
  untouched ("失败则保留原状态").
* :func:`review_active_plan_in_tx` runs inside the registration transaction;
  when a legal new tag makes the active plan ambiguous it withdraws the plan
  in that very transaction, so registration and withdrawal commit together.
"""
from __future__ import annotations

import json
from typing import Any, Sequence

from . import repository as repo
from .codes import MAX_UNREADABLE
from .db import connect as repo_connect, immediate_tx
from .fixture import (
    MODE_ERRORS_0_1,
    BoxCode,
    PlanResult,
    Survey,
    SurveyError,
    extract_survey,
    plan_minimum,
    verify_remaining,
)
from .locking import OPERATION_LOCK


class PlanConflictError(Exception):
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        super().__init__(payload.get("message", "fixture plan conflict"))


# ------------------------------------------------------------ serialization

def _boxes_from_conn(conn: Any) -> list[BoxCode]:
    return [BoxCode(b["box_number"], repo.flat_of_box(b)) for b in repo.list_boxes(conn)]


def plan_row_to_dict(row: Any, *, live: dict[str, Any] | None = None) -> dict[str, Any]:
    report = json.loads(row["report_json"]) if row["report_json"] else {}
    out: dict[str, Any] = {
        "id": row["id"],
        "plan_id": row["plan_no"],
        "plan_no": row["plan_no"],
        "status": row["status"],
        "active": row["status"] == "active",
        "is_active": row["status"] == "active",
        "withdrawn": row["status"] == "withdrawn",
        "feasible": bool(row["feasible"]),
        "mode": row["mode"],
        "minimum_holes": row["min_holes"],
        "hole_count": row["min_holes"],
        "reason": row["reason"],
        "blocked": json.loads(row["blocked_json"]),
        "openable": json.loads(row["openable_json"]),
        "holes": json.loads(row["holes_json"]),
        "open_positions": json.loads(row["holes_json"]),
        "remaining_blocked": json.loads(row["remaining_json"]),
        "remaining": json.loads(row["remaining_json"]),
        "created_at": row["created_at"],
        "adopted_at": row["adopted_at"],
        "withdrawn_at": row["withdrawn_at"],
        "withdraw_reason": row["withdraw_reason"],
        "withdrawal": json.loads(row["withdraw_json"]) if row["withdraw_json"] else None,
        "report": report,
        # Convenience top-level conflict fields expected by query clients:
        "conflicting_box_numbers": report.get("conflicting_box_numbers", []),
        "conflicts": report.get("conflicts", []),
        "safe": report.get("safe", False) if row["feasible"] else False,
    }
    if live is not None:
        out["live"] = live
        out["live_safe"] = live.get("safe")
        out["live_conflicting_box_numbers"] = live.get("conflicting_box_numbers", [])
        # While active, surface *live* conflicts at top level so GET callers
        # always see the current registry verdict.
        if row["status"] == "active":
            out["conflicting_box_numbers"] = live.get("conflicting_box_numbers", [])
            out["conflicts"] = live.get("conflicts", [])
            out["safe"] = live.get("safe", False)

    # A withdrawn plan must surface the withdrawal-time conflicts (stored in
    # withdraw_json), not the original safe adoption report.
    if row["status"] == "withdrawn" and out["withdrawal"]:
        wd = out["withdrawal"]
        verification = wd.get("verification") or {}
        numbers = wd.get("conflicting_box_numbers") or verification.get(
            "conflicting_box_numbers", []
        )
        out["conflicting_box_numbers"] = numbers
        out["conflicts"] = verification.get("conflicts", out["conflicts"])
        out["safe"] = False
    return out


# ------------------------------------------------------------ evaluation

def evaluate_survey(raw: Any) -> dict[str, Any]:
    """Compute the minimum-hole plan against the current registry (no writes)."""
    survey = extract_survey(raw)
    conn = repo_connect()
    try:
        boxes = _boxes_from_conn(conn)
    finally:
        conn.close()
    result = plan_minimum(survey, boxes)
    data = result.as_dict()
    data["registry_boxes"] = len(boxes)
    data["box_count"] = len(boxes)
    data["max_remaining_blocked"] = MAX_UNREADABLE

    # Supplementary: the weaker "masked truth only, match_grid <=1" reading,
    # so callers using that convention can see its minimum as well.
    if survey.mode == MODE_ERRORS_0_1:
        try:
            weak_survey = Survey(
                blocked=survey.blocked, openable=survey.openable,
                mode="matcher_only", extra=survey.extra,
            )
            weak = plan_minimum(weak_survey, boxes)
            data["matcher_only_plan"] = {
                "mode": "matcher_only",
                "feasible": weak.feasible,
                "minimum_holes": weak.minimum_holes,
                "holes": list(weak.holes),
                "remaining_blocked": list(weak.remaining),
                "reason": weak.reason,
                "conflicting_box_numbers": weak.safety.get(
                    "conflicting_box_numbers", []
                ),
            }
        except Exception:  # pragma: no cover - supplementary only
            data["matcher_only_plan"] = None

    compact = {
        "feasible": data["feasible"],
        "minimum_holes": data["minimum_holes"],
        "hole_count": data["minimum_holes"],
        "holes": data["holes"],
        "open_positions": data["holes"],
        "holes_to_open": data["holes"],
        "remaining_blocked": data["remaining_blocked"],
        "safe": data["safe"],
        "conflicting_box_numbers": data["conflicting_box_numbers"],
        "reason": data["reason"],
        "mode": data["mode"],
    }
    data["plan"] = compact
    data["solution"] = compact
    return data


# ------------------------------------------------------------ proposals

def create_proposal(raw: Any, *, plan_no: str | None = None) -> dict[str, Any]:
    """Evaluate and persist a proposal/infeasible record."""
    survey = extract_survey(raw)
    result = _compute(survey)

    with OPERATION_LOCK:
        conn = repo_connect()
        try:
            with immediate_tx(conn):
                if plan_no:
                    if repo.plan_number_exists(conn, plan_no):
                        existing = repo.get_plan_by_no(conn, plan_no)
                        return plan_row_to_dict(existing) | {"already_exists": True}
                    chosen_no = plan_no
                else:
                    chosen_no = repo.next_plan_number(conn)
                pk = repo.insert_plan(
                    conn,
                    plan_no=chosen_no,
                    status="proposed" if result.feasible else "infeasible",
                    feasible=result.feasible,
                    mode=result.mode,
                    blocked=result.blocked,
                    openable=result.openable,
                    holes=result.holes,
                    remaining=result.remaining,
                    min_holes=result.minimum_holes,
                    reason=result.reason,
                    report=result.as_dict(),
                    request=raw if isinstance(raw, dict) else None,
                )
                row = repo.get_plan_by_id(conn, pk)
                assert row is not None
                return plan_row_to_dict(row)
        finally:
            conn.close()


def _compute(survey: Survey) -> PlanResult:
    conn = repo_connect()
    try:
        boxes = _boxes_from_conn(conn)
    finally:
        conn.close()
    return plan_minimum(survey, boxes)


# ------------------------------------------------------------ adopt / withdraw

def adopt_plan(
    identifier: str | int | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-verify and activate a stored plan (or an inline survey/holes)."""
    payload = payload or {}
    with OPERATION_LOCK:
        conn = repo_connect()
        try:
            with immediate_tx(conn):
                ident = identifier or payload.get("plan_id") or payload.get("plan_no") or payload.get("id")
                row = repo.find_plan(conn, ident) if ident is not None else None

                if ident is not None and row is None and not (
                    payload.get("blocked") or payload.get("cells") or payload.get("mask")
                ):
                    raise PlanConflictError({
                        "error": "plan_not_found",
                        "message": f"no fixture plan {ident!r}",
                        "plan_id": ident,
                    })

                if row is not None:
                    mode = row["mode"]
                    remaining = json.loads(row["remaining_json"])
                    plan_no = row["plan_no"]
                else:
                    # Inline adoption: parse survey and optional explicit holes.
                    survey = extract_survey(payload)
                    mode = survey.mode
                    plan_no = (ident if isinstance(ident, str) and not str(ident).isdigit()
                               else payload.get("plan_no")) or repo.next_plan_number(conn)
                    if repo.plan_number_exists(conn, plan_no):
                        row = repo.get_plan_by_no(conn, plan_no)
                        mode = row["mode"]
                        remaining = json.loads(row["remaining_json"])
                    else:
                        explicit_holes = payload.get("holes") or payload.get("open_positions")
                        if explicit_holes:
                            from .fixture import normalize_position
                            holes = tuple(sorted({normalize_position(p) for p in explicit_holes}))
                            blocked_set = set(survey.blocked)
                            bad = [p for p in holes if p not in blocked_set]
                            if bad:
                                raise SurveyError(
                                    f"holes {bad} must be among blocked {sorted(blocked_set)}"
                                )
                            # If an allowed-to-drill set was supplied, explicit
                            # holes must respect it; otherwise the explicit
                            # holes themselves are the authorization.
                            if survey.openable:
                                not_allowed = [p for p in holes if p not in set(survey.openable)]
                                if not_allowed:
                                    raise SurveyError(
                                        f"holes {not_allowed} are not marked openable"
                                    )
                            remaining = tuple(p for p in survey.blocked if p not in set(holes))
                            if len(remaining) > MAX_UNREADABLE:
                                raise SurveyError(
                                    f"{len(remaining)} cells remain blocked; "
                                    f"at most {MAX_UNREADABLE}"
                                )
                        else:
                            result = plan_minimum(survey, _boxes_from_conn(conn))
                            if not result.feasible:
                                raise PlanConflictError(_infeasible_payload(result))
                            remaining = result.remaining
                            holes = result.holes
                        verification = verify_remaining(
                            remaining, _boxes_from_conn(conn), mode=mode
                        )
                        if not verification["safe"]:
                            raise PlanConflictError({
                                "error": "plan_verification_failed",
                                "message": "inline plan is not safe against the current registry",
                                "preserved_active_plan_no": repo.get_active_plan_no(conn),
                                "original_state_preserved": True,
                                **verification,
                            })
                        repo.insert_plan(
                            conn, plan_no=plan_no, status="proposed", feasible=True,
                            mode=mode, blocked=survey.blocked, openable=survey.openable,
                            holes=tuple(p for p in survey.blocked if p not in set(remaining)),
                            remaining=remaining,
                            min_holes=len(survey.blocked) - len(remaining),
                            reason=None, report=verification,
                            request=payload,
                        )
                        row = repo.get_plan_by_no(conn, plan_no)

                # Re-verify against the *current* registry inside the write tx.
                verification = verify_remaining(
                    remaining, _boxes_from_conn(conn), mode=mode
                )
                if not verification["safe"]:
                    current_no = repo.get_active_plan_no(conn)
                    raise PlanConflictError({
                        "error": "plan_verification_failed",
                        "message": (
                            "computed plan is no longer safe against the current "
                            "registry; active fixture state was preserved"
                        ),
                        "plan_id": plan_no,
                        "plan_no": plan_no,
                        "preserved_active_plan_no": current_no,
                        "original_state_preserved": True,
                        **verification,
                    })

                current_no = repo.get_active_plan_no(conn)
                already_active = current_no == plan_no and row["status"] == "active"
                if current_no and current_no != plan_no:
                    repo.update_plan_status(conn, current_no, "superseded")
                repo.update_plan_status(conn, plan_no, "active", adopt=True,
                                        report={**json.loads(row["report_json"]),
                                                "last_adopt_verification": verification}
                                        if row["report_json"] else verification)
                repo.set_active_plan(conn, plan_no)
                fresh = repo.get_plan_by_no(conn, plan_no)
                assert fresh is not None
                data = plan_row_to_dict(fresh, live=verification)
                data["adopted"] = True
                data["already_active"] = already_active
                data["previous_active_plan_no"] = current_no if current_no != plan_no else None
                return data
        finally:
            conn.close()


def _infeasible_payload(result: PlanResult) -> dict[str, Any]:
    d = result.as_dict()
    return {
        "error": "plan_infeasible",
        "message": "opening every allowed position still yields no safe plan",
        **d,
    }


def withdraw_active_plan(reason: str = "manual", *, actor: str | None = None) -> dict[str, Any]:
    with OPERATION_LOCK:
        conn = repo_connect()
        try:
            with immediate_tx(conn):
                active = repo.get_active_plan_row(conn)
                if active is None:
                    return {"withdrawn": False, "active": False,
                            "message": "no active fixture plan"}
                plan_no = active["plan_no"]
                info = {"by": actor or "api", "reason": reason}
                repo.update_plan_status(
                    conn, plan_no, "withdrawn",
                    withdraw_reason=reason, withdraw_json=info,
                )
                repo.set_active_plan(conn, None)
                row = repo.get_plan_by_no(conn, plan_no)
                return {
                    "withdrawn": True,
                    "active": False,
                    "plan_id": plan_no,
                    "plan_no": plan_no,
                    "reason": reason,
                    "plan": plan_row_to_dict(row),
                }
        finally:
            conn.close()


# ------------------------------------------------------------ registration hook

def review_active_plan_in_tx(conn: Any, *, trigger_box_number: str) -> dict[str, Any] | None:
    """Must be called inside the registration transaction after inserting box."""
    active = repo.get_active_plan_row(conn)
    if active is None:
        return None
    plan_no = active["plan_no"]
    remaining = json.loads(active["remaining_json"])
    mode = active["mode"] or MODE_ERRORS_0_1
    verification = verify_remaining(remaining, _boxes_from_conn(conn), mode=mode)
    if verification["safe"]:
        return {
            "plan_id": plan_no,
            "plan_no": plan_no,
            "withdrawn": False,
            "active": True,
            "safe": True,
        }

    conflict_numbers = verification.get("conflicting_box_numbers", [])
    info = {
        "by": "registration",
        "reason": "new_tag_made_active_plan_ambiguous",
        "trigger_box_number": trigger_box_number,
        "conflicting_box_numbers": conflict_numbers,
        "verification": verification,
    }
    repo.update_plan_status(
        conn, plan_no, "withdrawn",
        withdraw_reason="new_tag_made_plan_ambiguous",
        withdraw_json=info, report={
            **(json.loads(active["report_json"]) if active["report_json"] else {}),
            "withdrawal_verification": verification,
        },
    )
    repo.set_active_plan(conn, None)
    return {
        "plan_id": plan_no,
        "plan_no": plan_no,
        "withdrawn": True,
        "active": False,
        "safe": False,
        "reason": "new_tag_made_plan_ambiguous",
        "trigger_box_number": trigger_box_number,
        "conflicting_box_numbers": conflict_numbers,
        "conflicts": verification.get("conflicts", []),
    }


# ------------------------------------------------------------ queries

def current_state() -> dict[str, Any]:
    conn = repo_connect()
    try:
        active = repo.get_active_plan_row(conn)
        latest = repo.latest_plan(conn)
        boxes = _boxes_from_conn(conn)
        active_dict: dict[str, Any] | None = None
        live: dict[str, Any] | None = None
        if active is not None:
            remaining = json.loads(active["remaining_json"])
            live = verify_remaining(remaining, boxes, mode=active["mode"])
            active_dict = plan_row_to_dict(active, live=live)

        latest_dict = plan_row_to_dict(latest) if latest is not None else None

        # Find most recently withdrawn plan for conflict reporting.
        withdrawn_no = None
        rows = repo.list_plans(conn, limit=50)
        withdrawn_row = next((r for r in rows if r["status"] == "withdrawn"), None)
        if withdrawn_row is not None:
            withdrawn_no = withdrawn_row["plan_no"]
        withdrawn_dict = plan_row_to_dict(withdrawn_row) if withdrawn_row is not None else None

        if active_dict is not None:
            conflict_numbers = active_dict.get("conflicting_box_numbers", [])
            conflicts = active_dict.get("conflicts", [])
        elif withdrawn_dict is not None:
            wd = withdrawn_dict.get("withdrawal") or {}
            conflict_numbers = (
                wd.get("conflicting_box_numbers")
                or withdrawn_dict.get("conflicting_box_numbers", [])
            )
            conflicts = withdrawn_dict.get("conflicts", [])
        elif latest_dict is not None:
            conflict_numbers = latest_dict.get("conflicting_box_numbers", [])
            conflicts = latest_dict.get("conflicts", [])
        else:
            conflict_numbers, conflicts = [], []

        return {
            "scope": "single-fixture",
            "fixture_count": 1,
            "active": active is not None,
            "active_plan_no": active["plan_no"] if active is not None else None,
            "active_plan": active_dict,
            "live_verification": live,
            "latest_plan": latest_dict,
            "last_withdrawn_plan": withdrawn_dict,
            "last_withdrawn_plan_no": withdrawn_no,
            "registry_boxes": len(boxes),
            "box_count": len(boxes),
            "conflicting_box_numbers": conflict_numbers,
            "conflicts": conflicts,
        }
    finally:
        conn.close()


def list_plans(limit: int = 100) -> dict[str, Any]:
    limit = max(1, min(limit, 1000))
    conn = repo_connect()
    try:
        rows = repo.list_plans(conn, limit=limit)
        active_no = repo.get_active_plan_no(conn)
        plans = [plan_row_to_dict(r) for r in rows]
        return {"count": len(plans), "active_plan_no": active_no, "plans": plans}
    finally:
        conn.close()


def get_plan(identifier: str) -> dict[str, Any] | None:
    conn = repo_connect()
    try:
        row = repo.find_plan(conn, identifier)
        if row is None:
            return None
        live = None
        if row["status"] == "active":
            live = verify_remaining(
                json.loads(row["remaining_json"]), _boxes_from_conn(conn), mode=row["mode"]
            )
        return plan_row_to_dict(row, live=live)
    finally:
        conn.close()
