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
    sql = "SELECT run_id, subject, revision, size, count, created_at FROM runs"
    args = []
    if subject:
        sql += " WHERE subject=?"
        args.append(subject)
    sql += " ORDER BY created_at DESC LIMIT 100"
    return [dict(r) for r in db().execute(sql, args).fetchall()]


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
    return {"ok": True}


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
    return {"spec": json.loads(row["spec"]), "prompt": row["prompt"]}


# 静态评审页
STATIC_DIR = Path(__file__).parent / "web"
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="web")


def serve(port: int = 8870):
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=port)
