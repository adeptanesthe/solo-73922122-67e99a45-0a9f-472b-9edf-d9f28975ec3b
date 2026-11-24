"""HTTP routes for the single-fixture hole-opening plan workflow."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from . import fixture_demo, plans_service

router = APIRouter(tags=["fixture-plans"])


# ------------------------------------------------------------- evaluation

@router.post("/api/fixture/plans/evaluate",
             summary="只计算：提交遮挡/可开孔位，返回开孔数最少方案（不入库不生效）")
def evaluate_plan(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return plans_service.evaluate_survey(payload)


@router.post("/api/fixture/plan/evaluate", include_in_schema=False)
def evaluate_plan_alias_singular(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return plans_service.evaluate_survey(payload)


# -------------------------------------------------------------- proposals

@router.post("/api/fixture/plans",
             summary="计算并保存一个开孔方案提案；可行 201，无解 409（含冲突箱号/格阵）")
def create_plan(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    plan_no = payload.get("plan_no") or payload.get("plan_number") or payload.get("idempotency_key")
    data = plans_service.create_proposal(payload, plan_no=plan_no)
    if not data.get("feasible"):
        return JSONResponse(status_code=409, content=data)
    return JSONResponse(status_code=201, content=data)


@router.get("/api/fixture/plans", summary="开孔方案历史列表（含状态与冲突箱号）")
def list_plans(limit: int = 100) -> dict[str, Any]:
    return plans_service.list_plans(limit=limit)


@router.get("/api/fixture/plans/current", include_in_schema=False)
def get_current_plan_alias_path() -> dict[str, Any]:
    return plans_service.current_state()


@router.get("/api/fixture/plans/active", include_in_schema=False)
def get_active_plan_alias_path() -> dict[str, Any]:
    return plans_service.current_state()


@router.get("/api/fixture/plans/latest", include_in_schema=False)
def get_latest_plan_alias_path() -> JSONResponse:
    state = plans_service.current_state()
    latest = state.get("latest_plan")
    if latest is None:
        return JSONResponse(status_code=404, content={"error": "no_plan_yet"})
    return JSONResponse(status_code=200, content=latest)


@router.get("/api/fixture/plans/{plan_id}",
            summary="查看单个方案（PLAN-0001 / 数字 id / 演示编号 PLAN-FIX-01）")
def get_plan(plan_id: str) -> JSONResponse:
    data = plans_service.get_plan(plan_id)
    if data is None:
        return JSONResponse(status_code=404,
                            content={"error": "plan_not_found", "plan_id": plan_id})
    return JSONResponse(status_code=200, content=data)


# ------------------------------------------------------------------ adopt

@router.post("/api/fixture/plans/adopt",
             summary="采用已算好的方案：按当前牌册复核，安全才生效，否则 409 且保留原状态")
def adopt_plan_body(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return plans_service.adopt_plan(payload=payload)


@router.post("/api/fixture/plans/{plan_id}/adopt",
             summary="按编号采用方案（复核当前牌册）")
def adopt_plan_path(plan_id: str,
                    payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    return plans_service.adopt_plan(identifier=plan_id, payload=payload or {})


@router.post("/api/fixture/plan/adopt", include_in_schema=False)
def adopt_plan_singular(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return plans_service.adopt_plan(payload=payload)


# --------------------------------------------------------------- withdraw

@router.post("/api/fixture/plans/{plan_id}/withdraw",
             summary="撤下指定方案（若它是当前生效方案）")
def withdraw_plan_path(plan_id: str) -> dict[str, Any]:
    current = plans_service.current_state()
    if current.get("active_plan_no") == plan_id:
        return plans_service.withdraw_active_plan(reason="manual")
    data = plans_service.get_plan(plan_id)
    return {"withdrawn": False, "plan_id": plan_id,
            "status": data.get("status") if data else None,
            "message": "plan is not the active plan"}


@router.post("/api/fixture/plan/withdraw",
             summary="手动撤下当前生效方案")
def withdraw_current(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    reason = "manual"
    if payload and payload.get("reason"):
        reason = str(payload["reason"])
    return plans_service.withdraw_active_plan(reason=reason)


# ----------------------------------------------------------------- state

@router.get("/api/fixture/plan",
            summary="查询单台夹具当前方案：生效状态、实时复核与冲突箱号")
def get_current_plan() -> dict[str, Any]:
    return plans_service.current_state()


@router.post("/api/fixture/plan",
             summary="提交现场遮挡勘测并保存提案；adopt=true 时复核并直接生效")
def submit_current_plan(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    plan_no = payload.get("plan_no") or payload.get("plan_number")
    proposal = plans_service.create_proposal(payload, plan_no=plan_no)
    should_adopt = bool(payload.get("adopt") or payload.get("activate")
                        or payload.get("apply"))
    if should_adopt and proposal.get("feasible"):
        adopted = plans_service.adopt_plan(proposal["plan_no"])
        adopted["proposal"] = {
            "plan_id": proposal["plan_id"], "minimum_holes": proposal["minimum_holes"]
        }
        return adopted
    proposal["adopt_requested"] = should_adopt
    return proposal


# ------------------------------------------------------------------- demo

@router.post("/api/demo/fixture-seed", tags=["demo"],
             summary="幂等准备开孔方案 /docs 演示数据（FIX-A/C/D + PLAN-FIX-01）")
def demo_fixture_seed(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    reset = bool(payload and (payload.get("reset") or payload.get("fresh")))
    return fixture_demo.seed_fixture_demo(reset=reset)


@router.post("/api/demo/fixture-reset", tags=["demo"],
             summary="重置开孔演示：删除 FIX-% 箱、读数与全部方案/生效状态")
def demo_fixture_reset() -> dict[str, Any]:
    return fixture_demo.reset_fixture_demo()


@router.get("/api/demo/fixture-scenario", tags=["demo"],
            summary="开孔方案演示脚本、预填请求体与当前状态")
def demo_fixture_scenario() -> dict[str, Any]:
    return fixture_demo.fixture_scenario_payload()
