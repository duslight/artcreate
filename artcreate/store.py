"""artcreate · 资产注册表（SQLite）：runs + candidates + events 三表
状态机、生命周期记录、append-only 操作事实流（D22 日志系统）

状态机（D-工厂三件套）：
  candidate: generated → gated → accepted | rejected
  拒绝可带拒绝码；accepted 的候选成为资产定稿（subject 下唯一）。

events（日志系统）：只增不改，用户视角的不可变事实流——
  谁(actor) 何时(ts) 做了什么(action) 对象是谁(target/run)。
  记录点挂在写操作入口（register_run/accept/reject/…），不靠手记。
  撤销类动作（undo 晋升等）本身也是一条 event，历史永不改写。
  actor 来自轻量身份（UUID+显示名），未来晋升 owner 权限时只加判断不改模型。
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
    parent_run  TEXT,                     -- 再生来源（迭代树溯源链）
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

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT DEFAULT (datetime('now','localtime')),
    actor_id    TEXT NOT NULL,            -- 轻量身份 UUID（本地环境缺省 'cli'）
    actor_name  TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL,            -- run_created|accepted|rejected|regenerate|axis_promoted|proposal_rejected|undo|redo|...
    target_type TEXT,                     -- run|candidate|axis|proposal|...
    target_id   TEXT,
    run_id      TEXT,                     -- 关联 run（可空）
    detail      TEXT NOT NULL DEFAULT '{}'  -- 动作附加信息 JSON
);

CREATE INDEX IF NOT EXISTS idx_cand_status ON candidates(status);
CREATE INDEX IF NOT EXISTS idx_runs_subject ON runs(subject);
CREATE INDEX IF NOT EXISTS idx_evt_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_evt_run ON events(run_id);
CREATE INDEX IF NOT EXISTS idx_evt_actor ON events(actor_id);
"""


def _conn() -> sqlite3.Connection:
    """取连接（不抢锁）。仅限已持有 _LOCK 的内部路径调用。"""
    global _DB
    if _DB is None:
        path = get_config().root / "assets.db"
        # check_same_thread=False + 全部访问经 _LOCK（uvicorn 线程池调用路由）
        _DB = sqlite3.connect(str(path), check_same_thread=False)
        _DB.row_factory = sqlite3.Row
        _DB.executescript(SCHEMA)
        _migrate(_DB)
        _DB.commit()
    return _DB


def db() -> sqlite3.Connection:
    """外部入口：自取锁再拿连接（不可在持锁函数内调用，会死锁）。"""
    with _LOCK:
        return _conn()


def _migrate(conn: sqlite3.Connection):
    """存量库补列（CREATE TABLE IF NOT EXISTS 不动旧表结构）。
    迁移记录：v3.6 runs 加 parent_run（regenerate 溯源此前只在 manifest，未入库）。"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()]
    if "parent_run" not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN parent_run TEXT")


# ---------- 事件日志（append-only，只增不改） ----------
def log_event(action: str, actor: dict = None, target_type: str = None,
              target_id: str = None, run_id: str = None, detail: dict = None):
    """追加一条操作事实。actor: {id, name}（轻量身份）。
    缺省走环境变量 ARTCREATE_ACTOR_ID/ARTCREATE_ACTOR_NAME（CLI 自报身份用），
    再缺省 'cli'。动作枚举：run_created/accepted/rejected/regenerate/
    axis_promoted/proposal_rejected/retired/undo/redo/…（新增动作不需改表）。"""
    import os
    if not actor:
        actor = {"id": os.getenv("ARTCREATE_ACTOR_ID", "cli"),
                 "name": os.getenv("ARTCREATE_ACTOR_NAME", "")}
    with _LOCK:
        conn = _conn()
        conn.execute(
            "INSERT INTO events (actor_id, actor_name, action, target_type,"
            " target_id, run_id, detail) VALUES (?,?,?,?,?,?,?)",
            (actor.get("id", "cli"), actor.get("name", ""), action,
             target_type, str(target_id) if target_id is not None else None,
             run_id,
             json.dumps(detail or {}, ensure_ascii=False)))
        conn.commit()


def list_events(actor_id: str = None, action: str = None, run_id: str = None,
                subject: str = None, limit: int = 50, offset: int = 0):
    """时间线查询（按时间倒序）。subject 过滤走 run 关联 + events.detail.subject 兜底。"""
    sql = ("SELECT e.* FROM events e LEFT JOIN runs r ON e.run_id = r.run_id"
           " WHERE 1=1")
    args = []
    if actor_id:
        sql += " AND e.actor_id=?"
        args.append(actor_id)
    if action:
        sql += " AND e.action=?"
        args.append(action)
    if run_id:
        sql += " AND e.run_id=?"
        args.append(run_id)
    if subject:
        sql += " AND (r.subject=? OR e.detail LIKE ?)"
        args.extend([subject, f'%"subject": "{subject}"%'])
    sql += " ORDER BY e.id DESC LIMIT ? OFFSET ?"
    args.extend([limit, offset])
    with _LOCK:
        rows = _conn().execute(sql, args).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        try:
            item["detail"] = json.loads(item.get("detail") or "{}")
        except Exception:
            item["detail"] = {}
        out.append(item)
    return out


def run_lineage(run_id: str):
    """run 迭代树：沿 parent_run 向上追全部祖先 + 直系子代。
    返回 {ancestors: [...], children: [...]}，每项含 run 概要。"""
    with _LOCK:
        conn = _conn()

        def brief(row):
            return {"run_id": row["run_id"], "subject": row["subject"],
                    "revision": row["revision"], "created_at": row["created_at"],
                    "parent_run": row["parent_run"]}

        ancestors = []
        cur = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        seen = set()
        while cur and cur["parent_run"] and cur["parent_run"] not in seen:
            seen.add(cur["run_id"])
            nxt = conn.execute("SELECT * FROM runs WHERE run_id=?",
                               (cur["parent_run"],)).fetchone()
            if not nxt:
                break
            ancestors.append(brief(nxt))
            cur = nxt
        ancestors.reverse()

        children = [dict(r) for r in conn.execute(
            "SELECT run_id FROM runs WHERE parent_run=?", (run_id,)).fetchall()]
        child_briefs = []
        for c in children:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?",
                               (c["run_id"],)).fetchone()
            if row:
                child_briefs.append(brief(row))
    return {"ancestors": ancestors, "children": child_briefs}


def register_run(manifest: dict, actor: dict = None):
    """run 命令产出 manifest 后注册。返回 run_id。自动记 run_created 事件。"""
    with _LOCK:
        conn = _conn()
        conn.execute(
            "INSERT INTO runs (run_id, subject, revision, spec, prompt, segments,"
            " provider, model, size, count, ref_images, parent_run, lint_warnings)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (manifest["run_id"], manifest["subject"], manifest["spec_revision"],
             json.dumps(manifest["spec"], ensure_ascii=False),
             manifest["prompt"],
             json.dumps(manifest["prompt_segments"], ensure_ascii=False),
             manifest["provider"], manifest["model"], manifest["size"],
             manifest["count"],
             json.dumps(manifest.get("ref_images")) if manifest.get("ref_images") else None,
             manifest.get("parent_run"),
             json.dumps(manifest.get("lint_warnings", []), ensure_ascii=False)))
        for c in manifest["candidates"]:
            conn.execute(
                "INSERT INTO candidates (run_id, idx, file, w, h, grid_w, grid_h, status)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (manifest["run_id"], c["index"], c["file"], c["w"], c["h"],
                 c["grid_w"], c["grid_h"], "generated"))
        conn.commit()
    log_event("run_created", actor, target_type="run",
              target_id=manifest["run_id"], run_id=manifest["run_id"],
              detail={"subject": manifest["subject"],
                      "revision": manifest["spec_revision"],
                      "count": manifest["count"],
                      "size": manifest["size"],
                      "parent_run": manifest.get("parent_run"),
                      "description": (manifest["spec"].get("description") or "")[:80]})
    return manifest["run_id"]


def _set_status_locked(conn, run_id, idx, status, reject_code=None, gate_report=None):
    """无锁内部版：调用方必须已持有 _LOCK。"""
    conn.execute(
        "UPDATE candidates SET status=?, reject_code=?, gate_report=?,"
        " decided_at=datetime('now','localtime') WHERE run_id=? AND idx=?",
        (status, reject_code,
         json.dumps(gate_report, ensure_ascii=False) if gate_report else None,
         run_id, idx))
    conn.commit()


def set_status(run_id: str, idx: int, status: str, reject_code: str = None,
               gate_report: dict = None):
    with _LOCK:
        _set_status_locked(_conn(), run_id, idx, status, reject_code, gate_report)


def accept(run_id: str, idx: int, actor: dict = None):
    """接受候选：本候选 accepted；同 subject 同 revision 的其余 accepted 候选降级 rejected(auto_superseded)。
    拍板后若 spec 带 asset_name / pose，自动发布到 exports/final/（资产命名条目）。"""
    with _LOCK:
        conn = _conn()
        row = conn.execute(
            "SELECT r.subject, r.revision FROM runs r WHERE r.run_id=?",
            (run_id,)).fetchone()
        if not row:
            raise ValueError(f"run {run_id} 不存在")
        superseded = conn.execute(
            "SELECT run_id, idx FROM candidates WHERE status='accepted' AND run_id IN"
            " (SELECT run_id FROM runs WHERE subject=? AND revision=?)",
            (row["subject"], row["revision"])).fetchall()
        conn.execute(
            "UPDATE candidates SET status='rejected', reject_code='auto_superseded',"
            " decided_at=datetime('now','localtime')"
            " WHERE status='accepted' AND run_id IN"
            " (SELECT run_id FROM runs WHERE subject=? AND revision=?)",
            (            row["subject"], row["revision"]))
        _set_status_locked(conn, run_id, idx, "accepted")
    log_event("accepted", actor, target_type="candidate",
              target_id=f"{run_id}#{idx}", run_id=run_id,
              detail={"subject": row["subject"], "revision": row["revision"],
                      "superseded": [f"{r['run_id']}#{r['idx']}" for r in superseded]})
    # 命名发布（锁外调用，publish 自取锁）
    try:
        from .publish import publish_final
        publish_final(run_id, idx, actor=actor)
    except Exception as e:
        # 发布失败不阻断拍板（评审库数据已落定），事件里记失败原因
        log_event("final_publish_failed", actor, target_type="candidate",
                  target_id=f"{run_id}#{idx}", run_id=run_id,
                  detail={"error": str(e)})


def reject(run_id: str, idx: int, code: str = "manual", actor: dict = None):
    set_status(run_id, idx, "rejected", reject_code=code)
    with _LOCK:
        row = _conn().execute("SELECT r.subject FROM runs r WHERE r.run_id=?",
                           (run_id,)).fetchone()
    log_event("rejected", actor, target_type="candidate",
              target_id=f"{run_id}#{idx}", run_id=run_id,
              detail={"code": code, "subject": row["subject"] if row else None})


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
