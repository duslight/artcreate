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
# kind：character=角色形象锚 / style=风格锚（2026-08-26 风格固化方案A）。
# scope：风格锚按资产页分库（scene|character|anim）——三页各自独立，互不可见
#        （2026-08-26 用户确认：场景和角色的锚点不该通用）。旧 style 行迁移默认 scene。
ANCHORS_SCHEMA = """
CREATE TABLE IF NOT EXISTS anchors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    character   TEXT NOT NULL,             -- 拥有者名（角色名/风格锚名）
    image_path  TEXT NOT NULL,             -- 锚点图（相对仓库根）
    note        TEXT,                      -- 三视图/立绘/来源说明
    parent_anchor INTEGER,                 -- 晋升链：上一个锚点 id
    kind        TEXT DEFAULT 'character',  -- character | style
    scope       TEXT DEFAULT 'scene',      -- 风格锚所属资产页（scene|character|anim）
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);
"""


# scope：风格/参考锚按用途分库。
#   旧三库（2026-08-26）：scene 场景页 / character 角色页 / anim 动作页
#   角色页三槽位（2026-08-27）：character_ref 人物 / style_ref 画风 / pose_ref 姿势
#     —— 三槽各挂 1 张，提交时按 人物→画风→姿势 顺序拼接 + 分工说明句
#   怪物页三槽位（2026-08-27）：monster_ref 本体 / monster_style_ref 画风 / monster_pose_ref 姿态
#     —— 独立于角色库，互不污染；提交按 本体→画风→姿态 顺序拼接
STYLE_SCOPES = ("scene", "character", "anim",
                "character_ref", "style_ref", "pose_ref",
                "monster_ref", "monster_style_ref", "monster_pose_ref")


def init_character():
    with _LOCK:
        _conn().executescript(ANCHORS_SCHEMA)
        # 旧库迁移：无 kind/scope 列则补（默认值保旧行语义不变）
        cols = {r["name"] for r in
                _conn().execute("PRAGMA table_info(anchors)").fetchall()}
        if "kind" not in cols:
            _conn().execute(
                "ALTER TABLE anchors ADD COLUMN kind TEXT DEFAULT 'character'")
        if "scope" not in cols:
            _conn().execute(
                "ALTER TABLE anchors ADD COLUMN scope TEXT DEFAULT 'scene'")
        _conn().commit()


def set_anchor(character: str, image_path: str, note: str = "",
               actor: dict = None, from_candidate: str = None,
               kind: str = "character", scope: str = "scene"):
    """设定/更新锚点。from_candidate: "run_id#idx"（从拍板候选晋升，记链）。
    kind: character（角色形象锚）| style（风格锚，画风固化参考）。
    scope: 风格锚归属资产页（scene|character|anim），仅 kind=style 时有意义。"""
    init_character()
    if kind == "style" and scope not in STYLE_SCOPES:
        raise ValueError(f"scope 必须是 {STYLE_SCOPES} 之一：{scope}")
    parent = None
    if from_candidate:
        run_id, idx = from_candidate.split("#")
        with _LOCK:
            row = _conn().execute(
                "SELECT id FROM anchors WHERE character=? AND kind=?"
                " AND (? IS NULL OR scope=?)"
                " ORDER BY id DESC LIMIT 1",
                (character, kind, scope if kind == "style" else None,
                 scope if kind == "style" else None)).fetchone()
        parent = row["id"] if row else None
    with _LOCK:
        cur = _conn().execute(
            "INSERT INTO anchors (character, image_path, note, parent_anchor,"
            " kind, scope) VALUES (?,?,?,?,?,?)",
            (character, image_path, note, parent, kind,
             scope if kind == "style" else "scene"))
        _conn().commit()
        aid = cur.lastrowid
    log_event("anchor_set", actor, target_type=kind + "_anchor",
              target_id=character, detail={"anchor_id": aid,
              "image": image_path, "from_candidate": from_candidate,
              "parent_anchor": parent, "kind": kind, "scope": scope})
    return aid


def get_anchor(character: str, kind: str = "character"):
    """最新锚点（角色当前生效的形象基准 / 风格锚最新一张）。"""
    init_character()
    with _LOCK:
        row = _conn().execute(
            "SELECT * FROM anchors WHERE character=? AND kind=?"
            " ORDER BY id DESC LIMIT 1", (character, kind)).fetchone()
    return dict(row) if row else None


def anchor_lineage(character: str, kind: str = "character"):
    """锚点晋升链（角色的形象演进史 / 风格锚演进史）。"""
    init_character()
    with _LOCK:
        rows = _conn().execute(
            "SELECT * FROM anchors WHERE character=? AND kind=? ORDER BY id",
            (character, kind)).fetchall()
    return [dict(r) for r in rows]


def list_style_anchors(scope: str = "scene"):
    """风格锚库（表单选择器数据源）：某资产页的锚，每风格名最新一张 + 张数。
    scope: scene|character|anim（三页独立库）。"""
    init_character()
    if scope not in STYLE_SCOPES:
        raise ValueError(f"scope 必须是 {STYLE_SCOPES} 之一：{scope}")
    with _LOCK:
        rows = _conn().execute(
            "SELECT character AS name, COUNT(*) AS total,"
            " MAX(id) AS latest_id FROM anchors WHERE kind='style'"
            " AND scope=?"
            " GROUP BY character ORDER BY latest_id DESC",
            (scope,)).fetchall()
    out = []
    for r in rows:
        with _LOCK:
            latest = _conn().execute(
                "SELECT * FROM anchors WHERE id=?", (r["latest_id"],)).fetchone()
        item = dict(latest)
        item["total"] = r["total"]
        out.append(item)
    return out


def count_style_anchor_names(scope: str = "scene") -> int:
    """风格锚名数（某页库容量，上限守卫用）。"""
    init_character()
    with _LOCK:
        row = _conn().execute(
            "SELECT COUNT(DISTINCT character) FROM anchors"
            " WHERE kind='style' AND scope=?", (scope,)).fetchone()
    return row[0] or 0


def delete_style_anchor(name: str, anchor_id: int = None, actor: dict = None,
                        scope: str = "scene"):
    """删除风格锚。anchor_id 空=删该名全部（常用）；给定=只删那一条。
    scope 限定资产页（防止同名跨页误删）。返回删除条数。"""
    init_character()
    with _LOCK:
        if anchor_id:
            cur = _conn().execute(
                "DELETE FROM anchors WHERE id=? AND kind='style' AND scope=?",
                (anchor_id, scope))
        else:
            cur = _conn().execute(
                "DELETE FROM anchors WHERE character=? AND kind='style'"
                " AND scope=?", (name, scope))
        _conn().commit()
    log_event("style_anchor_deleted", actor, target_type="style_anchor",
              target_id=name, detail={"anchor_id": anchor_id,
                                      "deleted": cur.rowcount, "scope": scope})
    return cur.rowcount


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


def pose_batch_spec(character: str, poses: list, description: str = "",
                    count_each: int = 4, style_refs: list = None, **kw) -> list:
    """7-B 动作批量：构造一组 pose spec（每 pose 一个 run）。

    poses 为 character_poses 字典里的合法 id 列表（调用方负责校验或
    依赖 spec_validate 的显式 error）。动作词走字典 inject，锚点自动挂
    最新锚点。style_refs：风格锚参考图路径列表（画风固化，与角色锚
    合并——角色锚在前保形象权重）。返回 [{pose, spec}, ...]。
    """
    cfg = get_config()
    pose_dict = cfg.character_poses
    anchor = get_anchor(character)
    if not anchor:
        raise ValueError(f"角色 {character} 尚无锚点，先 set_anchor")
    refs = [anchor["image_path"]] + [p for p in (style_refs or [])
                                     if p != anchor["image_path"]]
    out = []
    for pose in poses:
        if pose not in pose_dict:
            raise ValueError(f"未知动作 '{pose}'（现有：{sorted(pose_dict)}）")
        spec = {
            "subject": character,
            "description": description or f"{character} 动作变体",
            "asset_type": "card_art",
            "count": count_each,
            "constraints": {"axis_sel": {}, "free_text": ""},
            "ref_images": refs,
            "character": {"pose": pose, "sheet": "动作"},
            # 动作词与 sheet 语料拼接（动作词在前，姿态语料兜底）
            "extra_prompt": pose_dict[pose]["inject"]
                           + ", dynamic action pose, expressive gesture",
        }
        spec.update(kw)
        out.append({"pose": pose, "spec": spec})
    return out


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


def attach_consistency(subject: str, run_id: str) -> list:
    """7-B：run 落库后自动跑一致性并写进 gate_report（评审页 Δ 徽标直接有数据）。

    动作变体的 hint 文案与立绘不同：动作幅度大自然距离偏大，重点排查
    极端值而非区间判定。返回报告列表（无锚点/无候选返回空）。
    """
    reports = check_run_consistency(subject, run_id)
    if not reports:
        return []
    for rep in reports:
        d = rep.get("distance")
        if d is None:
            continue
        if d > 45:
            rep["hint"] = f"距离 {d}：动作变体极端偏差，建议人审"
        else:
            rep["hint"] = f"距离 {d}：动作变体正常范围（动作幅度大自然偏大）"
        with _LOCK:
            row = _conn().execute(
                "SELECT gate_report FROM candidates WHERE run_id=? AND idx=?",
                (run_id, rep["idx"])).fetchone()
        try:
            existing = json.loads(row["gate_report"]) if row and row["gate_report"] else {}
            if not isinstance(existing, dict):
                existing = {}
        except Exception:
            existing = {}
        existing["consistency"] = rep
        with _LOCK:
            _conn().execute(
                "UPDATE candidates SET gate_report=? WHERE run_id=? AND idx=?",
                (json.dumps(existing, ensure_ascii=False), run_id, rep["idx"]))
            _conn().commit()
    return reports
