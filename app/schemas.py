"""Pydantic request models and /docs examples."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from .codes import (
    parse_grid,
    to_matrix,
    validate_for_registration,
    validate_for_scan,
)
from .demo import DEMO_BLOCKER, DEMO_SCAN, DEMO_TAG, DEMO_VALID_NEW
from .fixture_demo import BREAK_BOX_NUMBER, FIX_BREAK

Grid = list[list[Optional[int]]]


class BoxCreate(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        json_schema_extra={
            "example": {"box_number": "BOX-DEMO-BLOCKER", "grid": DEMO_BLOCKER}
        },
    )

    box_number: Optional[str] = Field(
        default=None,
        description="箱号；省略时由系统分配 BX-0001 样式编号。",
        validation_alias=AliasChoices(
            "box_number", "boxNumber", "box_no", "boxNo", "number"
        ),
    )
    grid: Grid = Field(
        description="完整的 5×5 黑白码，单元格取 0/1。也接受一维 25 格或 5 个字符串行。",
        validation_alias=AliasChoices("grid", "code", "matrix", "cells", "pattern"),
    )

    @field_validator("grid", mode="before")
    @classmethod
    def _coerce_and_validate(cls, value: Any) -> Grid:
        flat = parse_grid(value)
        validate_for_registration(flat)
        return to_matrix(flat)  # type: ignore[return-value]


class ScanCreate(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        json_schema_extra={"example": {"scanner_id": "demo-gate", "grid": DEMO_SCAN}},
    )

    scanner_id: Optional[str] = Field(
        default=None,
        description="扫码器标识（可选）。",
        validation_alias=AliasChoices("scanner_id", "scannerId", "scanner"),
    )
    grid: Grid = Field(
        description=(
            "拉正后的 5×5 格阵。看不清的格子用 null（或 '?'、'x'、-1、2），"
            "最多 4 格；可见格上某旋转朝向至多错 1 格即命中。"
        ),
        validation_alias=AliasChoices("grid", "scan", "matrix", "cells", "pattern"),
    )

    @field_validator("grid", mode="before")
    @classmethod
    def _coerce_and_validate(cls, value: Any) -> Grid:
        flat = parse_grid(value)
        validate_for_scan(flat)
        return to_matrix(flat)  # type: ignore[return-value]


# ------------------------------------------------------- /docs examples

BOX_EXAMPLES: dict[str, dict[str, Any]] = {
    "demo_blocked_by_occluded_reading": {
        "summary": "拦截示例：被一条遮挡确认读数拦下的新牌（预期 409）",
        "description": (
            "需先调用 POST /api/demo/seed。该码与旧箱完整码相差 4 格，"
            "但与那条四角遮挡读数在可见格上 0 误差。"
        ),
        "value": {"box_number": "BOX-DEMO-BLOCKER", "grid": DEMO_BLOCKER},
    },
    "valid_new_tag": {
        "summary": "可成功挂牌的差异明显新牌（预期 201）",
        "value": {"box_number": "BOX-DEMO-CHECK", "grid": DEMO_VALID_NEW},
    },
    "register_first_box": {
        "summary": "登记演示旧箱 BOX-DEMO-01",
        "value": {"box_number": "BOX-DEMO-01", "grid": DEMO_TAG},
    },
    "fixture_break_withdraws_plan": {
        "summary": "开孔演示：合法新牌 FIX-BREAK，会令生效方案歧义并触发撤下（预期 201 + withdrawn）",
        "description": (
            "需先 POST /api/demo/fixture-seed 并 adopt PLAN-FIX-01。"
            "该牌与 FIX-A 完整码仅差 2 格（正常挂牌合法），但两格 6/8 正在生效方案的遮挡掩码内，"
            "于是方案在同一事务被撤下，响应 fixture_plan.conflicting_box_numbers 给出冲突箱号。"
        ),
        "value": {"box_number": BREAK_BOX_NUMBER, "grid": FIX_BREAK},
    },
}

SCAN_EXAMPLES: dict[str, dict[str, Any]] = {
    "occluded_corners": {
        "summary": "遮挡示例：四角看不清（4 格），唯一命中并确认",
        "value": {"scanner_id": "demo-gate", "grid": DEMO_SCAN},
    },
    "clear_complete_code": {
        "summary": "清晰完整码（0 遮挡）",
        "value": {"scanner_id": "gate-7", "grid": DEMO_TAG},
    },
    "too_unreadable": {
        "summary": "非法：5 格看不清（预期 422）",
        "value": {
            "scanner_id": "gate-7",
            "grid": [
                [None, None, 1, 1, 1],
                [1, 0, 0, 0, 1],
                [1, 0, 1, 0, 1],
                [1, 0, 0, 0, 1],
                [None, None, None, 1, 1],
            ],
        },
    },
}


# Examples for the fixture hole-plan endpoints.
FIXTURE_SURVEY_EXAMPLES: dict[str, dict[str, Any]] = {
    "fixture_survey_minimum": {
        "summary": "开孔演示勘测：8 格被挡、6 格可开，最少开 4 孔（PLAN-FIX-01）",
        "description": "位置为行主序 0..24；也接受 [row,col]、'r,c'、cells 描述或 5×5 掩码。",
        "value": {"blocked": [1, 6, 7, 8, 11, 12, 13, 17],
                  "openable": [1, 7, 11, 12, 13, 17]},
    },
    "fixture_ambiguous_witness": {
        "summary": "无解·双箱共命中格阵：全打开仍遮住 FIX-C/FIX-D 的两处差异（预期 feasible=false）",
        "value": {"blocked": [0, 4, 24, 5, 9, 15], "openable": [24, 5, 9, 15]},
    },
    "fixture_occlusion_exceeded": {
        "summary": "无解·遮挡超限：5 个不可开孔位 > 4（预期 reason=occlusion_exceeded）",
        "value": {"blocked": [0, 1, 2, 3, 4, 5], "openable": [5]},
    },
    "fixture_cells_descriptor": {
        "summary": "cells 描述形式：逐项标 blocked/openable",
        "value": {"cells": [
            {"position": 6, "blocked": True},
            {"position": 8, "blocked": True},
            {"position": 11, "blocked": True, "openable": True},
            {"row": 2, "col": 2, "blocked": True, "openable": True},
        ]},
    },
}

FIXTURE_ADOPT_EXAMPLES: dict[str, dict[str, Any]] = {
    "adopt_demo_plan": {
        "summary": "采用 compose/seed 预建的 PLAN-FIX-01（按当前牌册复核）",
        "value": {"plan_id": "PLAN-FIX-01"},
    },
}
