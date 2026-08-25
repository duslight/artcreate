"""artcreate · 经验蒸馏与晋升提案（D22 闭环最后一环）

流程：
1. harvest：扫全部 accepted 定稿 run 的 spec，收集自由文本约束
   （free_text / description 里的细节诉求）与高频 axis 选项组合
2. 聚类去重：词法归一（去空白/标点/大小写）→ 相同归一签名合并计数
3. LLM 判定（带缓存表）：这段约束是否可固化为轴选项？
   输出 {promotable, axis_id, option: {label, positive, negative, verify_question}, reason}
4. 置信门槛：高频（≥min_count）且 LLM 给出完整选项 → 生成提案入 proposals 表
5. 人工晋升（互信模型：任何用户可在工作台点）：写 derived/axes_evolved.yaml
   + 自动 git commit + events 留痕（axis_promoted 事件本身可撤销）

可逆动作（D22 定案）：
- promote 提案 → derived + 事件；undo → 从 derived 移除 + 恢复提案 pending + 事件
- reject 提案（驳回不当提案）→ events；undo_reject → 恢复 pending
- 撤销操作本身也写 events（历史永不改写）

七坑对策落点：粒度对齐→LLM 判定给 axis_id；同义词→归一签名+aliases 留字段；
否定反转→LLM prompt 明示拒绝 no X 式；一词多义→项目隔离（derived 按项目）；
冷启动→min_count 门槛；关系约束→non_promotable 轴直接跳过；缓存→llm_judgments 表。
"""
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

from .store import db, _conn, _LOCK, log_event, list_events
from .tools.config import get_config

_LOCK_D = _LOCK  # 与 store 共用一把锁（同一 SQLite）

PROPOSALS_SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|promoted|rejected|expired
    kind        TEXT NOT NULL DEFAULT 'free_text', -- free_text|axis_combo
    signature   TEXT NOT NULL,                    -- 归一签名（去重键）
    sample_text TEXT NOT NULL,                    -- 原始样本（代表例）
    count       INTEGER NOT NULL DEFAULT 1,       -- 出现次数
    axis_id     TEXT,                             -- 建议挂载轴（LLM 判定）
    option_draft TEXT,                            -- LLM 生成的完整选项 JSON
    confidence  REAL,                             -- LLM 置信度 0-1
    reason      TEXT,
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    decided_at  TEXT,
    decided_by  TEXT,
    UNIQUE(signature, axis_id)
);

CREATE TABLE IF NOT EXISTS llm_judgments (
    signature   TEXT PRIMARY KEY,
    verdict     TEXT NOT NULL,        -- LLM 原始判定 JSON（缓存）
    judged_at   TEXT DEFAULT (datetime('now','localtime'))
);
"""

DERIVED_FILE = "axes_evolved.yaml"
MIN_COUNT = 3          # 冷启动门槛：自由文本出现 ≥3 次才提案
LLM_CACHE = True


def init_distill():
    with _LOCK_D:
        _conn().executescript(PROPOSALS_SCHEMA)
        _conn().commit()


# ---------- 归一签名 ----------
def normalize_sig(text: str) -> str:
    """词法归一：小写、去标点空白。同义词合并交给 LLM 判定阶段。"""
    return re.sub(r"[\s，。、！？,.\!\?；;：:·]+", "", text).lower()


# ---------- harvest ----------
def harvest() -> dict:
    """扫 accepted 定稿 run 的 spec，收集自由文本约束。
    只取 accepted（拍板）——用户最终认可的编译词才是经验（D22 核心原则）。"""
    with _LOCK_D:
        rows = _conn().execute(
            "SELECT r.spec FROM runs r JOIN candidates c ON c.run_id=r.run_id"
            " WHERE c.status='accepted'").fetchall()
    samples = {}
    for r in rows:
        try:
            spec = json.loads(r["spec"])
        except Exception:
            continue
        free = str((spec.get("constraints") or {}).get("free_text") or "").strip()
        if not free or len(free) < 2:
            continue
        sig = normalize_sig(free)
        if not sig:
            continue
        if sig in samples:
            samples[sig]["count"] += 1
        else:
            samples[sig] = {"text": free, "count": 1}
    return samples


# ---------- LLM 判定（带缓存） ----------
JUDGE_PROMPT = """你是游戏美术管线的约束字典审计员。判断下面这条用户反复使用的自由文本约束，
是否值得固化为约束轴上的一个新选项（晋升后它会出现在所有用户的表单里）。

用户约束原文（多次出现，count={count}）：
"{text}"

现有约束轴（只能挂到其中之一，或判为不适合）：
{axes_desc}

判定规则：
- 只收"可独立执行的正向描述"：能翻译成 positive/negative 英文词组 + 一句可验图问句
- 拒绝否定式（"不要X/没有X"）——生成模型不懂否定，这是 L1 lint 拦截项
- 拒绝与现有选项语义重复的（归并比新增好）
- 拒绝关系型约束（"A 在 B 旁边"类，无法独立成轴）
- 粒度对齐：教堂比建筑细、比窗户粗——挂到最贴近的轴
严格只输出 JSON：
{{"promotable": true|false, "axis_id": "...", "confidence": 0.0-1.0,
  "option": {{"id": "...", "label": "...", "positive": ["..."], "negative": ["..."],
             "verify_question": "画面中是否…？"}},
  "reason": "一句话理由"}}
promotable=false 时 axis_id/option 可为空。"""


def _judge_cached(sig: str, text: str, count: int, axes_desc: str) -> dict:
    with _LOCK_D:
        row = _conn().execute(
            "SELECT verdict FROM llm_judgments WHERE signature=?",
            (sig,)).fetchone()
    if row and LLM_CACHE:
        try:
            return json.loads(row["verdict"])
        except Exception:
            pass
    from .tools.llm_client import text_chat
    raw = text_chat(
        JUDGE_PROMPT.replace("{count}", str(count))
        .replace("{text}", text).replace("{axes_desc}", axes_desc),
        temperature=0.1)
    m = re.search(r"\{.*\}", raw, re.S)
    verdict = json.loads(m.group(0)) if m else {"promotable": False,
                                                "reason": "LLM 输出不可解析"}
    with _LOCK_D:
        _conn().execute(
            "INSERT OR REPLACE INTO llm_judgments (signature, verdict) VALUES (?,?)",
            (sig, json.dumps(verdict, ensure_ascii=False)))
        _conn().commit()
    return verdict


def _axes_desc() -> str:
    cfg = get_config()
    lines = []
    for ax in cfg.constraint_axes:
        if ax.get("non_promotable"):
            continue
        opts = "、".join(o.get("label", o["id"]) for o in ax.get("options", []))
        lines.append(f"- {ax['id']}（{ax.get('label','')}）：现有选项 {opts}")
    return "\n".join(lines)


# ---------- 主流程 ----------
def run_distill(min_count: int = MIN_COUNT, use_llm: bool = True,
                actor: dict = None) -> dict:
    """蒸馏一轮：harvest → 高频项 LLM 判定 → 提案入表。
    返回 {harvested: N, judged: N, new_proposals: N, skipped: {...}}。"""
    init_distill()
    cfg = get_config()
    samples = harvest()
    stats = {"harvested": len(samples), "judged": 0,
             "new_proposals": 0, "skipped": {}}
    axes_desc = _axes_desc()
    existing_opts = {(ax["id"], o.get("id")) for ax in cfg.constraint_axes
                     for o in ax.get("options", [])}

    for sig, s in samples.items():
        if s["count"] < min_count:
            stats["skipped"][s["text"]] = f"频次 {s['count']} < {min_count}"
            continue
        # 已有同签名提案则只累计次数
        with _LOCK_D:
            old = _conn().execute(
                "SELECT id FROM proposals WHERE signature=? AND status='pending'",
                (sig,)).fetchone()
            if old:
                _conn().execute("UPDATE proposals SET count=? WHERE id=?",
                                (s["count"], old["id"]))
                _conn().commit()
                continue

        if use_llm:
            verdict = _judge_cached(sig, s["text"], s["count"], axes_desc)
            stats["judged"] += 1
        else:
            verdict = {"promotable": False, "reason": "LLM 关闭（调试模式）"}

        if not verdict.get("promotable"):
            stats["skipped"][s["text"]] = verdict.get("reason", "LLM 判不可晋升")
            continue
        axis_id = verdict.get("axis_id")
        opt = verdict.get("option") or {}
        if not axis_id or not opt.get("id"):
            stats["skipped"][s["text"]] = "判定缺 axis_id/option"
            continue
        if (axis_id, opt["id"]) in existing_opts:
            stats["skipped"][s["text"]] = f"选项 {axis_id}/{opt['id']} 已存在"
            continue

        with _LOCK_D:
            _conn().execute(
                "INSERT OR IGNORE INTO proposals (kind, signature, sample_text,"
                " count, axis_id, option_draft, confidence, reason)"
                " VALUES ('free_text',?,?,?,?,?,?,?)",
                (sig, s["text"], s["count"], axis_id,
                 json.dumps(opt, ensure_ascii=False),
                 verdict.get("confidence", 0.5), verdict.get("reason", "")))
            _conn().commit()
        stats["new_proposals"] += 1

    if actor:
        log_event("distill_run", actor, target_type="proposals",
                  detail=stats)
    return stats


# ---------- 晋升 / 驳回 / 撤销（可逆动作） ----------
def _derived_path() -> Path:
    cfg = get_config()
    d = cfg.root / "derived"
    d.mkdir(exist_ok=True)
    return d / DERIVED_FILE


def list_proposals(status: str = None):
    with _LOCK_D:
        if status:
            rows = _conn().execute(
                "SELECT * FROM proposals WHERE status=? ORDER BY count DESC, id DESC",
                (status,)).fetchall()
        else:
            rows = _conn().execute(
                "SELECT * FROM proposals ORDER BY status, count DESC, id DESC").fetchall()
    out = []
    for r in rows:
        item = dict(r)
        try:
            item["option_draft"] = json.loads(item.get("option_draft") or "null")
        except Exception:
            pass
        out.append(item)
    return out


def get_proposal(pid: int):
    with _LOCK_D:
        row = _conn().execute("SELECT * FROM proposals WHERE id=?",
                              (pid,)).fetchone()
    return dict(row) if row else None


def approve(pid: int, actor: dict = None):
    """晋升提案：写 derived/axes_evolved.yaml + git commit + 事件。"""
    p = get_proposal(pid)
    if not p or p["status"] != "pending":
        raise ValueError(f"提案 {pid} 不存在或非 pending")
    cfg = get_config()
    opt = json.loads(p["option_draft"])
    opt["source"] = "distilled"
    opt["confidence"] = p["confidence"] or 0.5

    # 生成/合并 derived yaml（手写 yaml 片段，保持注释能力留给人工后续维护）
    import yaml
    path = _derived_path()
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    axes = data.setdefault("constraint_axes", [])
    ax = next((a for a in axes if a.get("id") == p["axis_id"]), None)
    if ax is None:
        ax = {"id": p["axis_id"], "label": p["axis_id"], "options": []}
        axes.append(ax)
    ax.setdefault("options", []).append(opt)
    path.write_text(yaml.dump(data, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")

    with _LOCK_D:
        _conn().execute(
            "UPDATE proposals SET status='promoted', decided_at=?, decided_by=?"
            " WHERE id=?",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             (actor or {}).get("name") or (actor or {}).get("id", ""), pid))
        _conn().commit()

    # 自动 git commit（经验回写进版本史；无 git 环境不阻断）
    commit = _git_commit(path, f"distill: promote proposal #{pid} -> {p['axis_id']}/{opt['id']}")
    log_event("axis_promoted", actor, target_type="proposal",
              target_id=str(pid),
              detail={"axis": p["axis_id"], "option": opt["id"],
                      "label": opt.get("label"), "count": p["count"],
                      "git_commit": commit})
    return {"derived": str(path), "git_commit": commit, "option": opt}


def reject_proposal(pid: int, actor: dict = None, reason: str = ""):
    """驳回提案（不当晋升建议）。可撤销。"""
    p = get_proposal(pid)
    if not p or p["status"] != "pending":
        raise ValueError(f"提案 {pid} 不存在或非 pending")
    with _LOCK_D:
        _conn().execute(
            "UPDATE proposals SET status='rejected', decided_at=?, decided_by=?,"
            " reason=reason||? WHERE id=?",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             (actor or {}).get("name") or (actor or {}).get("id", ""),
             f" | 驳回：{reason}" if reason else "", pid))
        _conn().commit()
    log_event("proposal_rejected", actor, target_type="proposal",
              target_id=str(pid),
              detail={"axis": p["axis_id"], "reason": reason})
    return {"ok": True}


def undo(pid: int, actor: dict = None):
    """撤销晋升：从 derived 移除该选项 + git commit + 提案回 pending。"""
    p = get_proposal(pid)
    if not p or p["status"] != "promoted":
        raise ValueError(f"提案 {pid} 不存在或非 promoted")
    opt = json.loads(p["option_draft"])
    import yaml
    path = _derived_path()
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for ax in data.get("constraint_axes", []):
            if ax.get("id") == p["axis_id"]:
                ax["options"] = [o for o in ax.get("options", [])
                                 if o.get("id") != opt["id"]]
        path.write_text(yaml.dump(data, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
    with _LOCK_D:
        _conn().execute(
            "UPDATE proposals SET status='pending', decided_at=NULL,"
            " decided_by=NULL WHERE id=?", (pid,))
        _conn().commit()
    commit = _git_commit(path, f"distill: undo promotion of proposal #{pid} ({opt['id']})")
    log_event("undo", actor, target_type="proposal", target_id=str(pid),
              detail={"action_undone": "axis_promoted", "axis": p["axis_id"],
                      "option": opt["id"], "git_commit": commit})
    return {"ok": True, "git_commit": commit}


def undo_reject(pid: int, actor: dict = None):
    """恢复被驳回的提案。"""
    p = get_proposal(pid)
    if not p or p["status"] != "rejected":
        raise ValueError(f"提案 {pid} 不存在或非 rejected")
    with _LOCK_D:
        _conn().execute(
            "UPDATE proposals SET status='pending', decided_at=NULL,"
            " decided_by=NULL WHERE id=?", (pid,))
        _conn().commit()
    log_event("undo", actor, target_type="proposal", target_id=str(pid),
              detail={"action_undone": "proposal_rejected"})
    return {"ok": True}


def _git_commit(path: Path, message: str):
    """derived 变更自动提交；无 git / 无变更静默跳过。"""
    try:
        root = get_config().root
        subprocess.run(["git", "add", str(path.relative_to(root))],
                       cwd=root, capture_output=True, timeout=15, check=True)
        r = subprocess.run(["git", "commit", "-m", message],
                           cwd=root, capture_output=True, timeout=15, text=True)
        if r.returncode != 0:
            return None
        h = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                           cwd=root, capture_output=True, timeout=15, text=True)
        return h.stdout.strip()
    except Exception:
        return None
