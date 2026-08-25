"""artcreate · 资产注册表（SQLite）：runs + candidates 两表，状态机与生命周期记录

状态机（D-工厂三件套）：
  candidate: generated → gated → accepted | rejected
  拒绝可带拒绝码；accepted 的候选成为资产定稿（subject 下唯一）。
"""
import json
import sqlite3
import threading
from pathlib import Path

from .tools.config import get_config

_LOCK = threading.Lock()
_DB = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    subject     TEXT NOT NULL,
    revision    INTEGER NOT NULL DEFAULT 1,
    spec        TEXT NOT NULL,            -- 完整 spec JSON
    prompt      TEXT NOT NULL,
    segments    TEXT NOT NULL,            -- 分段 prompt JSON（消融/溯源）
    provider    TEXT,
    model       TEXT,
    size        TEXT,
    count       INTEGER,
    ref_images  TEXT,                     -- JSON 数组或 NULL
    lint_warnings TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS candidates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    idx         INTEGER NOT NULL,         -- 候选序号（1 起）
    file        TEXT NOT NULL,            -- 相对 exports 的路径
    w           INTEGER, h INTEGER,
    grid_w      INTEGER, grid_h INTEGER,
    status      TEXT NOT NULL DEFAULT 'generated',  -- generated|gated|accepted|rejected
    reject_code TEXT,                     -- 拒绝码（rejected 时）
    gate_report TEXT,                     -- 门禁报告 JSON（gated 后）
    decided_at  TEXT,
    UNIQUE(run_id, idx)
);

CREATE INDEX IF NOT EXISTS idx_cand_status ON candidates(status);
CREATE INDEX IF NOT EXISTS idx_runs_subject ON runs(subject);
"""


def db() -> sqlite3.Connection:
    global _DB
    with _LOCK:
        if _DB is None:
            path = get_config().root / "assets.db"
            _DB = sqlite3.connect(str(path))
            _DB.row_factory = sqlite3.Row
            _DB.executescript(SCHEMA)
            _DB.commit()
        return _DB


def register_run(manifest: dict):
    """run 命令产出 manifest 后注册。返回 run_id。"""
    conn = db()
    conn.execute(
        "INSERT INTO runs (run_id, subject, revision, spec, prompt, segments,"
        " provider, model, size, count, ref_images, lint_warnings)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (manifest["run_id"], manifest["subject"], manifest["spec_revision"],
         json.dumps(manifest["spec"], ensure_ascii=False),
         manifest["prompt"],
         json.dumps(manifest["prompt_segments"], ensure_ascii=False),
         manifest["provider"], manifest["model"], manifest["size"],
         manifest["count"],
         json.dumps(manifest.get("ref_images")) if manifest.get("ref_images") else None,
         json.dumps(manifest.get("lint_warnings", []), ensure_ascii=False)))
    for c in manifest["candidates"]:
        conn.execute(
            "INSERT INTO candidates (run_id, idx, file, w, h, grid_w, grid_h, status)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (manifest["run_id"], c["index"], c["file"], c["w"], c["h"],
             c["grid_w"], c["grid_h"], "generated"))
    conn.commit()
    return manifest["run_id"]


def set_status(run_id: str, idx: int, status: str, reject_code: str = None,
               gate_report: dict = None):
    conn = db()
    conn.execute(
        "UPDATE candidates SET status=?, reject_code=?, gate_report=?," 
        " decided_at=datetime('now','localtime') WHERE run_id=? AND idx=?",
        (status, reject_code,
         json.dumps(gate_report, ensure_ascii=False) if gate_report else None,
         run_id, idx))
    conn.commit()


def accept(run_id: str, idx: int):
    """接受候选：本候选 accepted；同 subject 同 revision 的其余 accepted 候选降级 rejected(auto_superseded)。"""
    conn = db()
    row = conn.execute(
        "SELECT r.subject, r.revision FROM runs r WHERE r.run_id=?",
        (run_id,)).fetchone()
    if not row:
        raise ValueError(f"run {run_id} 不存在")
    conn.execute(
        "UPDATE candidates SET status='rejected', reject_code='auto_superseded',"
        " decided_at=datetime('now','localtime')"
        " WHERE status='accepted' AND run_id IN"
        " (SELECT run_id FROM runs WHERE subject=? AND revision=?)",
        (row["subject"], row["revision"]))
    set_status(run_id, idx, "accepted")


def reject(run_id: str, idx: int, code: str = "manual"):
    set_status(run_id, idx, "rejected", reject_code=code)


def latest_run(subject: str):
    return db().execute(
        "SELECT * FROM runs WHERE subject=? ORDER BY created_at DESC, run_id DESC LIMIT 1",
        (subject,)).fetchone()


def list_candidates(subject: str = None, status: str = None):
    sql = ("SELECT c.*, r.subject, r.revision FROM candidates c"
           " JOIN runs r ON c.run_id = r.run_id WHERE 1=1")
    args = []
    if subject:
        sql += " AND r.subject=?"
        args.append(subject)
    if status:
        sql += " AND c.status=?"
        args.append(status)
    sql += " ORDER BY r.created_at DESC, c.idx"
    return db().execute(sql, args).fetchall()
