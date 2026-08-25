"""artcreate · L2 编译自检 + L3 意图回查（阶段3-B，D18 纠偏机制核心）

L2 编译自检：第二个 LLM 调用，拿 spec 原始输入反向核对编译产物——
  覆盖度（每项输入是否体现在 prompt）/ 越权（prompt 是否引入了未要求的内容）。
L3 意图回查：VLM 拿"用户原始约束"直接验图（绕过 prompt），输出违反率。
  参照系是 spec 原始中文意图——策划唯一信任的东西。
"""
import base64
import json
from pathlib import Path

from ..tools.config import get_config
from ..tools.llm_client import text_chat, vlm_chat

L2_META_PROMPT = """You are a QA auditor for a prompt compiler (D18-L2).
Given the ORIGINAL form inputs and the COMPILED prompt, check translation fidelity.

ORIGINAL FORM (JSON):
{spec}

COMPILED PROMPT (system parts already stripped; user-domain only):
{prompt}

The prompt below has system parts REMOVED (media prefix, style suffix,
"strictly no ..." lists, mood lighting injection). Audit ONLY user fidelity:

MAPPING RULES — how form items appear (do NOT report as missing):
- description → ENGLISH PARAPHRASE near the start (faithful meaning, not
  word-for-word). Paraphrase ≠ mistranslation.
- extra_prompt → English paraphrase if non-empty.
Check ONLY user-input fidelity. Ignore size/count (not prompt content).
Answer STRICT JSON only:
{{
  "coverage": [list of USER FORM items genuinely missing or CONTRADICTED in the prompt],
  "intrusion": [list of objects/scenes the prompt ADDS that the user form never asked for],
  "verdict": "pass" | "warn"
}}
Paraphrase is fine; only flag meaning loss, contradiction, or added objects/scenes."""


def audit_compilation(spec: dict, compiled_prompt: str,
                      segments: dict = None) -> dict:
    """L2：返回 {coverage, intrusion, verdict}。失败返回 verdict=error（不阻断）。
    程序侧剥离全部系统/派生段（媒介前缀/扩展词/氛围/正向引导/负向块/画风尾块），
    LLM 只审用户域两段：scene_core（对 description 的忠实转译）与 extra（细化项）。
    小模型指令遵循不稳——能程序侧解决的绝不托付 prompt。"""
    cfg = get_config()
    audited = compiled_prompt
    if segments:
        style_id = spec.get("art_style", cfg.defaults["art_style"])
        style = cfg.art_styles.get(style_id) or cfg.art_styles[cfg.defaults["art_style"]]
        for part in (style["suffix"], segments.get("media_prefix"),
                     segments.get("scene_expansion"), segments.get("mood_inject"),
                     segments.get("atmosphere_notes"),
                     segments.get("extra_en"),
                     *segments.get("positives", [])):
            if part:
                audited = audited.replace(part, "")
    else:
        # 无 segments 时退回保守剥离（画风尾块 + 前缀 + strictly no）
        style_id = spec.get("art_style", cfg.defaults["art_style"])
        style = cfg.art_styles.get(style_id) or cfg.art_styles[cfg.defaults["art_style"]]
        audited = compiled_prompt.replace(style["suffix"], "")
        at_id = spec.get("asset_type", cfg.defaults["asset_type"])
        audited = audited.replace(cfg.asset_types[at_id]["prefix"], "")
    import re as _re
    audited = _re.sub(r"strictly no [^,]*(?:, [^,]*)*(?=,|$)", "", audited)
    audited = _re.sub(r"— [^,]*(?=,|$)", "", audited)
    audited = ", ".join(p for p in (x.strip() for x in audited.split(",")) if p)

    try:
        raw = text_chat(L2_META_PROMPT
                        .replace("{spec}", json.dumps(spec, ensure_ascii=False))
                        .replace("{prompt}", audited),
                        temperature=0.1)
        import re
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return {"coverage": [], "intrusion": [], "verdict": "error"}
        d = json.loads(m.group(0))
        return {"coverage": d.get("coverage", []),
                "intrusion": d.get("intrusion", []),
                "verdict": d.get("verdict", "error")}
    except Exception as e:
        return {"coverage": [], "intrusion": [], "verdict": f"error: {e}"}


def _build_intent_questions(spec: dict):
    """从 spec 原始意图构造验图问题（中文问 VLM，回答 yes/no 可统计）。
    只取'可视觉判定'的约束：约束轴 + 自由文本；描述主体作为辅助问题。"""
    cfg = get_config()
    questions = []
    axis_sel = (spec.get("constraints") or {}).get("axis_sel", {}) or {}

    axis_q = {
        "human_trace": {"natural": "画面中是否完全没有人工建筑/道路/桥梁等人造物？",
                         "present": "画面中是否有人工痕迹（建筑/道路/桥梁等）？"},
        "water": {"none": "画面中是否完全没有河流/湖泊/水塘等水体？",
                  "present": "画面中是否有水体（河流/湖泊/水塘等）？"},
        "enclosure": {"open": "画面是否是开阔场景（能看到天空/地平线）？",
                      "enclosed": "画面是否是封闭室内/洞穴场景（看不到开阔天空）？"},
        "vegetation": {"barren": "画面是否几乎没有植被（荒芜地表）？",
                       "lush": "画面是否有茂密植被？"},
    }
    for axis_id, opts in axis_q.items():
        sel = axis_sel.get(axis_id)
        if sel and sel in opts:
            questions.append({"source": f"axis:{axis_id}={sel}",
                              "question": opts[sel],
                              "expect": "yes"})

    free_text = str((spec.get("constraints") or {}).get("free_text") or "").strip()
    if free_text:
        questions.append({
            "source": f"free_text",
            "question": f"用户的约束是：\"{free_text}\"。这张图是否满足该约束？（只答 yes 或 no）",
            "expect": "yes"})

    questions.append({"source": "no_people",
                      "question": "画面中是否没有人物或人形生物？",
                      "expect": "yes"})
    return questions


def audit_intent(image_path, spec: dict) -> dict:
    """L3：VLM 拿原始意图逐条验图。返回 {results: [...], violation_rate}。
    高置信提示用，非硬拦截（D18 定案）。"""
    p = Path(image_path)
    b64 = base64.b64encode(p.read_bytes()).decode()
    results = []
    for q in _build_intent_questions(spec):
        try:
            ans = vlm_chat(b64, q["question"] + "（只回答 yes 或 no）")
            ok = _parse_yesno(ans, q["expect"])
            results.append({**q, "answer": ans.strip()[:40], "ok": bool(ok)})
        except Exception as e:
            results.append({**q, "answer": f"error: {e}", "ok": None})
    judged = [r for r in results if r["ok"] is not None]
    violated = [r for r in judged if not r["ok"]]
    rate = len(violated) / len(judged) if judged else None
    return {"results": results, "violation_rate": rate}


def _parse_yesno(ans: str, expect: str) -> bool:
    """中英文 yes/no 解析。expect='yes'：肯定回答=满足。
    处理："yes"/"no"/"是的"/"没有（是没有水体→满足'无水体'问句时仍需肯定）"。
    问句全部构造成"是否满足约束"形式，肯定（yes/是/有的/可以看到）=满足。"""
    a = ans.strip().lower()
    # 中文否定词开头 → 不满足
    if a.startswith(("不", "没有。", "没。", "否", "无。")) or a == "没有":
        return False
    # 中文肯定开头 → 满足
    if a.startswith(("是", "有", "可以", "能", "对")):
        return True
    # 英文
    if a.startswith(("yes", "correct", "true")):
        return True
    if a.startswith(("no", "false", "not")):
        return False
    # 兜底：包含判定
    return a.startswith("yes") if expect == "yes" else not a.startswith("yes")
