"""artcreate · 定稿发布（资产命名条目）

拍板（accept）时把定稿图复制到 exports/final/<asset_name>.png，
形成"评审库（带历史）"与"交付库（干净文件名）"双层：
- exports/<subject>/<run_id>/  评审库：全部候选 + run 溯源（已有）
- exports/final/               交付库：拍板产物的规范命名视图（本模块）

命名规则：asset_name（spec 顶层字段，表单"资产命名"条目写入）
+ 后缀（asset_suffixes 字典拉选，id 即后缀含义，suffix 字段为实际拼接串）
动作批量（pose run）不走表单命名：自动 = 角色名_动作（如 alice_attack）。

同名覆盖安全：final 里已有同名文件时，旧文件移入 final/.history/
（带时间戳后缀），不丢数据。final/ 整体可被引擎/下游直接拖走。
"""
import json
import shutil
from datetime import datetime
from pathlib import Path

from .store import _conn, _LOCK, log_event
from .tools.config import get_config


def resolve_final_name(spec: dict) -> str:
    """从 spec 解析最终文件主名（不含扩展名）。

    - pose run：角色名_动作（自动，优先级最高）
    - 显式 asset_name + 后缀 id（suffix_key）：asset_name + suffix 串
    - 都没有：返回 None（不发布，行为与旧版一致）
    """
    ch = spec.get("character") or {}
    if ch.get("pose"):
        subject = spec.get("subject", "unnamed")
        return f"{subject}_{ch['pose']}"
    name = spec.get("asset_name")
    if not name:
        return None
    name = name.strip()
    suffix_key = spec.get("asset_suffix_key")
    if suffix_key and suffix_key != "none":
        suf = (get_config().asset_suffixes or {}).get(suffix_key, {})
        name += suf.get("suffix", "")
    return name


def publish_final(run_id: str, idx: int, actor: dict = None) -> dict:
    """把拍板候选发布到 exports/final/。返回发布信息（未命名返回 None）。"""
    with _LOCK:
        row = _conn().execute(
            "SELECT r.subject, r.spec, c.file FROM runs r"
            " JOIN candidates c ON c.run_id=r.run_id"
            " WHERE r.run_id=? AND c.idx=?", (run_id, idx)).fetchone()
    if not row:
        raise ValueError(f"候选 {run_id}#{idx} 不存在")
    spec = json.loads(row["spec"])
    stem = resolve_final_name(spec)
    if not stem:
        return None

    cfg = get_config()
    src = cfg.root / "exports" / row["subject"] / run_id / row["file"]
    if not src.exists():
        raise FileNotFoundError(f"候选图缺失：{src}")

    final_dir = cfg.root / "exports" / "final"
    final_dir.mkdir(parents=True, exist_ok=True)
    dst = final_dir / f"{stem}{src.suffix}"

    # 同名归档（不丢数据）
    archived = None
    if dst.exists():
        hist = final_dir / ".history"
        hist.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        archived = dst.name.replace(dst.suffix, f".{ts}{dst.suffix}")
        shutil.move(str(dst), str(hist / archived))
    shutil.copyfile(src, dst)

    log_event("final_published", actor, target_type="asset",
              target_id=stem, run_id=run_id,
              detail={"file": str(dst.relative_to(cfg.root)), "idx": idx,
                      "archived": archived})
    return {"name": stem, "path": str(dst.relative_to(cfg.root)),
            "archived": archived}


def list_final() -> list:
    """交付库清单（引擎/下游可直接拖走的视图）。"""
    cfg = get_config()
    final_dir = cfg.root / "exports" / "final"
    if not final_dir.exists():
        return []
    out = []
    for p in sorted(final_dir.iterdir()):
        if p.is_file():
            st = p.stat()
            out.append({"name": p.stem, "file": p.name,
                        "size_kb": round(st.st_size / 1024),
                        "mtime": datetime.fromtimestamp(st.st_mtime).strftime(
                            "%Y-%m-%d %H:%M")})
    return out
