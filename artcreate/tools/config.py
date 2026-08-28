"""artcreate · 配置加载器：base → project → derived 链式深合并（D22）
- base.yaml：工具出厂字典，随代码走，项目不直接改
- project.yaml：项目定制（仓库根），覆盖/追加 base
- derived/*.yaml：经验晋升产物（仓库根 derived/），最后合入
外部消费方一律 get_config() 拿单例；字典属性直接给合并结果。
"""
import copy
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent  # tools/ → artcreate包/ → 仓库根
BASE_YAML = Path(__file__).resolve().parent.parent / "base.yaml"  # 包内 artcreate/base.yaml
PROJECT_YAML = ROOT / "project.yaml"
DERIVED_DIR = ROOT / "derived"

# .env 尽早加载（任何入口 import artcreate 即生效）
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass


# ---------- 深合并 ----------
def _merge_list_by_id(base: list, over: list) -> list:
    """元素全为含 id 的 dict 时按 id 合并：同 id 递归合并，新 id 追加；否则整体替换。"""
    if not all(isinstance(x, dict) and "id" in x for x in base + over):
        return copy.deepcopy(over)

    merged = copy.deepcopy(base)
    index = {item["id"]: i for i, item in enumerate(merged)}
    for item in over:
        if item["id"] in index:
            merged[index[item["id"]]] = deep_merge(merged[index[item["id"]]], item)
        else:
            merged.append(copy.deepcopy(item))
            index[item["id"]] = len(merged) - 1
    return merged


def deep_merge(base, over):
    """递归深合并：over 覆盖 base。dict 按键合并；含 id 的 dict 列表按 id 合并；其余替换。"""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            if k in out:
                out[k] = deep_merge(out[k], v)
            else:
                out[k] = copy.deepcopy(v)
        return out
    if isinstance(base, list) and isinstance(over, list):
        return _merge_list_by_id(base, over)
    return copy.deepcopy(over)


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class Config:
    """加载一次，全局只读。属性直查，不做花哨封装。"""

    #: 本轮加载实际生效的层数据（base/project/derived），供调试与回源展示
    layers: dict

    def __init__(self, base_path: Path = BASE_YAML, project_path: Path = PROJECT_YAML,
                 derived_dir: Path = DERIVED_DIR):
        base = _load_yaml(base_path)
        project = _load_yaml(project_path)
        derived_files = sorted(derived_dir.glob("*.yaml")) if derived_dir.exists() else []
        derived = {}
        for p in derived_files:
            derived = deep_merge(derived, _load_yaml(p))

        self._raw = deep_merge(deep_merge(base, project), derived)
        self.layers = {"base": base, "project": project, "derived": derived}
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
    def character_poses(self):
        return self._raw.get("character_poses", {})

    @property
    def asset_suffixes(self):
        return self._raw.get("asset_suffixes", {})

    @property
    def vfx_elements(self):
        return self._raw.get("vfx_elements", {})

    @property
    def vfx_forms(self):
        return self._raw.get("vfx_forms", {})

    @property
    def constraint_axes(self):
        return self._raw["constraint_axes"]

    @property
    def sizes(self):
        return self._raw["sizes"]

    @property
    def gates(self):
        return self._raw.get("gates", {})

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

    # ---------- 字典溯源（供服务端工作台展示"这一项从哪来"） ----------
    def axis_source(self, axis_id: str, option_id: str = None) -> str:
        """返回该字典项的'出生层'：'base' / 'project' / 'derived' / 'unknown'。

        语义：id 首次出现的那一层即归属（base 出厂 / project 项目手写 /
        derived 经验晋升）。project 对 base 轴的修改不改变轴的出生归属，
        但新选项 (axis_id, option_id) 首现于 project 则归属 project。
        """
        for layer_name in ("base", "project", "derived"):
            layer = self.layers.get(layer_name) or {}
            for ax in layer.get("constraint_axes") or []:
                if ax.get("id") != axis_id:
                    continue
                if option_id is None:
                    return layer_name
                for opt in ax.get("options", []):
                    if opt.get("id") == option_id:
                        return layer_name
                # 轴存在但选项不在本层 → 继续往上层找该选项
        return "unknown"

    def axis_source_map(self) -> dict:
        """全量 {(axis_id, option_id|None): source} 映射，一次算齐给工作台用。"""
        out = {}
        for layer_name in ("base", "project", "derived"):
            layer = self.layers.get(layer_name) or {}
            for ax in layer.get("constraint_axes") or []:
                aid = ax.get("id")
                if (aid, None) not in out:
                    out[(aid, None)] = layer_name
                for opt in ax.get("options", []):
                    key = (aid, opt.get("id"))
                    if key not in out:
                        out[key] = layer_name
        return out


_cfg = None

def get_config() -> Config:
    global _cfg
    if _cfg is None:
        _cfg = Config()
    return _cfg
