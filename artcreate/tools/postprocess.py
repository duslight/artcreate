"""artcreate · 后处理：下载 RAW → 像素网格化 → 落盘 exports 目录结构

相对 art_pipeline 版本：目录结构改为资产契约（D-工厂三件套）：
  exports/{subject}/{run_id}/raw.png  raw_N.png / out_N.png
"""
import urllib.request
from pathlib import Path

import cv2
import numpy as np
from perfect_pixel import get_perfect_pixel

from .config import get_config


def download(url: str, dest: Path):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        f.write(r.read())


def pixelate(src_path: Path, dst_path: Path):
    """网格检测 → INTER_AREA 缩到网格 → NEAREST 放大回原尺寸（VibeGame pixel_clean 复刻）"""
    img = cv2.imread(str(src_path), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError(f"无法读取图像: {src_path}")
    if img.shape[2] == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
    h, w = img.shape[:2]
    result = get_perfect_pixel(img[:, :, :3])
    gw, gh = max(1, int(result[0])), max(1, int(result[1]))
    small = cv2.resize(img, (gw, gh), interpolation=cv2.INTER_AREA)
    out = cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)
    cv2.imwrite(str(dst_path), out)
    return {"w": w, "h": h, "grid_w": gw, "grid_h": gh}


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
