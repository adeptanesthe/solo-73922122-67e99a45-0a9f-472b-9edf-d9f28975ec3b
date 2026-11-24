#!/usr/bin/env python3
"""End-to-end fixture hole-plan walkthrough over HTTP (stdlib only).

Demonstrates: survey -> minimum-hole plan -> adopt (re-verified) -> a legal
new tag registers AND atomically withdraws the active plan -> query lists the
conflicting box numbers -> the two no-solution shapes.

Usage:
    python3 scripts/fixture_walkthrough.py [base_url]
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

SURVEY = {"blocked": [1, 6, 7, 8, 11, 12, 13, 17],
          "openable": [1, 7, 11, 12, 13, 17]}
BREAK_BODY = {
    "box_number": "FIX-BREAK",
    "grid": [
        [0, 0, 1, 0, 0],
        [0, 0, 1, 0, 0],
        [1, 1, 1, 1, 1],
        [0, 1, 1, 1, 0],
        [0, 0, 1, 0, 0],
    ],
}
WITNESS = {"blocked": [0, 4, 24, 5, 9, 15], "openable": [24, 5, 9, 15]}
OCCLUSION = {"blocked": [0, 1, 2, 3, 4, 5], "openable": [5]}
PLAN_NO = "PLAN-FIX-01"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def main() -> int:
    status, health = call("GET", "/health")
    print(f"[health] {status} {health.get('stats')}")

    status, _ = call("POST", "/api/demo/fixture-reset", {})
    print(f"[reset] {status}")
    status, seed = call("POST", "/api/demo/fixture-seed", {})
    assert status == 200, seed
    print(f"[seed]  {status} prepared {PLAN_NO}")

    status, plan = call("POST", "/api/fixture/plans/evaluate", SURVEY)
    assert status == 200 and plan["feasible"], plan
    print(f"[plan]  minimum_holes={plan['minimum_holes']} holes={plan['holes']} "
          f"remaining={plan['remaining_blocked']}")
    assert plan["minimum_holes"] == 4 and plan["holes"] == [11, 12, 13, 17]

    status, adopted = call("POST", f"/api/fixture/plans/{PLAN_NO}/adopt", {})
    assert status == 200 and adopted["status"] == "active", adopted
    print(f"[adopt] {status} {PLAN_NO} -> active")

    status, cur = call("GET", "/api/fixture/plan")
    assert cur["active"] is True and cur["conflicting_box_numbers"] == []
    print("[query] active=true conflicts=[]")

    status, reg = call("POST", "/api/boxes", BREAK_BODY)
    print(f"[new legal tag] HTTP {status} "
          f"withdrawn={reg.get('active_fixture_plan_withdrawn')} "
          f"fixture_conflicts={reg.get('fixture_plan', {}).get('conflicting_box_numbers')}")
    assert status == 201, "FIX-BREAK must still be a legal registration"
    assert reg["active_fixture_plan_withdrawn"] is True

    status, cur = call("GET", "/api/fixture/plan")
    assert cur["active"] is False
    assert {"FIX-A", "FIX-BREAK"} <= set(cur["conflicting_box_numbers"])
    print(f"[query] active=false conflicts={cur['conflicting_box_numbers']} "
          f"reason={cur['last_withdrawn_plan']['withdraw_reason']}")

    status, w = call("POST", "/api/fixture/plans/evaluate", WITNESS)
    assert w["feasible"] is False and w["reason"] == "ambiguous_pair"
    pair = next(c for c in w["conflicts"]
                if set(c["box_numbers"]) == {"FIX-C", "FIX-D"})
    print(f"[witness] reason=ambiguous_pair boxes={w['conflicting_box_numbers']} "
          f"visible_differences={pair['visible_differences']}")
    print("           witness_grid=")
    for row in pair["witness_grid"]:
        line = " ".join("?" if v is None else str(v) for v in row)
        print("              ", line)

    status, o = call("POST", "/api/fixture/plans/evaluate", OCCLUSION)
    assert o["feasible"] is False and o["reason"] == "occlusion_exceeded"
    print(f"[occlusion] reason=occlusion_exceeded "
          f"non_openable={o['occlusion']['non_openable_count']} (>4)")

    print("\nFIXTURE WALKTHROUGH OK. Open /docs to replay interactively.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
