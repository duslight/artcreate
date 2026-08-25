"""artcreate · 服务端工作台 API（阶段5）

- GET /api/form/dict        表单字典（sizes/styles/asset_types/moods/axes，
                            含 axis_source 溯源徽标数据 + defaults）
- POST /api/jobs            提交生成任务（spec 校验 → 入队）
- GET  /api/jobs            任务列表
- GET  /api/jobs/{id}       任务状态轮询
- POST /api/runs/{id}/regenerate  再生（也走队列）

访问令牌：环境变量 WORKBENCH_TOKEN 配置后，全部 API 需带 X-Token 头
（或 ?token= 查询参数，方便手机浏览器直接打开）。未配置则放行（本地模式）。
挂载方式：server.app.include_router(workbench.router)
"""
import json
import os

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse

from . import store, jobs
from .tools.config import get_config
from .tools.spec_validate import validate_spec, format_issues

router = APIRouter()

cfg = get_config()


def _check_token(request: Request, x_token: str = Header(None)):
    """互信+令牌双层：本地不配 token 全放行；配置了就强制。"""
    need = os.getenv("WORKBENCH_TOKEN", "").strip()
    if not need:
        return
    got = x_token or request.query_params.get("token") or ""
    if got != need:
        raise HTTPException(401, "无效访问令牌（X-Token 头或 ?token= 参数）")


def _actor(x_actor_id: str = None, x_actor_name: str = None) -> dict:
    return {"id": x_actor_id or "web-anonymous", "name": x_actor_name or ""}


# ---------- 表单字典 ----------
@router.get("/api/form/dict")
def form_dict(request: Request, x_token: str = Header(None)):
    """工作台表单渲染所需全部字典 + 默认值 + 溯源。"""
    _check_token(request, x_token)
    src_map = cfg.axis_source_map()
    axes = []
    for ax in cfg.constraint_axes:
        opts = []
        for o in ax.get("options", []):
            item = dict(o)
            item["source"] = src_map.get((ax["id"], o.get("id")), "base")
            opts.append(item)
        axes.append({"id": ax["id"], "label": ax.get("label", ax["id"]),
                     "non_promotable": bool(ax.get("non_promotable")),
                     "options": opts})
    return {
        "defaults": cfg.defaults,
        "sizes": cfg.sizes,
        "art_styles": cfg.art_styles,
        "asset_types": cfg.asset_types,
        "moods": cfg.moods,
        "constraint_axes": axes,
    }


# ---------- 任务 ----------
@router.post("/api/jobs")
def create_job(request: Request, spec: dict,
               x_token: str = Header(None),
               x_actor_id: str = Header(None), x_actor_name: str = Header(None)):
    _check_token(request, x_token)
    issues = validate_spec(spec)
    errors = [i for i in issues if i["level"] == "error"]
    if errors:
        raise HTTPException(422, {"issues": issues, "message": format_issues(issues)})
    jobs.init_jobs()
    job_id = jobs.submit_job(spec, _actor(x_actor_id, x_actor_name))
    return {"job_id": job_id, "warnings": [i for i in issues if i["level"] == "warn"]}


@router.get("/api/jobs")
def job_list(request: Request, x_token: str = Header(None)):
    _check_token(request, x_token)
    jobs.init_jobs()
    return jobs.list_jobs()


@router.get("/api/jobs/{job_id}")
def job_status(job_id: int, request: Request, x_token: str = Header(None)):
    _check_token(request, x_token)
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404)
    return job


@router.post("/api/runs/{run_id}/regenerate")
def web_regenerate(run_id: str, request: Request, x_token: str = Header(None),
                   x_actor_id: str = Header(None), x_actor_name: str = Header(None)):
    """从已有 run 再生一批（走队列，parent_run 溯源）。"""
    _check_token(request, x_token)
    row = store.db().execute(
        "SELECT spec FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        raise HTTPException(404, "run 不存在")
    spec = json.loads(row["spec"])
    spec["parent_run"] = run_id
    jobs.init_jobs()
    job_id = jobs.submit_job(spec, _actor(x_actor_id, x_actor_name))
    return {"job_id": job_id, "parent_run": run_id}
