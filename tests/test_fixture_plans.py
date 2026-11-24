"""Hole-opening fixture plans: pure planning logic + API lifecycle.

Includes an independent brute-force oracle that enumerates every box, every
placement and every 0/1 visible-cell error variant through the real
``match_grid`` matcher, and checks the pairwise distance criterion.
"""
from __future__ import annotations

import itertools
import random

import pytest

from app.codes import match_grid, rotate
from app.fixture import (
    MODE_ERRORS_0_1,
    MODE_MATCHER_ONLY,
    BoxCode,
    Survey,
    SurveyError,
    build_constraints,
    evaluate_mask_detailed,
    extract_survey,
    mask_is_safe,
    normalize_position,
    plan_minimum,
)

ZERO = tuple([0] * 25)


def flip(g, positions):
    g = list(g)
    for p in positions:
        g[p] ^= 1
    return tuple(g)


def boxes_from(*pairs):
    return [BoxCode(n, g) for n, g in pairs]


# ----------------------------------------------------------- position parsing

def test_normalize_position_shapes():
    assert normalize_position(0) == 0
    assert normalize_position(24) == 24
    assert normalize_position([0, 0]) == 0
    assert normalize_position([4, 4]) == 24
    assert normalize_position("2,3") == 13
    assert normalize_position("(1, 2)") == 7
    assert normalize_position({"row": 1, "col": 2}) == 7
    assert normalize_position({"position": 9}) == 9
    with pytest.raises(SurveyError):
        normalize_position(25)
    with pytest.raises(SurveyError):
        normalize_position([5, 0])


def test_extract_survey_aliases_and_cells():
    s = extract_survey({"blocked_positions": [1, 2, 3], "drillable": [2]})
    assert s.blocked == (1, 2, 3)
    assert s.openable == (2,)
    assert s.non_openable == (1, 3)

    s2 = extract_survey({"cells": [
        {"position": 0, "blocked": True},
        {"row": 0, "col": 1, "openable": True},
        7,
    ]})
    assert s2.blocked == (0, 1, 7)
    assert s2.openable == (1,)

    # 5x5 mask form.
    mask = [[1, 0, 0, 0, 0]] * 5
    s3 = extract_survey({"mask": mask, "openable": [0]})
    assert 0 in s3.blocked and 5 in s3.blocked and len(s3.blocked) == 5


def test_survey_validation():
    with pytest.raises(SurveyError):
        extract_survey({"blocked": list(range(13))})  # > 12
    with pytest.raises(SurveyError):
        extract_survey({"blocked": [1, 2], "openable": [9]})  # not blocked
    with pytest.raises(SurveyError):
        extract_survey({})


# --------------------------------------------------------------- minimum plan

def test_single_box_plan_is_count_minimum():
    boxes = boxes_from(("A", ZERO))
    blocked = tuple(range(8))           # 8 blocked
    openable = tuple(range(2, 8))       # 0,1 non-openable
    res = plan_minimum(Survey(blocked, openable), boxes)
    assert res.feasible
    assert res.minimum_holes == 4       # 8 - 4
    assert len(res.remaining) == 4
    assert set(res.non_openable) == {0, 1}
    assert {0, 1} <= set(res.remaining)
    assert mask_is_safe(res.remaining, boxes)


def test_occlusion_exceeded_when_five_non_openable():
    res = plan_minimum(
        Survey(tuple(range(6)), (5,)), boxes_from(("A", ZERO))
    )
    assert not res.feasible
    assert res.reason == "occlusion_exceeded"
    assert res.minimum_holes is None


def test_ambiguous_pair_witness_hits_both_boxes():
    a = ZERO
    c = flip(ZERO, [0, 4, 24])
    boxes = boxes_from(("A", a), ("C", c))
    # Leave two of the three differences covered.
    survey = Survey((0, 4, 24, 5, 9), (24, 5, 9))
    res = plan_minimum(survey, boxes)
    assert not res.feasible
    assert res.reason == "ambiguous_pair"
    assert {"A", "C"} <= set(res.safety["conflicting_box_numbers"])
    conf = next(
        x for x in res.safety["conflicts"]
        if set(x["box_numbers"]) == {"A", "C"}
    )
    witness = tuple(v for row in conf["witness_grid"] for v in row)
    # The witness really co-matches both registered codes.
    assert match_grid(witness, a) is not None
    assert match_grid(witness, c) is not None
    assert conf["visible_differences"] <= 2


def test_strong_vs_matcher_only_distance_two():
    a = ZERO
    b = flip(ZERO, [7, 11])  # exactly two bits, every rotation keeps dist == 2
    boxes = boxes_from(("A", a), ("B", b))
    # Empty remaining mask: matcher-only is safe (distance 2 > 1) but the
    # robust 0/1-error contract is not (one lucky error bridges both).
    strong = evaluate_mask_detailed((), boxes, mode=MODE_ERRORS_0_1)
    lenient = evaluate_mask_detailed((), boxes, mode=MODE_MATCHER_ONLY)
    assert strong["safe"] is False
    assert lenient["safe"] is True
    witness_conf = strong["conflicts"][0]
    assert witness_conf["simulated_error_at"] is not None

    strong_plan = plan_minimum(
        Survey((7, 11), (7, 11), mode=MODE_ERRORS_0_1), boxes
    )
    # Even opening both (empty mask) cannot satisfy distance >= 3.
    assert strong_plan.feasible is False
    lenient_plan = plan_minimum(
        Survey((7, 11), (7, 11), mode=MODE_MATCHER_ONLY), boxes
    )
    assert lenient_plan.feasible is True
    assert lenient_plan.remaining == ()
    assert lenient_plan.holes == (7, 11)


# ------------------------------------------------- exhaustive oracle + optimum

def brute_force_safe(mask, boxes):
    """Independent oracle: enumerate placements + every 0/1 visible error."""
    mset = set(mask)
    for bx in boxes:
        for p in range(4):
            truth = rotate(bx.grid, p)
            base = [None if i in mset else truth[i] for i in range(25)]
            variants = [tuple(base)]
            for e in range(25):
                if e in mset:
                    continue
                v = list(base)
                v[e] ^= 1
                variants.append(tuple(v))
            for scan in variants:
                hits = set()
                for other in boxes:
                    if match_grid(scan, other.grid) is not None:
                        hits.add(other.box_number)
                if hits != {bx.box_number}:
                    return False
    return True


def test_distance_criterion_matches_bruteforce_oracle_random():
    rng = random.Random(20261124)
    for trial in range(300):
        n = rng.randrange(1, 5)
        boxes = boxes_from(*[
            (f"B{i}", tuple(rng.randrange(2) for _ in range(25)))
            for i in range(n)
        ])
        mask = tuple(sorted(rng.sample(range(25), rng.randrange(5))))
        got = mask_is_safe(mask, boxes, MODE_ERRORS_0_1)
        expected = brute_force_safe(mask, boxes)
        assert got == expected, (trial, mask)


def test_plan_is_minimum_and_complete_random():
    rng = random.Random(99)
    for trial in range(200):
        n = rng.randrange(1, 4)
        boxes = boxes_from(*[
            (f"B{i}", tuple(rng.randrange(2) for _ in range(25)))
            for i in range(n)
        ])
        blocked = tuple(sorted(rng.sample(range(25), rng.randrange(4, 12))))
        openable = tuple(p for p in blocked if rng.random() < 0.7)
        res = plan_minimum(Survey(blocked, openable), boxes)

        mandatory = tuple(p for p in blocked if p not in set(openable))
        # Exhaustively check every legal remaining mask.
        possible_sizes = range(len(mandatory), min(4, len(blocked)) + 1)
        safe_sizes = []
        for size in possible_sizes:
            extra_needed = size - len(mandatory)
            drillable = [p for p in blocked if p in set(openable)]
            for extra in itertools.combinations(drillable, extra_needed):
                m = tuple(sorted(mandatory + extra))
                if mask_is_safe(m, boxes):
                    safe_sizes.append(size)
                    break
        if not safe_sizes:
            assert res.feasible is False
        else:
            best_size = max(safe_sizes)
            assert res.feasible is True
            assert len(res.remaining) == best_size
            assert res.minimum_holes == len(blocked) - best_size
            assert mask_is_safe(res.remaining, boxes)
