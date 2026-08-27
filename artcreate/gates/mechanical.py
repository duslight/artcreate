"""artcreate · 机械门禁（阶段3-A）：调色板/网格/透明/尺寸 四项机检，零 LLM 全自动

输出：{"pass": bool, "checks": [{name, pass, detail}], "reject_code": str|None}
拒绝码：PALETTE_OVERFLOW / GRID_MISALIGNED / DIRTY_ALPHA / SIZE_MISMATCH
阈值从 project.yaml gates.mechanical 读取（可按画风覆盖）。
"""
import cv2
import numpy as np
from pathlib import Path

from ..tools.config import get_config


def _thresholds(art_style: str) -> dict:
    cfg = get_config()
    gates = cfg.gates
    base = dict(gates["mechanical"])
    override = gates.get("by_style", {}).get(art_style)
    if override:
        for k, v in override.items():
            base.setdefault(k, {}).update(v)
    return base


def check_palette(img_bgra: np.ndarray, th: dict):
    """调色板主色覆盖统计：top-N 色须覆盖 ≥ coverage_ratio 的像素。
    比 unique 计数更贴近像素画评估（渐变噪声色不主导判断）。"""
    level = 2 ** (8 - th.get("quantize_level", 6))
    q = (img_bgra[:, :, :3] // level) * level
    flat = q.reshape(-1, 3)
    # unique 计数（供报告）
    n_unique = len(np.unique(flat, axis=0))
    # 主色覆盖率
    colors, counts = np.unique(flat, axis=0, return_counts=True)
    order = np.argsort(-counts)
    max_colors = th.get("max_colors", 64)
    ratio = counts[order[:max_colors]].sum() / counts.sum()
    need = th.get("coverage_ratio", 0.95)
    ok = ratio >= need
    return ok, (f"top{max_colors} 主色覆盖 {ratio:.1%}（需 ≥{need:.0%}），"
                f"量化后独立色 {n_unique}")


def check_grid(img_bgra: np.ndarray, th: dict):
    """网格对齐：横向梯度峰值间隔的方差（像素画网格应等距）。
    复用 perfect_pixel 检测结果更准，但门禁独立实现避免依赖。"""
    gray = cv2.cvtColor(img_bgra, cv2.COLOR_BGR2GRAY).astype(np.float32)
    grad = np.abs(np.diff(gray, axis=1)).sum(axis=0)  # 每列边缘强度
    if grad.max() <= 0:
        return True, "无明显边缘（纯色图），跳过"
    # 主周期估计：自相关
    g = grad - grad.mean()
    ac = np.correlate(g, g, "full")[len(g) - 1:]
    ac /= (ac[0] + 1e-9)
    # 找第一个显著峰（跳过 0 lag）
    peaks = [(i, ac[i]) for i in range(4, min(len(ac) - 1, 200))
             if ac[i] > ac[i - 1] and ac[i] > ac[i + 1] and ac[i] > 0.3]
    if not peaks:
        return False, f"未检测到规则像素网格（无显著自相关峰）"
    period = peaks[0][0]
    # 峰强度即网格规则度：> (1 - max_deviation) 视为对齐
    strength = peaks[0][1]
    tol = 1 - th.get("max_deviation", 0.02)
    ok = strength >= tol
    return ok, f"网格周期 ~{period}px，规则度 {strength:.3f}（需 ≥{tol:.3f}）"


def check_alpha(img_bgra: np.ndarray, th: dict):
    """透明通道干净度：半透明像素（0<a<255）占全图比例。"""
    if not th.get("require_clean", True):
        return True, "未启用"
    a = img_bgra[:, :, 3]
    semi = ((a > 0) & (a < 255)).sum()
    ratio = semi / a.size
    limit = th.get("edge_semi_alpha_max", 0.05)
    return ratio <= limit, f"半透明像素占比 {ratio:.4f} / 上限 {limit}"


def check_size(img_bgra: np.ndarray, th: dict, expect_size: str):
    """尺寸合规：与 spec 声明一致。"""
    if not th.get("exact_match", True):
        return True, "未启用"
    h, w = img_bgra.shape[:2]
    ew, eh = map(int, expect_size.split("x"))
    return (w, h) == (ew, eh), f"实际 {w}x{h} / 声明 {expect_size}"


def gate_image(image_path, expect_size: str, art_style: str = "pixel_classic"):
    """单图四项检查。image_path: Path 或 str。"""
    th = _thresholds(art_style)
    from ..tools.postprocess import imread_unicode   # Windows 中文路径安全
    img = imread_unicode(image_path)
    if img is None:
        return {"pass": False,
                "checks": [{"name": "readable", "pass": False,
                            "detail": f"无法读取 {image_path}"}],
                "reject_code": "UNREADABLE"}
    if img.shape[2] == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)

    checks = []
    for name, fn, arg in (
            ("palette", check_palette, th["palette"]),
            ("grid", check_grid, th["grid"]),
            ("alpha", check_alpha, th["alpha"]),
            ("size", check_size, (th["size"], expect_size))):
        if name == "size":
            ok, detail = fn(img, arg[0], arg[1])
        else:
            ok, detail = fn(img, arg)
        checks.append({"name": name, "pass": bool(ok), "detail": detail})

    passed = all(c["pass"] for c in checks)
    reject = None
    if not passed:
        first_fail = next(c for c in checks if not c["pass"])
        reject = {"palette": "PALETTE_OVERFLOW", "grid": "GRID_MISALIGNED",
                  "alpha": "DIRTY_ALPHA", "size": "SIZE_MISMATCH",
                  "readable": "UNREADABLE"}[first_fail["name"]]
    return {"pass": passed, "checks": checks, "reject_code": reject}
