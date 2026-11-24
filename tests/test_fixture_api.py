"""HTTP lifecycle for fixture plans: propose, adopt, re-verify-fail preserve,
atomic withdrawal on a legal new tag, query conflicts, aliases."""
from __future__ import annotations

import pytest

from app.fixture_demo import (
    BREAK_BOX_NUMBER,
    EXPECTED_HOLES,
    FIX_PLAN_NO,
    OCCLUSION_SURVEY,
    SURVEY,
    WITNESS_SURVEY,
)

ZERO = [[0] * 5 for _ in range(5)]


def flip_grid(positions):
    g = [row[:] for row in ZERO]
    for p in positions:
        r, c = divmod(p, 5)
        g[r][c] = 1
    return g


# ----------------------------------------------------------- compute / propose

def test_evaluate_returns_minimum_plan(client):
    r = client.post("/api/fixture/plans/evaluate", json=SURVEY)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["feasible"] is True
    assert body["minimum_holes"] == 4
    assert body["holes"] == EXPECTED_HOLES
    assert len(body["remaining_blocked"]) == 4
    assert body["safe"] is True


def test_survey_over_twelve_is_422(client):
    r = client.post("/api/fixture/plans/evaluate",
                    json={"blocked": list(range(13))})
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_survey"


def test_create_proposal_201_then_get(client):
    r = client.post("/api/fixture/plans", json=SURVEY)
    assert r.status_code == 201, r.text
    plan_no = r.json()["plan_no"]
    assert plan_no == "PLAN-0001"
    g = client.get(f"/api/fixture/plans/{plan_no}")
    assert g.status_code == 200 and g.json()["status"] == "proposed"
    assert client.get("/api/fixture/plans/NOPE").status_code == 404


def test_infeasible_proposal_returns_409_with_boxes_and_witness(client):
    client.post("/api/boxes", json={"box_number": "A", "grid": ZERO})
    client.post("/api/boxes", json={"box_number": "C", "grid": flip_grid([0, 4, 24])})
    r = client.post("/api/fixture/plans", json=WITNESS_SURVEY)
    assert r.status_code == 409
    body = r.json()
    assert body["feasible"] is False
    assert body["reason"] == "ambiguous_pair"
    assert {"A", "C"} <= set(body["conflicting_box_numbers"])
    assert body["conflicts"][0]["witness_grid"] is not None


def test_occlusion_exceeded_shape(client):
    r = client.post("/api/fixture/plans/evaluate", json=OCCLUSION_SURVEY)
    assert r.status_code == 200
    body = r.json()
    assert body["feasible"] is False
    assert body["reason"] == "occlusion_exceeded"
    assert body["occlusion"]["non_openable_count"] > 4


# --------------------------------------------------------------- adopt / query

def test_adopt_and_manual_withdraw(client):
    p = client.post("/api/fixture/plans", json=SURVEY).json()
    r = client.post(f"/api/fixture/plans/{p['plan_no']}/adopt")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"

    cur = client.get("/api/fixture/plan").json()
    assert cur["active"] is True
    assert cur["active_plan_no"] == p["plan_no"]
    assert cur["conflicting_box_numbers"] == []

    w = client.post("/api/fixture/plan/withdraw", json={"reason": "unit"})
    assert w.status_code == 200 and w.json()["withdrawn"] is True
    cur2 = client.get("/api/fixture/plan").json()
    assert cur2["active"] is False


def test_adopt_recheck_failure_preserves_previous_active_plan(client):
    # Box A = all zero.
    client.post("/api/boxes", json={"box_number": "A", "grid": ZERO})

    # P1 keeps central mask {6,7,8,12}, disjoint from every rotation of the
    # triple {0,1,2} that the future box X will add.
    m1 = [6, 7, 8, 12]
    r = client.post("/api/fixture/plan/adopt", json={
        "blocked": m1 + [5, 11],
        "openable": [5, 11],
        "holes": [5, 11],
        "plan_no": "PLAN-KEEP",
    })
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"

    # P2 proposal makes position 0 non-openable; with only A it is feasible.
    p2 = client.post("/api/fixture/plans", json={
        "plan_no": "PLAN-STALE",
        "blocked": [0, 5, 6, 7, 8],
        "openable": [5, 6, 7, 8],
    })
    assert p2.status_code == 201

    # Legal new tag at distance exactly 3 from A.  P1 stays safe (its mask is
    # disjoint from all rotated triples), so registration must not withdraw.
    reg = client.post("/api/boxes", json={"box_number": "X", "grid": flip_grid([0, 1, 2])})
    assert reg.status_code == 201, reg.text
    assert reg.json()["active_fixture_plan_withdrawn"] is False

    # Adopting the stale P2 now fails verification and preserves PLAN-KEEP.
    bad = client.post("/api/fixture/plans/PLAN-STALE/adopt")
    assert bad.status_code == 409
    body = bad.json()
    assert body["error"] == "plan_verification_failed"
    assert body["original_state_preserved"] is True
    assert body["preserved_active_plan_no"] == "PLAN-KEEP"
    assert {"A", "X"} <= set(body["conflicting_box_numbers"])

    cur = client.get("/api/fixture/plan").json()
    assert cur["active"] is True
    assert cur["active_plan_no"] == "PLAN-KEEP"


# --------------------------------------------------------- headline demo flow

def test_demo_adopt_then_legal_new_tag_withdraws_atomically(client):
    seed = client.post("/api/demo/fixture-seed")
    assert seed.status_code == 200, seed.text
    assert seed.json()["plan"]["plan_no"] == FIX_PLAN_NO

    adopt = client.post(f"/api/fixture/plans/{FIX_PLAN_NO}/adopt")
    assert adopt.status_code == 200, adopt.text
    assert adopt.json()["status"] == "active"

    before = client.get("/api/fixture/plan").json()
    assert before["active"] is True

    # The prefilled breaking tag is legal under the ordinary tag rules.
    scenario = client.get("/api/demo/fixture-scenario").json()
    reg = client.post("/api/boxes", json=scenario["break_box_body"])
    assert reg.status_code == 201, reg.text
    rb = reg.json()
    assert rb["active_fixture_plan_withdrawn"] is True
    fp = rb["fixture_plan"]
    assert fp["withdrawn"] is True
    assert set(fp["conflicting_box_numbers"]) >= {"FIX-A", BREAK_BOX_NUMBER}

    state = client.get("/api/fixture/plan").json()
    assert state["active"] is False
    assert FIX_PLAN_NO in (state["last_withdrawn_plan_no"] or "")
    assert set(state["conflicting_box_numbers"]) >= {"FIX-A", BREAK_BOX_NUMBER}
    withdrawn = state["last_withdrawn_plan"]
    assert withdrawn["status"] == "withdrawn"
    assert withdrawn["withdraw_reason"] == "new_tag_made_plan_ambiguous"

    # Querying the plan by its id must also list the conflict box numbers.
    by_id = client.get(f"/api/fixture/plans/{FIX_PLAN_NO}").json()
    assert by_id["status"] == "withdrawn"
    assert by_id["safe"] is False
    assert set(by_id["conflicting_box_numbers"]) >= {"FIX-A", BREAK_BOX_NUMBER}

    # Registry really contains the new tag: registration + withdrawal committed.
    assert client.get(f"/api/boxes/{BREAK_BOX_NUMBER}").status_code == 200


def test_fixture_seed_is_idempotent_and_reset_replays(client):
    client.post("/api/demo/fixture-seed")
    client.post("/api/demo/fixture-seed")
    plans = client.get("/api/fixture/plans").json()["plans"]
    assert [p["plan_no"] for p in plans].count(FIX_PLAN_NO) == 1

    client.post(f"/api/fixture/plans/{FIX_PLAN_NO}/adopt")
    reset = client.post("/api/demo/fixture-reset")
    assert reset.status_code == 200
    state = client.get("/api/fixture/plan").json()
    assert state["active"] is False
    assert client.get("/api/boxes/FIX-A").status_code == 404

    client.post("/api/demo/fixture-seed")
    assert client.get(f"/api/fixture/plans/{FIX_PLAN_NO}").status_code == 200


# -------------------------------------------------------------------- aliases

def test_common_path_aliases_work(client):
    r = client.post("/api/hole-plan", json={"blocked": [0, 1, 2, 3]})
    assert r.status_code == 200 and r.json()["feasible"] is True
    g = client.get("/api/plan")
    assert g.status_code == 200 and g.json()["active"] is False

    r2 = client.post("/api/plans", json=SURVEY)
    assert r2.status_code == 201
    lst = client.get("/api/plans")
    assert lst.status_code == 200 and lst.json()["count"] >= 1
