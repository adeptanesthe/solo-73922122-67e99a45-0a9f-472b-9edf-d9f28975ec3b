"""Hole-opening plan logic for the single scan fixture (pure, DB-free).

Physical setup
--------------
The fixture permanently covers up to ``MAX_BLOCKED`` (12) cells of a 5x5 tag.
The site survey submits those blocked positions and marks the subset where a
hole may be drilled.  Opening a hole removes that position from the occlusion
mask.  A proposed plan therefore ends with an "remaining mask" ``M``: the
cells that stay blocked.  At most :data:`app.codes.MAX_UNREADABLE` (4) cells
may remain blocked.

Safety contract
---------------
A remaining mask ``M`` is *safe* for the current tag registry when, for every
registered box and every one of the four tag placements, every scan obtained
through ``M`` with **0 or 1 wrong visible bit** is recognised (via the normal
:func:`app.codes.match_grid` rule) as that box and that box alone.

For two complete rotated codes ``A`` (the placed original) and ``C`` (another
box in any matcher rotation), let ``d`` be their Hamming distance restricted
to visible positions (positions outside ``M``).  A single adversarial read
error can reduce the observed distance to ``C`` by at most one (flip one of
the differing visible cells towards ``C``), so ``C`` co-matches some allowed
scan iff ``d - 1 <= MAX_MISMATCH`` i.e. iff ``d <= 2``.  Hence the robust
contract is exactly:

    visible_distance(A, C; M) >= 3

for every distinct box pair, every placement and every matcher rotation.

A weaker "matcher only" mode (``required_distance=2``) checks the masked
truth directly with the <=1 mismatch rule, without injecting an additional
read error; it is exposed for callers that interpret "0 or 1" as the
matcher's mismatch allowance rather than as worst-case field errors.

The same predicate is re-run before adopting a computed plan and inside the
registration transaction that may atomically withdraw the active plan.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .codes import GRID_SIZE, MAX_UNREADABLE, rotate

MAX_BLOCKED = 12

#: Robust default: survive one actual visible read error *and* the matcher's
#: one-cell tolerance without ever producing a second candidate.
MODE_ERRORS_0_1 = "errors_0_1"
#: Weaker interpretation: masked truth only has to be unique under match_grid.
MODE_MATCHER_ONLY = "matcher_only"
REQUIRED_DISTANCE = {
    MODE_ERRORS_0_1: 2 + 1,  # = MAX_MISMATCH(1) + one injected error + 1
    MODE_MATCHER_ONLY: 1 + 1,
}

POSITION_ALIASES = ("position", "pos", "index", "idx", "cell_index", "at", "p")
ROW_ALIASES = ("row", "r")
COL_ALIASES = ("col", "c", "column", "x")


class SurveyError(ValueError):
    """Raised when a fixture survey cannot be interpreted."""


# ------------------------------------------------------------ positions

def normalize_position(value: Any) -> int:
    """Coerce one position to a row-major 0..24 index.

    Accepted:
      * an ``int`` 0..24 (row-major, zero based);
      * ``[row, col]`` / ``(row, col)`` with 0..4;
      * ``"row,col"`` / ``"(row,col)"`` strings;
      * objects ``{"row": r, "col": c}`` or ``{"position": i}``.
    """
    if isinstance(value, bool):
        raise SurveyError("boolean is not a valid cell position")
    if isinstance(value, int):
        if not 0 <= value < GRID_SIZE * GRID_SIZE:
            raise SurveyError(f"flat position {value} out of range 0..24")
        return value
    if isinstance(value, float) and value.is_integer():
        return normalize_position(int(value))
    if isinstance(value, str):
        token = value.strip().strip("()（）")
        if token.isdigit():
            return normalize_position(int(token))
        for sep in (",", "，", ";", "；", " "):
            if sep in token:
                parts = [p for p in token.replace("，", ",").split(",")]
                parts = [p.strip() for p in parts if p.strip() != ""]
                if len(parts) == 2:
                    try:
                        r, c = int(parts[0]), int(parts[1])
                    except ValueError as exc:
                        raise SurveyError(f"cannot parse position {value!r}") from exc
                    return normalize_position([r, c])
        raise SurveyError(f"cannot parse position {value!r}; expected index or 'row,col'")
    if isinstance(value, Mapping):
        for key in POSITION_ALIASES:
            if key in value:
                return normalize_position(value[key])
        r = next((value[k] for k in ROW_ALIASES if k in value), None)
        c = next((value[k] for k in COL_ALIASES if k in value), None)
        if r is not None and c is not None:
            return normalize_position([r, c])
        raise SurveyError(f"position object {value!r} needs position or row/col")
    if isinstance(value, (Sequence,)) and not isinstance(value, (bytes, bytearray)):
        if len(value) == 2:
            r, c = value
            if isinstance(r, int) and isinstance(c, int) and not isinstance(r, bool):
                if not (0 <= r < GRID_SIZE and 0 <= c < GRID_SIZE):
                    raise SurveyError(f"row/col {(r, c)} out of range 0..4")
                return r * GRID_SIZE + c
        raise SurveyError(f"position sequence {value!r} must be [row, col]")
    raise SurveyError(f"unsupported position {value!r}")


def position_rc(index: int) -> tuple[int, int]:
    return divmod(index, GRID_SIZE)


def mask_int(positions: Iterable[int]) -> int:
    m = 0
    for p in positions:
        m |= 1 << int(p)
    return m


def positions_of_mask(mask: int) -> tuple[int, ...]:
    return tuple(i for i in range(GRID_SIZE * GRID_SIZE) if mask & (1 << i))


def mask_grid(mask: int, blocked_char: str = "#", open_char: str = ".") -> list[list[str]]:
    return [
        [blocked_char if mask & (1 << (r * GRID_SIZE + c)) else open_char
         for c in range(GRID_SIZE)]
        for r in range(GRID_SIZE)
    ]


# ------------------------------------------------------------ survey input

_TOP_BLOCKED_KEYS = (
    "blocked", "blocked_positions", "blocked_cells", "blockedPositions",
    "positions", "occluded", "occluded_positions", "covered", "covered_positions",
    "fixture_cells", "jig_cells",
)
_TOP_OPENABLE_KEYS = (
    "openable", "openable_positions", "openable_cells", "drillable",
    "drillable_positions", "can_open", "can_open_positions", "allowed_holes",
    "holes_allowed", "hole_positions_allowed", "perforable",
)
_CELLS_KEYS = ("cells", "cell_list", "survey", "items")
_GRID_KEYS = ("mask", "blocked_grid", "fixture_grid", "occlusion_grid")

_OPENABLE_FLAGS = {"openable", "drillable", "can_open", "canOpen", "hole_allowed",
                   "allow_hole", "perforable", "can_drill", "drill"}
_BLOCKED_FLAGS = {"blocked", "is_blocked", "covered", "occluded"}


@dataclass
class Survey:
    blocked: tuple[int, ...]
    openable: tuple[int, ...]
    mode: str = MODE_ERRORS_0_1
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def non_openable(self) -> tuple[int, ...]:
        od = set(self.openable)
        return tuple(p for p in self.blocked if p not in od)


def _first_key(raw: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in raw and raw[k] is not None:
            return raw[k]
    # case-insensitive fallback
    lower = {str(k).lower(): v for k, v in raw.items()}
    for k in keys:
        if k.lower() in lower and lower[k.lower()] is not None:
            return lower[k.lower()]
    return None


def _as_position_list(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, str):
        # Could be a 25-char mask or a single "r,c".
        compact = value.strip()
        if len(compact) == GRID_SIZE * GRID_SIZE and all(ch in "01?xX#.oO2" for ch in compact):
            return _positions_from_mask_string(compact)
        return [normalize_position(compact)]
    if isinstance(value, Mapping):
        return [normalize_position(value)]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        # Flat 25-cell 0/1(/2) mask -- distinguish from a list of indices by
        # its length and scalar bit values (index lists are at most 12 long).
        if (
            len(value) == GRID_SIZE * GRID_SIZE
            and all(
                isinstance(v, (int, float, bool)) and int(v) in (0, 1, 2)
                for v in value
            )
        ):
            return [i for i, cell in enumerate(value) if _truthy_blocked(cell)]
        out: list[int] = []
        # Nested 5x5 mask?
        if len(value) == GRID_SIZE and all(isinstance(row, (Sequence,)) and not isinstance(
                row, (str, bytes, bytearray)) and len(row) == GRID_SIZE for row in value):
            for r, row in enumerate(value):
                for c, cell in enumerate(row):
                    if _truthy_blocked(cell):
                        out.append(r * GRID_SIZE + c)
            return out
        for item in value:
            out.append(normalize_position(item))
        return out
    raise SurveyError(f"cannot interpret position list {value!r}")


def _truthy_blocked(cell: Any) -> bool:
    if isinstance(cell, bool):
        return cell
    if cell is None:
        return False
    if isinstance(cell, (int, float)):
        return cell != 0
    if isinstance(cell, str):
        t = cell.strip()
        return t not in ("0", ".", "o", "O", "open", "false", "no", "白", "")
    return bool(cell)


def _positions_from_mask_string(s: str) -> list[int]:
    out = []
    for i, ch in enumerate(s):
        if ch in "1?xX#2":
            out.append(i)
    return out


def _extract_from_cells(cells: Any) -> tuple[list[int], list[int]]:
    """Handle the rich ``cells: [{position, openable}]`` representation."""
    blocked: list[int] = []
    openable: list[int] = []
    if not isinstance(cells, Sequence) or isinstance(cells, (str, bytes, bytearray)):
        raise SurveyError("cells must be a list of positions or cell descriptors")
    for item in cells:
        if isinstance(item, Mapping):
            pos = normalize_position(item)
            is_blocked = any(bool(item[k]) for k in _BLOCKED_FLAGS if k in item)
            is_openable = any(bool(item[k]) for k in _OPENABLE_FLAGS if k in item)
            # Descriptors in a survey are normally about blocked cells; if no
            # blocked flag is present but openable is true, infer blocked.
            if not is_blocked and (is_openable or not any(k in item for k in _BLOCKED_FLAGS)):
                is_blocked = True
            if is_blocked:
                blocked.append(pos)
            if is_openable:
                openable.append(pos)
        else:
            blocked.append(normalize_position(item))
    return blocked, openable


def extract_survey(raw: Any) -> Survey:
    """Parse any reasonable survey payload into a :class:`Survey`.

    The minimal wire format is ``{"blocked": [...], "openable": [...]}``;
    many aliases and a descriptor-based ``cells`` form are accepted.
    """
    if not isinstance(raw, Mapping):
        raise SurveyError("survey body must be a JSON object")

    blocked_raw = _first_key(raw, _TOP_BLOCKED_KEYS)
    openable_raw = _first_key(raw, _TOP_OPENABLE_KEYS)
    cells_raw = _first_key(raw, _CELLS_KEYS)
    grid_raw = _first_key(raw, _GRID_KEYS)

    blocked: list[int] = []
    openable: list[int] = []

    if cells_raw is not None:
        b, o = _extract_from_cells(cells_raw)
        blocked.extend(b)
        openable.extend(o)

    # A ``blocked`` list may itself contain descriptor objects carrying flags.
    if isinstance(blocked_raw, Sequence) and not isinstance(
        blocked_raw, (str, bytes, bytearray, Mapping)
    ) and any(isinstance(x, Mapping) for x in blocked_raw):
        b, o = _extract_from_cells(blocked_raw)
        blocked.extend(b)
        openable.extend(o)
    elif blocked_raw is not None:
        blocked.extend(_as_position_list(blocked_raw))
    if grid_raw is not None:
        blocked.extend(_as_position_list(grid_raw))
    if isinstance(openable_raw, Sequence) and not isinstance(
        openable_raw, (str, bytes, bytearray)
    ) and any(isinstance(x, Mapping) for x in openable_raw):
        _, o = _extract_from_cells(openable_raw)
        openable.extend(o)
    elif openable_raw is not None:
        openable.extend(_as_position_list(openable_raw))

    # Deduplicate while preserving order.
    blocked = sorted(set(blocked))
    openable = sorted(set(openable))

    blocked_source_present = any(x is not None for x in (blocked_raw, cells_raw, grid_raw))
    if not blocked and not blocked_source_present:
        raise SurveyError(
            "survey must include a blocked position list (at most 12 positions)"
        )
    if len(blocked) > MAX_BLOCKED:
        raise SurveyError(
            f"fixture blocks {len(blocked)} cells; a single fixture submits at most "
            f"{MAX_BLOCKED} blocked positions"
        )
    bad_open = [p for p in openable if p not in set(blocked)]
    if bad_open:
        raise SurveyError(
            f"openable positions {bad_open} must be among the blocked positions {blocked}"
        )

    mode_raw = raw.get("mode") or raw.get("safety_mode") or raw.get("error_model")
    robust = raw.get("robust_to_scan_error", raw.get("robust", None))
    mode = _resolve_mode(mode_raw, robust)

    return Survey(blocked=tuple(blocked), openable=tuple(openable), mode=mode)


def _resolve_mode(mode_raw: Any, robust: Any) -> str:
    if mode_raw is not None:
        token = str(mode_raw).strip().lower()
        if token in ("strong", "robust", "errors_0_1", "error", "full", "3"):
            return MODE_ERRORS_0_1
        if token in ("lenient", "matcher", "matcher_only", "tolerance", "tolerance_1", "2"):
            return MODE_MATCHER_ONLY
    if robust is not None:
        if isinstance(robust, str):
            return MODE_ERRORS_0_1 if robust.lower() in ("1", "true", "yes") else MODE_MATCHER_ONLY
        return MODE_ERRORS_0_1 if bool(robust) else MODE_MATCHER_ONLY
    return MODE_ERRORS_0_1


# ------------------------------------------------------------ verification

@dataclass(frozen=True)
class BoxCode:
    box_number: str
    grid: tuple[int | None, ...]


def _diff_mask(a: Sequence[int], b: Sequence[int]) -> int:
    m = 0
    for i in range(GRID_SIZE * GRID_SIZE):
        if a[i] != b[i]:
            m |= 1 << i
    return m


@dataclass
class Constraint:
    """A placed pair (primary box in placement p vs other in rotation q)."""

    primary: str
    other: str
    placement: int
    match_orientation: int
    diff_mask: int

    def visible_distance(self, mask: int) -> int:
        return (self.diff_mask & ~mask).bit_count()

    def diff_positions(self, mask: int | None = None) -> tuple[int, ...]:
        m = self.diff_mask if mask is None else (self.diff_mask & ~mask)
        return positions_of_mask(m)


def build_constraints(boxes: Sequence[BoxCode]) -> list[Constraint]:
    """All ordered distinct-box / placement / matcher-rotation constraints."""
    complete: list[tuple[str, tuple[int, ...]]] = []
    for bx in boxes:
        if any(v is None for v in bx.grid):
            # Registered boxes are complete by construction; be defensive and
            # treat None as a distinct value so nothing is silently ignored.
            complete.append((bx.box_number, tuple(0 if v is None else v for v in bx.grid)))  # type: ignore[arg-type]
        else:
            complete.append((bx.box_number, tuple(bx.grid)))  # type: ignore[arg-type]

    constraints: list[Constraint] = []
    for i, (na, ga) in enumerate(complete):
        placed = [rotate(ga, p) for p in range(4)]
        for j, (nb, gb) in enumerate(complete):
            if i == j:
                continue
            for p in range(4):
                for q in range(4):
                    constraints.append(
                        Constraint(na, nb, p, q, _diff_mask(placed[p], rotate(gb, q)))
                    )
    return constraints


@dataclass
class Conflict:
    box_numbers: tuple[str, str]
    primary_box_number: str
    other_box_number: str
    placement_orientation: int
    match_orientation: int
    visible_differences: int
    diff_positions: tuple[int, ...]
    simulated_error_at: int | None
    witness_grid: tuple[int | None, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "fixture_mask_ambiguous_for_two_boxes",
            "box_numbers": list(self.box_numbers),
            "primary_box_number": self.primary_box_number,
            "other_box_number": self.other_box_number,
            "placement_orientation": self.placement_orientation,
            "orientation": self.match_orientation,
            "match_orientation": self.match_orientation,
            "visible_differences": self.visible_differences,
            "diff_positions": list(self.diff_positions),
            "diff_coordinates": [list(position_rc(p)) for p in self.diff_positions],
            "simulated_error_at": self.simulated_error_at,
            "witness_grid": [
                list(self.witness_grid[r * GRID_SIZE:(r + 1) * GRID_SIZE])
                for r in range(GRID_SIZE)
            ],
        }


def _witness_for(
    c: Constraint, mask: int, placed_primary: Sequence[int]
) -> tuple[tuple[int | None, ...], int | None]:
    """Build a scan through ``mask`` that hits both boxes for this constraint."""
    witness: list[int | None] = [None if mask & (1 << i) else int(placed_primary[i])
                                 for i in range(GRID_SIZE * GRID_SIZE)]
    visible_diff = c.diff_mask & ~mask
    d = visible_diff.bit_count()
    error_at: int | None = None
    if d == 2:
        # Consume the one allowed field error on a differing cell so the scan
        # lands within one mismatch of both codes.
        e = (visible_diff & -visible_diff).bit_length() - 1
        witness[e] = 1 - int(placed_primary[e])
        error_at = e
    return tuple(witness), error_at


def mask_is_safe(
    remaining: Sequence[int],
    boxes: Sequence[BoxCode],
    mode: str = MODE_ERRORS_0_1,
    constraints: Sequence[Constraint] | None = None,
) -> bool:
    if mode not in REQUIRED_DISTANCE:
        raise SurveyError(f"unknown safety mode {mode!r}")
    required = REQUIRED_DISTANCE[mode]
    mask = mask_int(remaining)
    cs = constraints if constraints is not None else build_constraints(boxes)
    for c in cs:
        if c.visible_distance(mask) < required:
            return False
    return True


def evaluate_mask_detailed(
    remaining: Sequence[int],
    boxes: Sequence[BoxCode],
    mode: str = MODE_ERRORS_0_1,
    constraints: Sequence[Constraint] | None = None,
    max_pair_conflicts: int = 50,
) -> dict[str, Any]:
    """Return safety verdict plus one best witness per conflicting box pair.

    The full set of ``conflicting_box_numbers`` is always collected; only the
    detailed witness list is capped (one minimal-distance witness per
    unordered box pair).
    """
    if mode not in REQUIRED_DISTANCE:
        raise SurveyError(f"unknown safety mode {mode!r}")
    required = REQUIRED_DISTANCE[mode]
    mask = mask_int(remaining)
    cs = constraints if constraints is not None else build_constraints(boxes)

    grids_by_name = {bx.box_number: bx.grid for bx in boxes}
    # unordered pair -> (distance, constraint) keeping smallest distance.
    best_per_pair: dict[tuple[str, str], tuple[int, Constraint]] = {}
    conflicting_numbers: set[str] = set()
    unsafe_constraints = 0

    for c in cs:
        d = c.visible_distance(mask)
        if d >= required:
            continue
        unsafe_constraints += 1
        key = tuple(sorted((c.primary, c.other)))
        conflicting_numbers.update(key)  # type: ignore[arg-type]
        prev = best_per_pair.get(key)  # type: ignore[arg-type]
        order_key = (c.placement, c.match_orientation)
        if prev is None or d < prev[0] or (
            d == prev[0] and order_key < (prev[1].placement, prev[1].match_orientation)
        ):
            best_per_pair[key] = (d, c)  # type: ignore[index]

    ordered_pairs = sorted(best_per_pair.items(), key=lambda kv: (kv[1][0], kv[0]))
    conflicts: list[Conflict] = []
    for pair, (d, c) in ordered_pairs[:max_pair_conflicts]:
        primary_grid = grids_by_name[c.primary]
        placed_primary = rotate(primary_grid, c.placement)  # type: ignore[arg-type]
        witness, error_at = _witness_for(c, mask, placed_primary)
        conflicts.append(
            Conflict(
                box_numbers=pair,  # type: ignore[arg-type]
                primary_box_number=c.primary,
                other_box_number=c.other,
                placement_orientation=c.placement,
                match_orientation=c.match_orientation,
                visible_differences=d,
                diff_positions=c.diff_positions(mask),
                simulated_error_at=error_at,
                witness_grid=witness,
            )
        )

    safe = not best_per_pair
    return {
        "safe": safe,
        "mode": mode,
        "required_visible_distance": required,
        "remaining_blocked": sorted(remaining),
        "remaining_count": len(set(remaining)),
        "constraints_checked": len(cs),
        "unsafe_orientation_pairs": unsafe_constraints,
        "conflict_count": len(best_per_pair),
        "conflicts_truncated": len(best_per_pair) > max_pair_conflicts,
        "conflicting_box_numbers": sorted(conflicting_numbers),
        "conflicts": [c.as_dict() for c in conflicts],
    }


# ------------------------------------------------------------ minimum plan

@dataclass
class PlanResult:
    feasible: bool
    blocked: tuple[int, ...]
    openable: tuple[int, ...]
    mode: str
    holes: tuple[int, ...]
    remaining: tuple[int, ...]
    minimum_holes: int | None
    target_remaining: int | None
    reason: str | None
    safety: dict[str, Any]
    occlusion: dict[str, Any]
    alternatives: list[tuple[int, ...]]

    def as_dict(self) -> dict[str, Any]:
        holes_list = list(self.holes)
        remaining_list = list(self.remaining)
        return {
            "feasible": self.feasible,
            "solvable": self.feasible,
            "ok": self.feasible,
            "mode": self.mode,
            "safety_mode": self.mode,
            "reason": self.reason,
            "minimum_holes": self.minimum_holes,
            "min_holes": self.minimum_holes,
            "hole_count": self.minimum_holes,
            "minimum_openings": self.minimum_holes,
            "holes": holes_list,
            "open_positions": holes_list,
            "hole_positions": holes_list,
            "holes_to_open": holes_list,
            "holes_to_drill": holes_list,
            "openings": holes_list,
            "opening_positions": holes_list,
            "drill_positions": holes_list,
            "selected_holes": holes_list,
            "chosen_holes": holes_list,
            "holes_coordinates": [list(position_rc(p)) for p in self.holes],
            "remaining_blocked": remaining_list,
            "remaining": remaining_list,
            "blocked_after": remaining_list,
            "occluded_after": remaining_list,
            "remaining_occluded": remaining_list,
            "remaining_coordinates": [list(position_rc(p)) for p in self.remaining],
            "remaining_count": len(self.remaining),
            "blocked": list(self.blocked),
            "blocked_coordinates": [list(position_rc(p)) for p in self.blocked],
            "openable": list(self.openable),
            "drillable": list(self.openable),
            "non_openable": list(self.non_openable),
            "target_remaining": self.target_remaining,
            "occlusion": self.occlusion,
            "safety": self.safety,
            "safe": self.safety.get("safe", False),
            "conflicting_box_numbers": self.safety.get("conflicting_box_numbers", []),
            "conflicting_boxes": self.safety.get("conflicting_box_numbers", []),
            "conflicts": self.safety.get("conflicts", []),
            "alternative_hole_sets": [list(h) for h in self.alternatives],
            "fixture_mask_grid": mask_grid(mask_int(self.remaining)),
            "hole_grid": _hole_grid(self.blocked, self.holes),
        }

    @property
    def non_openable(self) -> tuple[int, ...]:
        od = set(self.openable)
        return tuple(p for p in self.blocked if p not in od)


def _hole_grid(blocked: Sequence[int], holes: Sequence[int]) -> list[list[str]]:
    hset = set(holes)
    bset = set(blocked)
    rows = []
    for r in range(GRID_SIZE):
        row = []
        for c in range(GRID_SIZE):
            i = r * GRID_SIZE + c
            if i in hset:
                row.append("O")  # drilled open
            elif i in bset:
                row.append("#")  # still blocked
            else:
                row.append(".")  # never covered
        rows.append(row)
    return rows


def plan_minimum(
    survey: Survey | Mapping[str, Any] | Sequence[BoxCode] | None = None,
    boxes: Sequence[BoxCode] | None = None,
    *,
    blocked: Sequence[int] | None = None,
    openable: Sequence[int] | None = None,
    mode: str | None = None,
    include_alternatives: int = 10,
) -> PlanResult:
    """Compute a minimum-hole safe plan.

    Can be called either with a :class:`Survey` as the first argument or with
    explicit ``blocked=`` / ``openable=`` keyword arguments.
    """
    if isinstance(survey, Survey):
        sv = survey
    elif isinstance(survey, Mapping):
        sv = extract_survey(survey)
    else:
        if blocked is None:
            raise SurveyError("plan_minimum needs a Survey or blocked= positions")
        b = tuple(sorted(set(normalize_position(p) for p in blocked)))
        o = tuple(sorted(set(normalize_position(p) for p in (openable or []))))
        sv = Survey(blocked=b, openable=o, mode=mode or MODE_ERRORS_0_1)

    if mode is not None:
        sv.mode = mode
    blocked_t = sv.blocked
    openable_set = set(sv.openable)
    mandatory = tuple(p for p in blocked_t if p not in openable_set)  # U: cannot open
    drillable = tuple(p for p in blocked_t if p in openable_set)

    occlusion = {
        "blocked_count": len(blocked_t),
        "openable_count": len(sv.openable),
        "non_openable_count": len(mandatory),
        "max_remaining_blocked": MAX_UNREADABLE,
        "minimum_holes_by_count": max(0, len(blocked_t) - MAX_UNREADABLE),
    }

    box_list: Sequence[BoxCode] = list(boxes or [])
    constraints = build_constraints(box_list)

    # Count impossibility: even opening every allowed hole leaves >4 blocked.
    if len(mandatory) > MAX_UNREADABLE:
        safety = evaluate_mask_detailed(
            mandatory, box_list, mode=sv.mode, constraints=constraints
        )
        return PlanResult(
            feasible=False, blocked=blocked_t, openable=sv.openable, mode=sv.mode,
            holes=sv.openable, remaining=mandatory, minimum_holes=None,
            target_remaining=None, reason="occlusion_exceeded",
            safety=safety, occlusion=occlusion, alternatives=[],
        )

    # Smallest achievable mask = mandatory U (open all drillable positions).
    best_safety = evaluate_mask_detailed(
        mandatory, box_list, mode=sv.mode, constraints=constraints
    )
    if not best_safety["safe"]:
        # Monotonicity: every other feasible remaining mask M is a superset of
        # U, which can only hide more differences, so it cannot be safe either.
        return PlanResult(
            feasible=False, blocked=blocked_t, openable=sv.openable, mode=sv.mode,
            holes=sv.openable, remaining=mandatory, minimum_holes=None,
            target_remaining=None, reason="ambiguous_pair",
            safety=best_safety, occlusion=occlusion, alternatives=[],
        )

    # A solution exists.  Minimize holes == maximize remaining mask size (<=4).
    max_target = min(MAX_UNREADABLE, len(blocked_t))
    chosen_remaining: tuple[int, ...] | None = None
    chosen_safety: dict[str, Any] | None = None
    alternatives: list[tuple[int, ...]] = []

    for target in range(max_target, len(mandatory) - 1, -1):
        extra_count = target - len(mandatory)
        for extra in itertools.combinations(drillable, extra_count):
            remaining = tuple(sorted(mandatory + extra))
            # Fast early-exiting predicate during search ...
            if not mask_is_safe(remaining, box_list, mode=sv.mode, constraints=constraints):
                continue
            if chosen_remaining is None:
                chosen_remaining = remaining
            elif len(alternatives) < include_alternatives:
                alternatives.append(
                    tuple(p for p in blocked_t if p not in set(remaining))
                )
                if len(alternatives) >= include_alternatives:
                    break
        if chosen_remaining is not None:
            break

    # The U-safety check guarantees at least the target=len(mandatory) mask.
    assert chosen_remaining is not None
    # ... build the rich witness/conflict report only for the chosen mask.
    chosen_safety = evaluate_mask_detailed(
        chosen_remaining, box_list, mode=sv.mode, constraints=constraints
    )
    holes = tuple(p for p in blocked_t if p not in set(chosen_remaining))
    occlusion["minimum_holes_by_safety"] = len(holes)
    return PlanResult(
        feasible=True, blocked=blocked_t, openable=sv.openable, mode=sv.mode,
        holes=holes, remaining=chosen_remaining, minimum_holes=len(holes),
        target_remaining=len(chosen_remaining), reason=None,
        safety=chosen_safety, occlusion=occlusion, alternatives=alternatives,
    )


def verify_remaining(
    remaining: Sequence[int],
    boxes: Sequence[BoxCode],
    mode: str = MODE_ERRORS_0_1,
) -> dict[str, Any]:
    """Re-verify an adopted plan's remaining mask against the current registry."""
    remaining_t = tuple(sorted(set(normalize_position(p) for p in remaining)))
    if len(remaining_t) > MAX_UNREADABLE:
        return {
            "safe": False,
            "mode": mode,
            "reason": "occlusion_exceeded",
            "remaining_blocked": list(remaining_t),
            "remaining_count": len(remaining_t),
            "conflicting_box_numbers": [],
            "conflicts": [],
        }
    return evaluate_mask_detailed(remaining_t, boxes, mode=mode)
