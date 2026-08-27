"""artcreate · Prompt 编译器 v3：spec → 全英文 prompt（继承 art_pipeline v2 全部行为）

v3 相对 v2 的变化（D21）：
- spec 改持链式修订：compile(spec) 永远基于最新 revision 全量编译，改稿=改 spec 非改 prompt
- 新增 edit 模式：元素级改稿指令 → 编译为正向英文编辑短指令（供参考图编辑用）
- 字典从 project.yaml 读取（config.get_config()）
"""
import json
import re

from .config import get_config
from .llm_client import text_chat

META_PROMPT = """You are a prompt compiler for a game art asset pipeline.
Compile the Chinese form below into ENGLISH prompt components for an image model.

Context you MUST respect (expansion must not contradict any of them):
- User scene description: {description}
- REQUIRED positive constraints (from the user's axis choices and free constraints — every expansion phrase must be consistent with them; if a simple/flat background is required, do NOT expand environment or background details at all): {positives}
- Selected mood (lighting/atmosphere direction): {mood_desc}
- Asset type: {asset_type_desc}
- Art style direction: {style_hint}
- Elements to EXCLUDE (never mention them, not even to negate): {exclusions}
- Free-text constraints to translate: {free_constraints}
- Negative free-text constraints (translate for the exclusion block): {free_negative}

Output STRICT JSON only, with these keys:
1. "scene_core_en": faithful ENGLISH translation of the scene description. Translate only, do NOT add or remove content.
2. "scene_expansion": 3-6 concrete visual NOUN-phrases that naturally belong to the scene, consistent with the mood, the style AND the REQUIRED positive constraints. Comma separated. No story. Never include anything from the EXCLUDE list or the negative constraints.
3. "atmosphere_notes": 1-2 short phrases about stillness/quiet unease/restrained mood (e.g. "faint drifting dust", "unnatural stillness").
4. "constraints_en": faithful ENGLISH translation of the free-text constraints ONLY. Do NOT restate or translate the REQUIRED positive constraints above. Empty string if the free-text constraints are empty.
5. "negative_en": faithful ENGLISH translation of the negative free-text constraints, as bare noun phrases WITHOUT negation words (e.g. "不要蜘蛛" → "spiders"). Empty string if none.

Rules:
- Keep every phrase under 8 words.
- Do NOT include style words (palette/outline/dithering/pixel) — handled elsewhere.
- Do NOT include lighting design — the mood injection is handled separately."""

EDIT_META_PROMPT = """You are an image edit instruction compiler (D21).
The user wants to REVISE an existing game art image. Translate their Chinese
edit request into ONE positive English edit instruction for an image model
that will receive the original image as reference.

Rules:
- POSITIVE phrasing only: describe the desired end state, never use "no/not/remove" alone.
  e.g. "把河改掉" → "replace the river with a dry cracked riverbed"
- Target the ELEMENT level (add/change/delete one thing), keep everything else.
- Under 30 words. Output the instruction text ONLY, no quotes, no explanation.

User edit request: {edit_request}
Current scene (for context): {scene_core}"""


def resolve_constraints(spec: dict):
    """约束合集解析：轴选择 → (排除词, 正向引导词)。字典直查，零 LLM。"""
    cfg = get_config()
    axis_sel = (spec.get("constraints") or {}).get("axis_sel", {}) or {}
    negatives, positives = [], []
    for axis in cfg.constraint_axes:
        opt_id = axis_sel.get(axis["id"], "any")
        opt = next((o for o in axis["options"] if o["id"] == opt_id), None)
        if not opt:
            continue
        negatives += opt.get("negative", [])
        positives += opt.get("positive", [])
    return negatives, positives


def _mode_of(spec: dict) -> str:
    """推断 spec 的页面模式：character/monster/scene（供按模式配置项查询）。"""
    atype = spec.get("asset_type", "")
    if str(atype).startswith("monster"):
        return "monster"
    if str(atype).startswith("character") or (spec.get("character") or {}).get("anchor") \
            or (spec.get("character") or {}).get("sheet"):
        return "character"
    return "scene"


def _forced_negative(cfg, mode: str) -> list:
    """模式级强制负向（base/project yaml 的 forced_negative 段，可配置）。"""
    conf = cfg._raw.get("forced_negative", {}) or {}
    return [str(x) for x in (conf.get(mode) or [])]


def compile_prompt(spec: dict) -> dict:
    """spec → {"prompt": str, "segments": {...}}。
    segments 供 lint/消融实验（D-黑箱调试法）与 manifest（D19）使用。"""
    cfg = get_config()
    d = cfg.defaults
    desc = spec.get("description", "").strip()
    asset_type = spec.get("asset_type", d["asset_type"])
    mood = spec.get("mood", d["mood"])
    art_style = spec.get("art_style", d["art_style"])
    cons_form = spec.get("constraints") or {}
    free_constraints = str(cons_form.get("free_text") or "").strip()
    free_negative = str(cons_form.get("free_text_negative") or "").strip()

    style = cfg.art_styles.get(art_style) or cfg.art_styles[d["art_style"]]
    mood_inject = cfg.moods.get(mood, {}).get("inject", "")
    negatives, positives = resolve_constraints(spec)
    asset_conf = cfg.asset_types.get(asset_type) or cfg.asset_types[d["asset_type"]]
    mode = _mode_of(spec)

    core_en, expansion, atmosphere, cons_en, neg_en = "", "", "", "", ""
    try:
        prompt = (META_PROMPT
                  .replace("{description}", desc)
                  .replace("{positives}",
                           ", ".join(positives) if positives else "(none)")
                  .replace("{mood_desc}", mood_inject or "(none)")
                  .replace("{asset_type_desc}", asset_conf["prefix"])
                  .replace("{style_hint}", style["compiler_hint"])
                  .replace("{exclusions}",
                           ", ".join(negatives) if negatives else "(none)")
                  .replace("{free_constraints}",
                           free_constraints or "(none)")
                  .replace("{free_negative}",
                           free_negative or "(none)"))
        raw = text_chat(prompt)
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            parsed = json.loads(m.group(0))
            core_en = (parsed.get("scene_core_en", "") or "").strip()
            expansion = (parsed.get("scene_expansion", "") or "").strip()
            atmosphere = (parsed.get("atmosphere_notes", "") or "").strip()
            cons_en = (parsed.get("constraints_en", "") or "").strip()
            neg_en = (parsed.get("negative_en", "") or "").strip()
    except Exception:
        pass  # 编译降级不报错（v1 行为：中文直通）

    # 注入顺序（2026-08-27 重构）：prefix → 场景翻译 → 约束轴正向 + 正向自由约束
    # → LLM 扩展 → 氛围。约束先于扩展注入，正向约束同时作为 META_PROMPT
    # 必守上下文（扩展不得与之冲突：角色"纯色简洁"背景轴 vs 自由扩展环境词）
    # 注意：LLM 偶尔把 REQUIRED positives 上下文复述进 constraints_en（轴词翻倍），
    # 仅当用户真的写了自由约束时才采信 cons_en
    parts = [asset_conf["prefix"]]
    parts.append(core_en if core_en else desc)
    parts += positives
    if free_constraints and cons_en:
        parts.append(cons_en)
    elif free_constraints:
        parts.append(free_constraints)   # LLM 失败降级：中文直通
    parts.append(expansion)
    parts.append(mood_inject)
    parts.append(atmosphere)
    parts.append(style["suffix"])

    # ---- 负向排除块：拆词 → 全局去重 → 句法隔离放真句尾 ----
    # 词表来源：轴负向 + 模式强制负向（forced_negative，配置化）+ 负向自由约束
    # （LLM 裸名词串或中文降级）
    def _split_items(s: str) -> list:
        return [t.strip() for t in re.split(r"[，,、]", s) if t.strip()]
    neg_raw = list(negatives) + _forced_negative(cfg, mode)
    if neg_en:
        neg_raw += _split_items(neg_en)
    elif free_negative:
        neg_raw += _split_items(free_negative)   # LLM 失败降级：中文直通
    neg_items, seen = [], set()
    for item in neg_raw:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            neg_items.append(item)

    # 主句（正向流）与排除块用句号隔离：T5 编码器把 "Strictly avoid:" 句
    # 当独立指令处理，逗号长流里排除词会被画风词稀释（2026-08-26 实测回归）
    full = ", ".join(p.strip().rstrip(".") for p in parts if p and p.strip())
    if neg_items:
        full += ". Strictly avoid: " + ", ".join(neg_items) + "."
    # 三槽位参考图分工说明（2026-08-27）：告知模型各参考图用途，
    # 顺序语义（第1张管长相）方舟 API 无显式标签，靠此句 + 拼接顺序软约束
    ref_note = str(spec.get("ref_role_note") or "").strip()
    if ref_note:
        full += " " + ref_note
    return {
        "prompt": full,
        "segments": {  # 分段结构落 manifest（消融实验/溯源 D19）
            "media_prefix": asset_conf["prefix"],
            "scene_core_en": core_en or desc,
            "positives": positives,
            "free_constraints_en": (cons_en if free_constraints else "") or free_constraints,
            "scene_expansion": expansion,
            "mood_inject": mood_inject,
            "atmosphere_notes": atmosphere,
            "free_negative_en": neg_en or free_negative,
            "negative_block": neg_items,
            "style_suffix": style["suffix"],
        },
    }


def compile_edit_instruction(edit_request: str, scene_core: str = "") -> str:
    """元素级改稿指令 → 正向英文编辑短指令（D21：改稿必重编译，禁止 no X 拼接）。"""
    raw = text_chat(EDIT_META_PROMPT
                    .replace("{edit_request}", edit_request)
                    .replace("{scene_core}", scene_core or "(unknown)"))
    # 剥可能的引号/换行，取首行
    line = raw.strip().splitlines()[0].strip().strip('"').strip("'")
    return line
