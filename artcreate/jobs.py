"""artcreate · 异步任务队列（服务端工作台核心）

jobs 表 + 单 worker 线程：web 表单提交 → jobs 排队 → worker 串行执行
pipeline.execute_run → 前端轮询 job 状态 → 完成后跳评审视图。

串行设计（非并发池）：生成 API 有速率限制，串行即天然限流；
多任务排队先来先服务，job 带 progress 阶段文本供前端展示。

附带：候选原图保留期清理（rejected/auto_superseded 候选的 raw/out PNG
超过保留期删除，只留缩略图——「定稿全保+候选缩略」策略的执行侧）。
"""
import json
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from .store import db, _conn, _LOCK
from .tools.config import get_config

_LOCK_JOBS = threading.Lock()   # jobs 表自身操作（与 store._LOCK 分开）

JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    status      TEXT NOT NULL DEFAULT 'queued',   -- queued|running|done|error
    spec        TEXT NOT NULL,                    -- 提交的 spec JSON
    actor_id    TEXT,
    actor_name  TEXT,
    run_id      TEXT,                             -- 完成后回填
    progress    TEXT,                             -- 阶段文本
    error       TEXT,
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    finished_at TEXT
);
"""

_worker_started = False
_last_distill = 0.0
_DISTILL_INTERVAL = 3600  # 空闲时每小时蒸馏一轮（D22 经验回写）


def init_jobs():
    """建表 + 启动 worker（进程内单例）。"""
    global _worker_started
    with _LOCK:
        _conn().executescript(JOBS_SCHEMA)
        _conn().commit()
    if not _worker_started:
        t = threading.Thread(target=_worker_loop, daemon=True,
                             name="artcreate-worker")
        t.start()
        _worker_started = True


def submit_job(spec: dict, actor: dict = None) -> int:
    """入队一个生成任务，返回 job id。spec 校验由调用方（API 层）先行。"""
    with _LOCK:
        conn = _conn()
        cur = conn.execute(
            "INSERT INTO jobs (spec, actor_id, actor_name) VALUES (?,?,?)",
            (json.dumps(spec, ensure_ascii=False),
             (actor or {}).get("id", "web-anonymous"),
             (actor or {}).get("name", "")))
        conn.commit()
        return cur.lastrowid


def get_job(job_id: int):
    with _LOCK:
        row = _conn().execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return None
    item = dict(row)
    try:
        item["spec"] = json.loads(item["spec"])
    except Exception:
        pass
    return item


def list_jobs(limit: int = 20):
    with _LOCK:
        rows = _conn().execute(
            "SELECT id, status, actor_id, actor_name, run_id, progress, error,"
            " created_at, finished_at FROM jobs"
            " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def _set_job(job_id, **fields):
    with _LOCK:
        conn = _conn()
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE jobs SET {sets} WHERE id=?",
                     (*fields.values(), job_id))
        conn.commit()


def _worker_loop():
    """串行取队执行。daemon 线程，进程退出即停。"""
    while True:
        job = None
        with _LOCK:
            row = _conn().execute(
                "SELECT * FROM jobs WHERE status='queued'"
                " ORDER BY id LIMIT 1").fetchone()
            if row:
                _conn().execute(
                    "UPDATE jobs SET status='running',"
                    " progress='排队完成，开始执行' WHERE id=?", (row["id"],))
                _conn().commit()
                job = dict(row)
        if not job:
            # 空闲时周期蒸馏（D22：拍板经验 → 晋升提案）
            global _last_distill
            now = time.time()
            if now - _last_distill > _DISTILL_INTERVAL:
                _last_distill = now
                try:
                    from .distill import run_distill
                    run_distill()
                except Exception:
                    pass  # 蒸馏失败不影响 worker 主循环
            time.sleep(2)
            continue

        job_id = job["id"]
        actor = {"id": job.get("actor_id") or "web-anonymous",
                 "name": job.get("actor_name") or ""}
        try:
            spec = json.loads(job["spec"])
            if spec.get("pose_batch"):
                # 7-B 动作批量：spec 是壳，真实参数在 pose_batch 段
                from .pipeline import execute_pose_batch
                pb = spec["pose_batch"]
                provider_override = pb.pop("provider_override", None)
                def _prog(pose, i, total):
                    _set_job(job_id, progress=f"动作 {i}/{total}：{pose}")
                result = execute_pose_batch(
                    pb["character"], pb["poses"],
                    description=pb.get("description", ""),
                    count_each=pb.get("count_each", 4),
                    style_refs=pb.get("style_refs"),
                    provider_override=provider_override,
                    actor=actor, on_progress=_prog)
                errs = [r for r in result["runs"] if r.get("error")]
                ok_runs = [r["run_id"] for r in result["runs"] if r.get("run_id")]
                _set_job(job_id, status="done" if not errs else "error",
                         run_id=ok_runs[0] if ok_runs else None,
                         progress=f"完成（{len(ok_runs)}/{len(result['runs'])} 动作）",
                         error="；".join(f"{r['pose']}: {r['error']}"
                                         for r in errs) or None,
                         finished_at=datetime.now().strftime(
                             "%Y-%m-%d %H:%M:%S"))
                continue

            from .pipeline import execute_run
            _set_job(job_id, progress="编译 + L1 lint")

            # ref_images 相对路径 → 仓库根绝对路径（不依赖进程 cwd；
            # 风格锚/角色锚存的都是 exports/... 相对路径）
            cfg = get_config()
            refs = spec.get("ref_images")
            if isinstance(refs, list) and refs:
                refs = [str(cfg.root / r) if not Path(r).is_absolute() else r
                        for r in refs]

            # 自定义生图 API：执行前剥离（密钥不落库），单独传 provider
            provider_override = spec.pop("provider_override", None)

            # 执行体内部 print 输出对 web 无意义，但保留（本地 serve 可见日志）
            manifest = execute_run(spec, refs, actor=actor,
                                   provider_override=provider_override)
            if manifest is None:
                _set_job(job_id, status="error",
                         error="L1 lint 拦截（block 级问题，未产生费用）",
                         finished_at=datetime.now().strftime(
                             "%Y-%m-%d %H:%M:%S"))
                continue
            _set_job(job_id, status="done", run_id=manifest["run_id"],
                     progress="完成",
                     finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        except Exception as e:
            _set_job(job_id, status="error",
                     error=f"{e}\n{traceback.format_exc()[-500:]}",
                     finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


# ---------- 候选原图保留期清理 ----------
def cleanup_candidates(retain_days: int = 30, dry_run: bool = False):
    """rejected/auto_superseded 候选的 raw_/out_ PNG 超过保留期删除，
    只留 thumb jpg。accepted 定稿永不清理。「定稿全保+候选缩略」执行侧。
    返回 {deleted: [...], kept_accepted: N}。"""
    cfg = get_config()
    cutoff = datetime.now() - timedelta(days=retain_days)
    with _LOCK:
        rows = _conn().execute(
            "SELECT c.run_id, c.idx, c.file, r.subject, r.created_at"
            " FROM candidates c JOIN runs r ON c.run_id=r.run_id"
            " WHERE c.status IN ('rejected')").fetchall()
    deleted, kept = [], 0
    for r in rows:
        try:
            created = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if created >= cutoff:
            continue
        run_dir = cfg.root / "exports" / r["subject"] / r["run_id"]
        idx = r["idx"]
        for pattern in (f"raw_{idx}.png", f"out_{idx}.png"):
            p = run_dir / pattern
            thumb = run_dir / pattern.replace(".png", ".jpg")
            if p.exists() and thumb.exists() and not dry_run:
                p.unlink()
                deleted.append(str(p.relative_to(cfg.root)))
    return {"deleted": deleted, "kept_accepted": kept}
