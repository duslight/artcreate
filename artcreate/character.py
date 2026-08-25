"""artcreate · 角色线（阶段7-A）：锚点一致性管线

参考 ai-game-spritesheets 8 步锚点法，裁剪为四步（贴合现有产线）：
  1. 锚点图：一张三视图/标准立绘（用户上传或首版拍板图晋升为锚点）
  2. 变体生成：execute_run 带 ref_images=[锚点]（Seedream 强锚，D19 验证）
  3. 一致性检查：dHash 感知哈希对比锚点（角色同一性的机械可测代理）
  4. 拍板：变体 accepted 后可晋升为新锚点（锚点链，parent_anchor 溯源）

角色 spec 约定（在场景 spec 基础上加两个字段）：
  character: {anchor: <锚点图路径或URL>, sheet: "三视图/立绘/动作"}
  asset_type 建议用 card_art（竖构图立绘）——前缀与构图匹配

一致性判定：dHash 8x8 = 64bit，汉明距离。与锚点同角色不同姿态的
合理距离区间实测约 [10, 30]：<10 几乎同图（变体多样性不足），
>30 结构差异过大（角色不一致风险）。报告距离，不硬拦（供评审参考，
与 L3 同哲学：提示用，非门禁）。
"""
import json
from pathlib import Path

import cv2
import numpy as np

from .store import db, _conn, _LOCK, log_event
from .tools.config import get_config


# ---------- dHash 感知哈希 ----------
def dhash(image_bgr: np.ndarray, size: int = 8) -> int:
    """64bit 差值哈希：缩放→灰度→水平相邻比较。"""
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    resized = cv2.resize(gray, (size + 1, size))
    diff = resized[:, 1:] > resized[:, :-1]
    bits = 0
    for row in diff:
        for v in row:
            bits = (bits << 1) | int(v)
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def consistency_report(candidate_path, anchor_path) -> dict:
    """候选 vs 锚点一致性。返回 {distance, verdict, hint}。"""
    c = cv2.imread(str(candidate_path))
    a = cv2.imread(str(anchor_path))
    if c is None or a is None:
        return {"distance": None, "verdict": "error",
                "hint": f"图读取失败（anchor={anchor_path}）"}
    d = hamming(dhash(c), dhash(a))
    if d < 10:
        verdict, hint = "too_similar", "与锚点几乎同图，变体多样性不足"
    elif d <= 30:
        verdict, hint = "consistent", "结构相近且姿态有变化——理想区间"
    else:
        verdict, hint = "drift_risk", "结构差异大，角色一致性存疑"
    return {"distance": d, "verdict": verdict, "hint": hint}


# ---------- 锚点注册表 ----------
ANCHORS_SCHEMA = """
CREATE TABLE IF NOT EXISTS anchors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    character   TEXT NOT NULL,             -- 角色名（=subject）
    image_path  TEXT NOT NULL,             -- 锚点图（相对仓库根）
    note        TEXT,                      -- 三视图/立绘/来源说明
    parent_anchor INTEGER,                 -- 晋升链：上一个锚点 id
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);
"""


def init_character():
    with _LOCK:
        _conn().executescript(ANCHORS_SCHEMA)
        _conn().commit()


def set_anchor(character: str, image_path: str, note: str = "",
               actor: dict = None, from_candidate: str = None):
    """设定/更新锚点。from_candidate: "run_id#idx"（从拍板候选晋升，记链）。"""
    init_character()
    parent = None
    if from_candidate:
        run_id, idx = from_candidate.split("#")
        with _LOCK:
            row = _conn().execute(
                "SELECT id FROM anchors WHERE character=? ORDER BY id DESC LIMIT 1",
                (character,)).fetchone()
        parent = row["id"] if row else None
    with _LOCK:
        cur = _conn().execute(
            "INSERT INTO anchors (character, image_path, note, parent_anchor)"
            " VALUES (?,?,?,?)", (character, image_path, note, parent))
        _conn().commit()
        aid = cur.lastrowid
    log_event("anchor_set", actor, target_type="character",
              target_id=character, detail={"anchor_id": aid,
              "image": image_path, "from_candidate": from_candidate,
              "parent_anchor": parent})
    return aid


def get_anchor(character: str):
    """最新锚点（角色当前生效的形象基准）。"""
    init_character()
    with _LOCK:
        row = _conn().execute(
            "SELECT * FROM anchors WHERE character=?"
            " ORDER BY id DESC LIMIT 1", (character,)).fetchone()
    return dict(row) if row else None


def anchor_lineage(character: str):
    """锚点晋升链（角色的形象演进史）。"""
    init_character()
    with _LOCK:
        rows = _conn().execute(
            "SELECT * FROM anchors WHERE character=? ORDER BY id", (character,)).fetchall()
    return [dict(r) for r in rows]


# ---------- 角色变体生成 ----------
def character_spec(character: str, description: str, sheet: str = "立绘",
                   anchor_path: str = None, **kw) -> dict:
    """构造角色变体 spec（走常规 execute_run，ref_images 挂锚点）。"""
    anchor = get_anchor(character) if anchor_path is None else \
        {"image_path": anchor_path}
    spec = {
        "subject": character,
        "description": description,
        "asset_type": "card_art",
        "constraints": {"axis_sel": {}, "free_text": ""},
        # 变体描述必含角色锚定语，配合参考图双保险
        # （参考图管轮廓/配色，描述管语义）
    }
    spec.update(kw)
    if anchor and anchor.get("image_path"):
        spec["ref_images"] = [anchor["image_path"]]
    if sheet:
        spec["extra_prompt"] = (
            {"三视图": "character turnaround sheet, front side back views, "
                       "full body, consistent proportions",
             "立绘": "full body standing pose, clean silhouette, centered",
             "动作": "dynamic action pose, expressive gesture"}[sheet]
            + (f"，{spec.get('extra_prompt') or ''}" if spec.get("extra_prompt") else ""))
    return spec


def check_run_consistency(subject: str, run_id: str) -> list:
    """对整个 run 的候选跑锚点一致性。锚点缺失返回空列表。"""
    anchor = get_anchor(subject)
    if not anchor:
        return []
    cfg = get_config()
    out = []
    with _LOCK:
        rows = _conn().execute(
            "SELECT idx, file FROM candidates WHERE run_id=? ORDER BY idx",
            (run_id,)).fetchall()
    for r in rows:
        img = cfg.root / "exports" / subject / run_id / r["file"]
        if not img.exists():
            continue
        rep = consistency_report(img, cfg.root / anchor["image_path"])
        rep["idx"] = r["idx"]
        out.append(rep)
    return out
