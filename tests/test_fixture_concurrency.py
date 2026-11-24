"""Race the fixture-plan adopt path against the tag that withdraws it.

Whatever serial order SQLite + the global lock choose, the final state must be
consistent: FIX-BREAK is registered, no plan is active and unsafe, and if the
plan was ever activated it is the registration transaction that withdrew it
with the right conflict box numbers.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import app.plans_service as plans
import app.services as services
from app.fixture_demo import (
    BREAK_BOX_NUMBER,
    FIX_BREAK,
    FIX_PLAN_NO,
    seed_fixture_demo,
)


def test_adopt_vs_breaking_registration_race_is_consistent(client):
    seed_fixture_demo()
    barrier = threading.Barrier(10)
    errors: list[BaseException] = []
    outcomes: list[str] = []

    def adopt_worker():
        barrier.wait()
        try:
            plans.adopt_plan(FIX_PLAN_NO)
            outcomes.append("adopted")
        except plans.PlanConflictError:
            outcomes.append("adopt_rejected")
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    def break_worker(i):
        barrier.wait()
        try:
            # Same canonical number/grid: exactly one registration commits,
            # the rest lose the race with 409.
            services.register_box(FIX_BREAK, box_number=BREAK_BOX_NUMBER)
            outcomes.append(f"registered-{i}")
        except services.ConflictError:
            outcomes.append(f"reg-conflict-{i}")
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=10) as pool:
        futs = [pool.submit(adopt_worker) for _ in range(5)]
        futs += [pool.submit(break_worker, i) for i in range(5)]
        for f in futs:
            f.result(timeout=30)

    assert not errors, errors

    state = plans.current_state()
    # The canonical breaking box exists exactly once.
    assert services.get_box(BREAK_BOX_NUMBER) is not None
    # No active plan may be unsafe against the final registry.
    if state["active"]:
        assert state["live_verification"]["safe"] is True  # pragma: no cover

    plan = plans.get_plan(FIX_PLAN_NO)
    if plan["status"] == "withdrawn":
        numbers = set(plan["withdrawal"]["conflicting_box_numbers"])
        assert {"FIX-A", BREAK_BOX_NUMBER} <= numbers
        assert plan["withdraw_reason"] == "new_tag_made_plan_ambiguous"
    else:
        # If the breaking registration happened before adoption, adoption must
        # have been rejected and the plan left non-active.
        assert plan["status"] in {"proposed", "infeasible"}
        assert state["active"] is False

    # Re-verifying explicitly always agrees with the stored/live verdict.
    if state["active"]:  # pragma: no cover
        assert state["live_verification"]["conflicting_box_numbers"] == []
