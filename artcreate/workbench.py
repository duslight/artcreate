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
    """工作台表单渲染所需全部字典 + 默认值 + 溯源。
    ui_hidden 条目不返回（表单精简；编译兼容由后端 spec 校验兜底）。"""
    _check_token(request, x_token)
    src_map = cfg.axis_source_map()
    axes = []
    for ax in cfg.constraint_axes:
        opts = []
        for o in ax.get("options", []):
            if o.get("ui_hidden"):
                continue
            item = dict(o)
            item["source"] = src_map.get((ax["id"], o.get("id")), "base")
            opts.append(item)
        axes.append({"id": ax["id"], "label": ax.get("label", ax["id"]),
                     "non_promotable": bool(ax.get("non_promotable")),
                     "options": opts})
    return {
        "defaults": cfg.defaults,
        "sizes": cfg.sizes,
        "art_styles": {k: v for k, v in cfg.art_styles.items()
                       if not v.get("ui_hidden")},
        "asset_types": {k: v for k, v in cfg.asset_types.items()
                        if not v.get("ui_hidden")},
        "moods": {k: v for k, v in cfg.moods.items()
                  if not v.get("ui_hidden")},
        "character_poses": cfg.character_poses,
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


# ---------- 蒸馏与提案（D22） ----------
@router.post("/api/distill/run")
def api_distill(request: Request, x_token: str = Header(None),
                x_actor_id: str = Header(None), x_actor_name: str = Header(None)):
    """手动触发一轮蒸馏（worker 也会周期跑）。"""
    _check_token(request, x_token)
    from . import distill
    distill.init_distill()
    return distill.run_distill(actor=_actor(x_actor_id, x_actor_name))


@router.get("/api/proposals")
def api_proposals(status: str = None, request: Request = None,
                  x_token: str = Header(None)):
    _check_token(request, x_token)
    from . import distill
    distill.init_distill()
    return distill.list_proposals(status)


@router.post("/api/proposals/{pid}/approve")
def api_approve(pid: int, request: Request, x_token: str = Header(None),
                x_actor_id: str = Header(None), x_actor_name: str = Header(None)):
    _check_token(request, x_token)
    from . import distill
    try:
        return distill.approve(pid, actor=_actor(x_actor_id, x_actor_name))
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.post("/api/proposals/{pid}/reject")
def api_reject_proposal(pid: int, request: Request, body: dict = None,
                        x_token: str = Header(None),
                        x_actor_id: str = Header(None),
                        x_actor_name: str = Header(None)):
    _check_token(request, x_token)
    from . import distill
    try:
        return distill.reject_proposal(pid, actor=_actor(x_actor_id, x_actor_name),
                                       reason=(body or {}).get("reason", ""))
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.post("/api/proposals/{pid}/undo")
def api_undo(pid: int, request: Request, x_token: str = Header(None),
             x_actor_id: str = Header(None), x_actor_name: str = Header(None)):
    """撤销晋升（恢复提案 pending，从 derived 移除）。"""
    _check_token(request, x_token)
    from . import distill
    try:
        return distill.undo(pid, actor=_actor(x_actor_id, x_actor_name))
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.post("/api/proposals/{pid}/undo-reject")
def api_undo_reject(pid: int, request: Request, x_token: str = Header(None),
                    x_actor_id: str = Header(None), x_actor_name: str = Header(None)):
    _check_token(request, x_token)
    from . import distill
    try:
        return distill.undo_reject(pid, actor=_actor(x_actor_id, x_actor_name))
    except ValueError as e:
        raise HTTPException(422, str(e))


# ---------- 角色线（阶段7） ----------
@router.get("/api/characters/{name}/anchor")
def api_get_anchor(name: str, request: Request, x_token: str = Header(None)):
    _check_token(request, x_token)
    from . import character
    a = character.get_anchor(name)
    if not a:
        raise HTTPException(404, "该角色尚无锚点")
    return {"anchor": a, "lineage": character.anchor_lineage(name)}


@router.post("/api/characters/{name}/anchor")
def api_set_anchor(name: str, request: Request, body: dict,
                   x_token: str = Header(None),
                   x_actor_id: str = Header(None), x_actor_name: str = Header(None)):
    """body: {image_path, note?, from_candidate?}（image_path 相对仓库根）。"""
    _check_token(request, x_token)
    from . import character
    if not body.get("image_path"):
        raise HTTPException(422, "image_path 必填")
    aid = character.set_anchor(name, body["image_path"],
                               body.get("note", ""),
                               actor=_actor(x_actor_id, x_actor_name),
                               from_candidate=body.get("from_candidate"))
    return {"anchor_id": aid}


@router.get("/api/runs/{run_id}/consistency")
def api_consistency(run_id: str, request: Request, x_token: str = Header(None)):
    """run 候选 vs 角色锚点一致性报告（评审参考，非门禁）。"""
    _check_token(request, x_token)
    from . import character
    row = store.db().execute("SELECT subject FROM runs WHERE run_id=?",
                             (run_id,)).fetchone()
    if not row:
        raise HTTPException(404, "run 不存在")
    return {"subject": row["subject"],
            "reports": character.check_run_consistency(row["subject"], run_id)}


@router.post("/api/characters/{name}/pose-batch")
def api_pose_batch(name: str, request: Request, body: dict,
                   x_token: str = Header(None),
                   x_actor_id: str = Header(None),
                   x_actor_name: str = Header(None)):
    """7-B 动作批量：body={poses:["idle","attack"], count_each?:4, description?}。
    前置：角色须有锚点。数量守卫：poses≤8、count_each≤8 防误烧钱。"""
    _check_token(request, x_token)
    from . import character, jobs as jobs_mod
    poses = body.get("poses", [])
    count_each = body.get("count_each", 4)
    # ---- 守卫 ----
    if not poses or not isinstance(poses, list):
        raise HTTPException(422, "poses 必填且为数组")
    if len(poses) > 8:
        raise HTTPException(422, "单批动作不超过 8 个（防误烧钱）")
    if count_each > 8:
        raise HTTPException(422, "每动作候选不超过 8 张（防误烧钱）")
    pose_dict = cfg.character_poses
    bad = [p for p in poses if p not in pose_dict]
    if bad:
        raise HTTPException(422, f"未知动作：{bad}（现有：{sorted(pose_dict)}）")
    # ---- 锚点前置校验 ----
    if not character.get_anchor(name):
        raise HTTPException(404, f"角色 {name} 尚无锚点，先 set_anchor 再批量生成")
    # ---- 入队 ----
    spec = {"pose_batch": {
        "character": name,
        "poses": poses,
        "count_each": count_each,
        "description": body.get("description", ""),
    }}
    jobs_mod.init_jobs()
    job_id = jobs_mod.submit_job(spec, _actor(x_actor_id, x_actor_name))
    return {"job_id": job_id, "poses": poses, "count_each": count_each}
