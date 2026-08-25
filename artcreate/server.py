"""artcreate · 评审视图服务（阶段4-A）：只读资产库+图目录，动作走同一 store

python -m artcreate serve [port]
"""
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .store import db, accept, reject, latest_run
from .tools.config import get_config

app = FastAPI(title="artcreate review")
cfg = get_config()


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
def do_accept(run_id: str, idx: int):
    row = db().execute("SELECT subject FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    accept(run_id, idx)
    return {"ok": True}


@app.post("/api/runs/{run_id}/candidates/{idx}/reject")
def do_reject(run_id: str, idx: int, code: str = "manual"):
    row = db().execute("SELECT subject FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    reject(run_id, idx, code)
    return {"ok": True}


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
