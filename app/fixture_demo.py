"""Deterministic /docs scenario: adopt a fixture plan, then watch a legal new
tag withdraw it.

Registry created by :func:`seed_fixture_demo` (all far from the original
``BOX-DEMO-01`` code and its occluded reading):

* ``FIX-A``     -- diamond target used by the adopt/withdraw path;
* ``FIX-C`` / ``FIX-D`` -- three bits apart at positions 0/4/24, used by the
  "no solution: two boxes would co-hit one grid" example.

The prepared proposal ``PLAN-FIX-01`` blocks eight positions; 6/8 are
drillable and positions 6 and 8 are *not*.  The minimum plan opens four
cells (11,12,13,17) and keeps 1,6,7,8 blocked.  Positions 6 and 8 are the
only difference between ``FIX-A`` and the perfectly legal new tag
``FIX-BREAK``; because the adopted mask keeps 6 and 8 covered, the masked
codes become identical and the plan is withdrawn atomically with the
201 registration.
"""
from __future__ import annotations

from typing import Any

from . import plans_service, repository as repo, services
from .db import connect, immediate_tx

FIX_PLAN_NO = "PLAN-FIX-01"

FIX_A: list[list[int]] = [
    [0, 0, 1, 0, 0],
    [0, 1, 1, 1, 0],
    [1, 1, 1, 1, 1],
    [0, 1, 1, 1, 0],
    [0, 0, 1, 0, 0],
]

# FIX_A with positions 6 and 8 flipped.  Complete-code distance is exactly 2,
# so normal registration accepts it (<=1 mismatch is the rejection boundary).
FIX_BREAK: list[list[int]] = [
    [0, 0, 1, 0, 0],
    [0, 0, 1, 0, 0],
    [1, 1, 1, 1, 1],
    [0, 1, 1, 1, 0],
    [0, 0, 1, 0, 0],
]

FIX_C: list[list[int]] = [
    [1, 1, 1, 0, 0],
    [1, 0, 0, 1, 1],
    [0, 0, 0, 1, 1],
    [0, 0, 0, 0, 0],
    [0, 0, 0, 1, 1],
]

# FIX_C flipped at positions 0, 4 and 24 -> distance exactly 3.
FIX_D: list[list[int]] = [
    [0, 1, 1, 0, 1],
    [1, 0, 0, 1, 1],
    [0, 0, 0, 1, 1],
    [0, 0, 0, 0, 0],
    [0, 0, 0, 1, 0],
]

# Eight blocked cells; positions 6 and 8 are not drillable.
SURVEY: dict[str, Any] = {
    "blocked": [1, 6, 7, 8, 11, 12, 13, 17],
    "openable": [1, 7, 11, 12, 13, 17],
}
EXPECTED_HOLES = [11, 12, 13, 17]
EXPECTED_REMAINING = [1, 6, 7, 8]

# Allowed holes opened to the full set still leaves 0 and 4 covered; those
# are two of the three FIX_C/FIX_D differences, so one grid co-hits both.
WITNESS_SURVEY: dict[str, Any] = {
    "blocked": [0, 4, 24, 5, 9, 15],
    "openable": [24, 5, 9, 15],
}

# Five blocked cells are non-openable -> more than the 4-cell limit remains.
OCCLUSION_SURVEY: dict[str, Any] = {
    "blocked": [0, 1, 2, 3, 4, 5],
    "openable": [5],
}

BREAK_BOX_NUMBER = "FIX-BREAK"


def seed_fixture_demo(*, reset: bool = False) -> dict[str, Any]:
    """Idempotently create fixture demo boxes and the prepared proposal."""
    if reset:
        reset_fixture_demo()

    for number, grid in (
        ("FIX-A", FIX_A),
        ("FIX-C", FIX_C),
        ("FIX-D", FIX_D),
    ):
        if services.get_box(number) is None:
            services.register_box(grid, box_number=number)

    existing = plans_service.get_plan(FIX_PLAN_NO)
    if existing is None:
        plans_service.create_proposal(SURVEY, plan_no=FIX_PLAN_NO)

    return fixture_scenario_payload()


def reset_fixture_demo() -> dict[str, Any]:
    """Remove FIX-% boxes, their readings and fixture plans/state (demo only)."""
    from .codes import grid_key, parse_grid  # noqa: F401

    conn = connect()
    try:
        with immediate_tx(conn):
            # Reading chunk rows reference readings; remove explicitly to stay
            # correct even if foreign_keys pragma ordering changes.
            fix_reading_ids = [
                r[0]
                for r in conn.execute(
                    "SELECT id FROM readings WHERE box_number LIKE 'FIX-%'"
                ).fetchall()
            ]
            if fix_reading_ids:
                q = ",".join("?" for _ in fix_reading_ids)
                conn.execute(
                    f"DELETE FROM reading_chunks WHERE reading_id IN ({q})",
                    fix_reading_ids,
                )
            conn.execute("DELETE FROM readings WHERE box_number LIKE 'FIX-%'")
            # Detach the singleton pointer before deleting referenced plans.
            conn.execute("UPDATE fixture_state SET active_plan_no = NULL, "
                         "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = 1")
            conn.execute("DELETE FROM fixture_plans")
            conn.execute("DELETE FROM boxes WHERE box_number LIKE 'FIX-%'")
            conn.execute("DELETE FROM sqlite_sequence WHERE name = 'fixture_plans'")
        return {"reset": True}
    finally:
        conn.close()


def fixture_scenario_payload() -> dict[str, Any]:
    state = plans_service.current_state()
    plan = plans_service.get_plan(FIX_PLAN_NO)
    return {
        "title": "开孔方案：采用 PLAN-FIX-01，再登记 FIX-BREAK 触发撤下",
        "steps": [
            "1) POST /api/demo/fixture-seed 准备 FIX-A/FIX-C/FIX-D 与提案 "
            f"{FIX_PLAN_NO}（compose 启动时已自动执行；幂等）。",
            f"2) POST /api/fixture/plans/{FIX_PLAN_NO}/adopt（或 POST /api/fixture/plan/adopt "
            f'请求体 {{"plan_id": "{FIX_PLAN_NO}"}}）→ 200，方案生效。',
            "3) 在 POST /api/boxes 选择示例 fixture_break_withdraws_plan 并 Execute "
            "→ 201，响应 fixture_plan.withdrawn=true，方案在同一事务撤下。",
            "4) GET /api/fixture/plan → active=false、status=withdrawn，"
            "conflicting_box_numbers 含 FIX-A 与 FIX-BREAK。",
            "5) 无解示例：POST /api/fixture/plans/evaluate 选 fixture_ambiguous_witness "
            "→ feasible=false/reason=ambiguous_pair，给 witness_grid 与箱号；"
            "fixture_occlusion_exceeded 示例 → reason=occlusion_exceeded。",
            "想完整重播：POST /api/demo/fixture-reset，再 POST /api/demo/fixture-seed。",
        ],
        "plan_id": FIX_PLAN_NO,
        "survey_body": SURVEY,
        "expected_holes": EXPECTED_HOLES,
        "expected_remaining": EXPECTED_REMAINING,
        "adopt_body": {"plan_id": FIX_PLAN_NO},
        "break_box_body": {"box_number": BREAK_BOX_NUMBER, "grid": FIX_BREAK},
        "witness_survey_body": WITNESS_SURVEY,
        "occlusion_survey_body": OCCLUSION_SURVEY,
        "state": state,
        "plan": plan,
    }
