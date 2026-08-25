"""artcreate · 配置加载器：project.yaml → 运行时配置（单一事实源，密钥走 .env）"""
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent  # tools/ → artcreate包/ → 仓库根
PROJECT_YAML = ROOT / "project.yaml"


class Config:
    """加载一次，全局只读。属性直查，不做花哨封装。"""

    def __init__(self, path: Path = PROJECT_YAML):
        with open(path, encoding="utf-8") as f:
            self._raw = yaml.safe_load(f)
        self.root = ROOT

    # ---------- 顶层快捷访问 ----------
    @property
    def defaults(self):
        return self._raw["defaults"]

    @property
    def active_provider(self) -> str:
        return os.getenv("ACTIVE_PROVIDER", self._raw.get("active_provider", "ark"))

    def provider_conf(self, pid: str = None) -> dict:
        pid = pid or self.active_provider
        conf = dict(self._raw["providers"][pid])
        # 密钥环境变量解析（不落盘、不进日志）
        for key in ("api_key", "access_key", "secret_key"):
            env = conf.get(f"{key}_env")
            if env:
                conf[key] = os.getenv(env, "")
        return conf

    def llm_conf(self, kind: str = "compiler") -> dict:
        conf = dict(self._raw["llm"][kind])
        conf["api_key"] = os.getenv(conf.pop("api_key_env", "ZHIPU_API_KEY"), "")
        return conf

    # ---------- 字典 ----------
    @property
    def art_styles(self):
        return self._raw["art_styles"]

    @property
    def asset_types(self):
        return self._raw["asset_types"]

    @property
    def moods(self):
        return self._raw["moods"]

    @property
    def constraint_axes(self):
        return self._raw["constraint_axes"]

    @property
    def sizes(self):
        return self._raw["sizes"]

    def validate_size(self, size: str, with_ref: bool = False) -> str:
        """尺寸白名单校验；参考图模式下加最小像素约束（D19）。"""
        if size not in self.sizes:
            raise ValueError(f"非法尺寸 {size}，可选：{list(self.sizes)}")
        if with_ref:
            w, h = map(int, size.split("x"))
            min_px = self.provider_conf().get("min_pixels_with_ref", 0)
            if w * h < min_px:
                raise ValueError(
                    f"参考图模式要求总像素 ≥{min_px}（D19 实测），{size} 不满足")
        return size


_cfg = None

def get_config() -> Config:
    global _cfg
    if _cfg is None:
        _cfg = Config()
    return _cfg
