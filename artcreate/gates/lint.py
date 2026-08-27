"""artcreate · L1 编译前 lint（D18-L1）：纯规则静态检查，生成前拦截已知失败模式

检查项：
1. 负向注入模式：自由文本约束里的"不要X/no X/不含X"式否定（模型会反向激活）
2. 画风条款冲突：自由文本与画风尾块的渲染词打架（D13 教训）
3. token 超限：编译后 prompt 过长有尾部截断风险
4. 空描述/非法枚举值

返回 (warnings, suggestions)：警告不阻断（用户可执意），建议是一键修复（D18 铁律）。
"""
import re

# ---------- 规则库 ----------
NEGATION_PATTERNS = [
    (re.compile(r"(不要|不能|不可|别|禁止|不含|没有|无)[\u4e00-\u9fff\w]{1,12}", ), "zh"),
    (re.compile(r"\b(no|without|never|avoid)\s+[\w-]+", re.I), "en"),
]

# token 估算：英文 ~4 字符/token；Seedream 系上限约 512 token（保守按 480 预警）
PROMPT_TOKEN_WARN = 480


def lint_spec(spec: dict, compiled_prompt: str = "", art_style: str = ""):
    """返回 warnings 列表：[{level, code, message, fix}]。level: warn|block。"""
    from ..tools.config import get_config
    cfg = get_config()

    warnings = []
    desc = spec.get("description", "").strip()
    free_text = str((spec.get("constraints") or {}).get("free_text") or "").strip()
    art_style = art_style or spec.get("art_style", cfg.defaults["art_style"])

    # 1. 空描述
    if not desc:
        warnings.append({"level": "block", "code": "EMPTY_DESC",
                         "message": "场景描述为空，无法编译",
                         "fix": None})

    # 2. 负向注入扫描（自由约束文本是重灾区；描述里的否定交给编译器语义处理）
    #    注意：constraints.free_text_negative（负向自由约束）不扫——
    #    它本来就该写否定，编译时直通句尾排除块（正确用法）
    #    extra_prompt 为管线内部语料（pose inject 等，预写英文），不扫
    for text, field in ((free_text, "自由约束"),):
        for pattern, lang in NEGATION_PATTERNS:
            m = pattern.search(text)
            if m:
                matched = m.group(0)
                if lang == "zh":
                    fix = f"改用约束合集对应轴（如'无水体'轴）或下方'负向自由约束'栏，或写正向表述：描述'应该是什么'（例：'干涸开裂的河床'）"
                else:
                    fix = "Use positive phrasing: describe what SHOULD be there instead, or move it to the negative constraints field"
                warnings.append({
                    "level": "warn", "code": "NEGATION_INJECTION",
                    "message": f"{field}含否定表述\"{matched}\"：生成模型不理解否定，"
                               f"否定词会被反向激活（粉红大象问题），越说越可能出现",
                    "fix": fix})

    # 3. 画风条款冲突（schema v2：冲突词从画风字典 conflict_words 读取）
    style_conf = cfg.art_styles.get(art_style, {})
    conflicts = [w for w in style_conf.get("conflict_words", [])
                 if w in desc or w in free_text]
    if conflicts:
        warnings.append({
            "level": "warn", "code": "STYLE_CONFLICT",
            "message": f"输入含与当前画风冲突的词：{'、'.join(conflicts)}"
                       f"（当前画风尾块与之矛盾，输出会随机倒向一边）",
            "fix": f"移除冲突词，画风交给画风项统一控制"})

    # 4. token 超限预警
    if compiled_prompt:
        est_tokens = len(compiled_prompt) // 4
        if est_tokens > PROMPT_TOKEN_WARN:
            warnings.append({
                "level": "warn", "code": "TOKEN_OVERFLOW",
                "message": f"编译后 prompt 约 {est_tokens} token，接近模型上限，"
                           f"尾部条款（画风尾块）有被静默截断的风险",
                "fix": "精简场景描述或细化项"})

    # 5. 枚举值合法性
    if spec.get("asset_type") and spec["asset_type"] not in cfg.asset_types:
        warnings.append({"level": "warn", "code": "BAD_ASSET_TYPE",
                         "message": f"未知资产类型 {spec['asset_type']}，将回退默认",
                         "fix": None})
    if spec.get("mood") and spec["mood"] not in cfg.moods:
        warnings.append({"level": "warn", "code": "BAD_MOOD",
                         "message": f"未知氛围 {spec['mood']}，将被忽略",
                         "fix": None})
    if spec.get("art_style") and spec["art_style"] not in cfg.art_styles:
        warnings.append({"level": "warn", "code": "BAD_ART_STYLE",
                         "message": f"未知画风 {spec['art_style']}，将回退默认",
                         "fix": None})

    return warnings


def format_warnings(warnings) -> str:
    """CLI 展示用。"""
    if not warnings:
        return "L1 lint 通过，无警告"
    lines = []
    for w in warnings:
        mark = "⛔" if w["level"] == "block" else "⚠️"
        lines.append(f"{mark} [{w['code']}] {w['message']}")
        if w.get("fix"):
            lines.append(f"   修复建议：{w['fix']}")
    return "\n".join(lines)
