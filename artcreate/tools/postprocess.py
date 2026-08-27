"""artcreate · 后处理：下载 RAW → 像素网格化 → 调色板量化 → 落盘 exports 目录结构

目录结构（资产契约 D-工厂三件套）：
  exports/{subject}/{run_id}/raw_N.png / out_N.png / thumb_N.jpg
量化（palette quantize）：网格小图上 kmeans 聚到 max_colors 主色，
NEAREST 放大不引入新色——真实像素画的调色板特征（门禁收紧的前置）。
"""
import urllib.request
from pathlib import Path

import cv2
import numpy as np
from perfect_pixel import get_perfect_pixel

from .config import get_config


def imread_unicode(path) -> np.ndarray:
    """Windows 中文路径安全读图（cv2.imread 对非 ASCII 路径静默返回 None）。"""
    data = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_UNCHANGED)


def imwrite_unicode(path, img, params=None) -> bool:
    """Windows 中文路径安全写图。"""
    ext = Path(str(path)).suffix or ".png"
    ok, buf = cv2.imencode(ext, img, params or [])
    if not ok:
        return False
    buf.tofile(str(path))
    return True


def download(url: str, dest: Path):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        f.write(r.read())


def quantize_palette(small_bgra: np.ndarray, max_colors: int) -> np.ndarray:
    """网格小图调色板量化：kmeans 聚到 ≤max_colors 主色后逐像素回贴。
    在小图（网格分辨率）上做——像素数少、色簇即最终可见色。"""
    h, w = small_bgra.shape[:2]
    px = small_bgra[:, :, :3].reshape(-1, 3).astype(np.float32)
    k = min(max_colors, len(np.unique(px, axis=0)))
    if k < 2:
        return small_bgra
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 12, 0.5)
    _, labels, centers = cv2.kmeans(px, k, None, criteria, 3,
                                     cv2.KMEANS_PP_CENTERS)
    q = centers[labels.flatten()].reshape(h, w, 3).astype(np.uint8)
    out = small_bgra.copy()
    out[:, :, :3] = q
    return out


def pixelate(src_path: Path, dst_path: Path, thumb_size: int = 512):
    """网格检测 → INTER_AREA 缩到网格 → 调色板量化 → NEAREST 放大回原尺寸
    同时产出 thumb_N.jpg 缩略图（定稿全保+候选缩略）。
    max_colors 从 gates.mechanical.palette 读（与门禁同源：量化=64 则覆盖率必达标）。"""
    cfg = get_config()
    pal = (cfg.gates.get("mechanical") or {}).get("palette") or {}
    max_colors = int(pal.get("max_colors", 64))

    img = imread_unicode(src_path)
    if img is None:
        raise ValueError(f"无法读取图像: {src_path}")
    if img.shape[2] == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
    h, w = img.shape[:2]
    result = get_perfect_pixel(img[:, :, :3])
    gw, gh = max(1, int(result[0])), max(1, int(result[1]))
    small = cv2.resize(img, (gw, gh), interpolation=cv2.INTER_AREA)
    small = quantize_palette(small, max_colors)
    out = cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)
    if not imwrite_unicode(dst_path, out):
        raise ValueError(f"成品写入失败: {dst_path}")

    # 缩略图：长边压到 thumb_size，质量 82（约 30-100KB，够时间线预览）
    scale = thumb_size / max(w, h)
    if scale < 1:
        thumb = cv2.resize(out, (int(w * scale), int(h * scale)),
                           interpolation=cv2.INTER_AREA)
    else:
        thumb = out
    thumb_path = dst_path.parent / dst_path.name.replace(".png", ".jpg")
    if not imwrite_unicode(thumb_path, thumb[:, :, :3],
                           [cv2.IMWRITE_JPEG_QUALITY, 82]):
        raise ValueError(f"缩略图写入失败: {thumb_path}")
    return {"w": w, "h": h, "grid_w": gw, "grid_h": gh,
            "palette_colors": max_colors}


def process_run(raw_urls, out_dir: Path):
    """一次 run 的全部候选图：raw_N.png（留档）+ out_N.png（成品）→ 后处理元数据列表"""
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = []
    for i, url in enumerate(raw_urls, 1):
        raw_path = out_dir / f"raw_{i}.png"
        out_path = out_dir / f"out_{i}.png"
        download(url, raw_path)
        info = pixelate(raw_path, out_path)
        info["index"] = i
        meta.append(info)
    return meta
