"""artcreate · 编译词中文对照（消除编译信息差）

把 spec 的编译注入拆成结构化分段：每段 = 来源字段 + 中文说明 + 英文词组。
评审页逐段展示中英对照，用户无需读英文也能理解"编译器到底说了什么"。

中文来源全部取自字典已有字段（desc/label/zh/desc翻译），零 LLM 成本。
"""
from .tools.config import get_config


def explain_spec(spec: dict) -> list:
    """返回 [{section, zh, en}] 分段列表（按编译注入顺序）。"""
    cfg = get_config()
    out = []

    # 1. 资产类型 prefix
    at = spec.get("asset_type")
    if at and at in cfg.asset_types:
        d = cfg.asset_types[at]
        out.append({"section": "资产类型", "zh": f"{d['label']}：{d.get('desc', '')}",
                    "en": d.get("prefix", "")})

    # 2. 描述（中文→LLM 编译英文，中文就是用户输入）
    desc = spec.get("description")
    if desc:
        out.append({"section": "主体描述（LLM 编译）",
                    "zh": f"你的描述：{desc}",
                    "en": "（由 LLM 实时编译为英文，见完整 prompt）"})

    # 3. 氛围
    mood = spec.get("mood")
    if mood and mood in cfg.moods:
        d = cfg.moods[mood]
        if d.get("inject"):
            out.append({"section": "氛围光影", "zh": f"{d['label']}：{d.get('desc', '')}",
                        "en": d["inject"]})

    # 4. 约束轴
    cons = spec.get("constraints") or {}
    axes = {a["id"]: a for a in cfg.constraint_axes}
    for ax_id, opt_id in (cons.get("axis_sel") or {}).items():
        ax = axes.get(ax_id)
        if not ax:
            continue
        for o in ax.get("options", []):
            if o["id"] != opt_id or opt_id == "any":
                continue
            zh = o.get("zh") or {}
            parts = []
            en_parts = []
            pos = o.get("positive") or []
            neg = o.get("negative") or []
            zpos = zh.get("positive") or [x for x in pos]
            zneg = zh.get("negative") or [x for x in neg]
            if pos:
                parts.append(f"注入：{'、'.join(zpos)}")
                en_parts.append(", ".join(pos))
            if neg:
                parts.append(f"排除：{'、'.join(zneg)}")
                en_parts.append("avoid: " + ", ".join(neg))
            out.append({"section": f"约束·{ax['label']}",
                        "zh": f"{o['label']} → {'；'.join(parts)}",
                        "en": "; ".join(en_parts)})

    # 5. 自由约束
    free = cons.get("free_text")
    if free:
        out.append({"section": "自由约束（LLM 编译）", "zh": f"你写的：{free}",
                    "en": "（由 LLM 实时编译为英文）"})

    # 5.5 负向自由约束（编译进句尾排除块，非正向注入）
    free_neg = cons.get("free_text_negative")
    if free_neg:
        seg_en = "（由 LLM 编译为英文名词短语，进句尾排除块）"
        # 若 spec 携带编译分段（审核直通场景），用真实编译结果
        segs = spec.get("_compiled_segments") or {}
        if segs.get("free_negative_en"):
            seg_en = "Strictly avoid: " + segs["free_negative_en"]
        out.append({"section": "负向约束（排除块）",
                    "zh": f"你要求排除的：{free_neg} → 编译为排除名词清单，"
                          f"作为独立句放在提示词最末尾",
                    "en": seg_en})

    # 6. 画风
    st = spec.get("art_style")
    if st and st in cfg.art_styles:
        d = cfg.art_styles[st]
        out.append({"section": "画风", "zh": f"{d['label']}：{d.get('desc', '')}",
                    "en": ", ".join(filter(None, [
                        d.get("compiler_hint", ""), d.get("suffix", "")]))})

    # 7. 动作（角色线）
    ch = spec.get("character") or {}
    pose = ch.get("pose")
    if pose and pose in (cfg.character_poses or {}):
        d = cfg.character_poses[pose]
        out.append({"section": "角色动作", "zh": f"{d['label']}：{d.get('desc', '')}",
                    "en": d.get("inject", "")})

    return out
