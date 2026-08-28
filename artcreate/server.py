"""artcreate · 评审视图服务（阶段4-A + 3.6 日志系统）
只读资产库+图目录+事件时间线，动作走同一 store

python -m artcreate serve [port]

轻量身份（D22）：客户端 localStorage 存 UUID+显示名，每次请求通过
X-Actor-Id / X-Actor-Name 头自报家门；服务端不做鉴权（互信模型），
actor.role 预留给未来 owner 升格。日志只记事实，不改历史。
"""
import json
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import store
from . import workbench
from .store import db, accept, reject, latest_run
from .tools.config import get_config

app = FastAPI(title="artcreate review")
app.include_router(workbench.router)
cfg = get_config()


def _actor(x_actor_id: str = None, x_actor_name: str = None) -> dict:
    """从请求头解析轻量身份；缺省 'web-anonymous'（仍留痕）。"""
    return {"id": x_actor_id or "web-anonymous",
            "name": x_actor_name or ""}


@app.get("/api/runs")
def list_runs(subject: str = None):
    sql = ("SELECT run_id, subject, revision, size, count, created_at, spec"
           " FROM runs")
    args = []
    if subject:
        sql += " WHERE subject=?"
        args.append(subject)
    sql += " ORDER BY created_at DESC LIMIT 100"
    # run spec → 产出页面推断（历史列表徽标：避免误触其他模块的 run）
    # 单点化锚点：前端 workbench.html pageOfSpec() 与此规则同步修改
    # （前端优先采信此处返回的 page 字段，推断仅 fallback）
    def _page_of(spec: dict) -> str:
        at = str(spec.get("asset_type") or "")
        if spec.get("vfx") or at.startswith("vfx_"):
            return "vfx"
        if (spec.get("character") or {}).get("pose"):
            return "anim"
        if at.startswith("monster_"):
            return "monster"
        if at.startswith("character_"):
            return "pose"
        return "new"
    _labels = {"new": "场景", "pose": "角色", "monster": "怪物",
               "anim": "角色动作", "vfx": "特效"}
    out = []
    for r in db().execute(sql, args).fetchall():
        item = dict(r)
        # 从 spec 提取 pose 信息供评审页显示徽标
        try:
            spec = json.loads(item.pop("spec", "{}"))
            ch = spec.get("character") or {}
            if ch.get("pose"):
                item["pose"] = ch["pose"]
            item["page"] = _page_of(spec)
            item["page_label"] = _labels.get(item["page"], "场景")
        except Exception:
            item.pop("spec", None)
            item["page"] = "new"
            item["page_label"] = "场景"
        out.append(item)
    return out


@app.get("/api/runs/{run_id}/candidates")
def run_candidates(run_id: str):
    run = db().execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if not run:
        raise HTTPException(404, "run 不存在")
    cands = db().execute(
        "SELECT idx, file, w, h, grid_w, grid_h, status, reject_code, gate_report"
        " FROM candidates WHERE run_id=? ORDER BY idx", (run_id,)).fetchall()
    out = []
    for c in cands:
        item = dict(c)
        if item["gate_report"]:
            try:
                item["gate_report"] = json.loads(item["gate_report"])
            except Exception:
                item["gate_report"] = None
        out.append(item)
    return {"run": {"run_id": run["run_id"], "subject": run["subject"],
                    "revision": run["revision"], "size": run["size"],
                    "prompt": run["prompt"]},
            "candidates": out}


@app.post("/api/runs/{run_id}/candidates/{idx}/accept")
def do_accept(run_id: str, idx: int,
              x_actor_id: str = Header(None), x_actor_name: str = Header(None)):
    row = db().execute("SELECT subject FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    accept(run_id, idx, actor=_actor(x_actor_id, x_actor_name))
    # 返回发布结果（spec 未命名时 published=None，行为与旧版一致）
    from .publish import resolve_final_name
    import json as _json
    spec_row = db().execute("SELECT spec FROM runs WHERE run_id=?", (run_id,)).fetchone()
    stem = resolve_final_name(_json.loads(spec_row["spec"]))
    return {"ok": True, "final_name": stem}


@app.post("/api/runs/{run_id}/candidates/{idx}/reject")
def do_reject(run_id: str, idx: int, code: str = "manual",
              x_actor_id: str = Header(None), x_actor_name: str = Header(None)):
    row = db().execute("SELECT subject FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    reject(run_id, idx, code, actor=_actor(x_actor_id, x_actor_name))
    return {"ok": True}


@app.get("/api/events")
def get_events(actor_id: str = None, action: str = None, run_id: str = None,
               subject: str = None, limit: int = 50, offset: int = 0):
    """操作时间线（倒序）。支持按人/动作/run/subject 筛选。"""
    limit = max(1, min(limit, 200))
    return store.list_events(actor_id=actor_id, action=action, run_id=run_id,
                             subject=subject, limit=limit, offset=offset)


@app.get("/api/runs/{run_id}/lineage")
def get_lineage(run_id: str):
    """run 迭代树：全部祖先 + 直系子代（跑了几轮、每轮从哪来）。"""
    row = db().execute("SELECT run_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        raise HTTPException(404, "run 不存在")
    return store.run_lineage(run_id)


@app.get("/api/images/{subject}/{run_id}/{name}")
def get_image(subject: str, run_id: str, name: str):
    p = cfg.root / "exports" / subject / run_id / name
    if not p.is_file():
        raise HTTPException(404)
    return FileResponse(p)


@app.get("/api/spec/{run_id}")
def get_spec(run_id: str):
    row = db().execute("SELECT spec, prompt FROM runs WHERE run_id=?",
                       (run_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    from .explain import explain_spec
    return {"spec": json.loads(row["spec"]), "prompt": row["prompt"],
            "explain": explain_spec(json.loads(row["spec"]))}


# 静态评审页
STATIC_DIR = Path(__file__).parent / "web"


# HTML 入口禁缓存：页面 JS 迭代频繁，浏览器缓存旧版会踩"修复不生效"的坑
# （图片等静态资源仍走 StaticFiles 默认策略，只拦 .html）
@app.get("/workbench.html")
def _workbench_nocache():
    return FileResponse(STATIC_DIR / "workbench.html",
                        headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/index.html")
def _index_nocache():
    return FileResponse(STATIC_DIR / "index.html",
                        headers={"Cache-Control": "no-cache, must-revalidate"})


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="web")


def serve(port: int = 8870, host: str = "127.0.0.1"):
    import uvicorn
    uvicorn.run(app, host=host, port=port)
