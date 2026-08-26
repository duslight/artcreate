"""artcreate · 服务端工作台 API（阶段5）

- GET /api/form/dict        表单字典（sizes/styles/asset_types/moods/axes，
                            含 axis_source 溯源徽标数据 + defaults）
- POST /api/jobs            提交生成任务（spec 校验 → 入队）
- GET  /api/jobs            任务列表
- GET  /api/jobs/{id}       任务状态轮询
- POST /api/runs/{id}/regenerate  再生（也走队列）

访问令牌：环境变量 WORKBENCH_TOKEN 配置后，全部 API 需带 X-Token 头
（或 ?token= 查询参数，方便手机浏览器直接打开）。未配置则放行（本地模式）。
挂载方式：server.app.include_router(workbench.router)
"""
import json
import os
import time
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse

from . import store, jobs
from .tools.config import get_config
from .tools.spec_validate import validate_spec, format_issues

router = APIRouter()

cfg = get_config()


def _check_token(request: Request, x_token: str = Header(None)):
    """互信+令牌双层：本地不配 token 全放行；配置了就强制。"""
    need = os.getenv("WORKBENCH_TOKEN", "").strip()
    if not need:
        return
    got = x_token or request.query_params.get("token") or ""
    if got != need:
        raise HTTPException(401, "无效访问令牌（X-Token 头或 ?token= 参数）")


def _actor(x_actor_id: str = None, x_actor_name: str = None) -> dict:
    return {"id": x_actor_id or "web-anonymous", "name": x_actor_name or ""}


# ---------- 表单字典 ----------
@router.get("/api/form/dict")
def form_dict(request: Request, x_token: str = Header(None)):
    """工作台表单渲染所需全部字典 + 默认值 + 溯源。
    ui_hidden 条目不返回（表单精简；编译兼容由后端 spec 校验兜底）。"""
    _check_token(request, x_token)
    src_map = cfg.axis_source_map()
    axes = []
    for ax in cfg.constraint_axes:
        opts = []
        for o in ax.get("options", []):
            if o.get("ui_hidden"):
                continue
            item = dict(o)
            item["source"] = src_map.get((ax["id"], o.get("id")), "base")
            opts.append(item)
        axes.append({"id": ax["id"], "label": ax.get("label", ax["id"]),
                     "desc": ax.get("desc", ""),
                     "applies_to": ax.get("applies_to", []),
                     "non_promotable": bool(ax.get("non_promotable")),
                     "options": opts})
    return {
        "defaults": cfg.defaults,
        "sizes": cfg.sizes,
        "art_styles": {k: v for k, v in cfg.art_styles.items()
                       if not v.get("ui_hidden")},
        "asset_types": {k: v for k, v in cfg.asset_types.items()
                        if not v.get("ui_hidden")},
        "moods": {k: v for k, v in cfg.moods.items()
                  if not v.get("ui_hidden")},
        "character_poses": cfg.character_poses,
        "asset_suffixes": cfg.asset_suffixes,
        "constraint_axes": axes,
    }


# ---------- 任务 ----------
@router.post("/api/compile-preview")
def compile_preview(request: Request, spec: dict,
                    x_token: str = Header(None),
                    x_actor_id: str = Header(None), x_actor_name: str = Header(None)):
    """编译预览（位置A 人工审核）：同步编译返回 prompt + explain 分段 + lint 警告。
    不调生图 API、不产生生图费用（仅 LLM 编译，几秒/几厘钱）。
    用户确认后带 compiled_prompt 入队 = 所见即所执行。"""
    _check_token(request, x_token)
    from .tools.compiler import compile_prompt
    from .gates.lint import lint_spec
    from .explain import explain_spec
    issues = validate_spec(spec)
    errors = [i for i in issues if i["level"] == "error"]
    if errors:
        raise HTTPException(422, {"issues": issues,
                                  "message": format_issues(issues)})
    result = compile_prompt(spec)
    warnings = lint_spec(spec, result["prompt"])
    # 完整 prompt 的中文翻译（审核用：不懂英文也能核对全貌）
    prompt_zh = ""
    try:
        from .tools.llm_client import text_chat
        raw = text_chat(
            "把下面的英文生图提示词翻译成通顺的中文，直接输出译文，"
            "不要任何解释：\n" + result["prompt"], temperature=0.2)
        prompt_zh = raw.strip()
    except Exception:
        prompt_zh = ""   # 翻译失败不阻断审核（分段对照仍在）
    return {
        "prompt": result["prompt"],
        "prompt_zh": prompt_zh,
        "segments": result["segments"],
        "explain": explain_spec(spec),
        "lint": warnings,
        "spec_issues": [i for i in issues if i["level"] == "warn"],
    }


@router.post("/api/retranslate")
def retranslate(request: Request, body: dict, x_token: str = Header(None)):
    """精修模式·分段批量重译：改中文段 → 对应英文段同步。
    body: {"items": [{"key": "scene_core_en", "zh": "雪原上的篝火营地，夜晚"},
                      {"key": "free_negative_en", "zh": "月亮、雾气"}]}
    返回 {"translations": {"scene_core_en": "...", ...}}
    规则：
    - scene_core_en / scene_expansion / atmosphere_notes / free_constraints_en
      → 忠实翻译（只译不加）
    - free_negative_en → 裸名词短语（无否定词，编译期再进排除块重组去重）
    单次 LLM 调用批量完成（省时省钱）；LLM 失败返回 503 由前端提示。"""
    _check_token(request, x_token)
    items = body.get("items") or []
    if not items:
        return {"translations": {}}
    allowed = {"scene_core_en", "scene_expansion", "atmosphere_notes",
               "free_constraints_en", "free_negative_en"}
    tasks = [{"key": it["key"], "zh": str(it.get("zh", "")).strip()}
             for it in items if it.get("key") in allowed and str(it.get("zh", "")).strip()]
    if not tasks:
        return {"translations": {}}
    from .tools.llm_client import text_chat
    spec_lines = "\n".join(
        f'{t["key"]}: {t["zh"]}' for t in tasks)
    meta = (
        "Translate the following Chinese prompt segments into ENGLISH for an "
        "image model. Output STRICT JSON only: an object mapping each segment "
        "key to its English translation.\n"
        "Rules per key:\n"
        "- scene_core_en / free_constraints_en: faithful translation only, "
        "do NOT add or remove content.\n"
        "- scene_expansion / atmosphere_notes: comma-separated short noun "
        "phrases, each under 6 words.\n"
        "- free_negative_en: bare noun phrases WITHOUT any negation words "
        "(e.g. 月亮、雾气 → moon, mist).\n"
        "- Keep every phrase under 8 words. No style words, no lighting words.\n\n"
        "Segments:\n" + spec_lines)
    try:
        raw = text_chat(meta, temperature=0.2)
        import re as _re
        m = _re.search(r"\{.*\}", raw, _re.S)
        parsed = json.loads(m.group(0)) if m else {}
    except Exception as e:
        raise HTTPException(503, f"重译失败（LLM 通道异常）：{e}")
    out = {t["key"]: str(parsed.get(t["key"], "")).strip()
           for t in tasks if str(parsed.get(t["key"], "")).strip()}
    missing = [t["key"] for t in tasks if t["key"] not in out]
    if missing:
        raise HTTPException(502, f"LLM 未返回分段：{missing}")
    return {"translations": out}


@router.post("/api/retranslate-full")
def retranslate_full(request: Request, body: dict, x_token: str = Header(None)):
    """精修模式·整段直改：用户改完整中文 prompt → 对齐改写英文全文。
    body: {"orig_zh": 原中文全文, "orig_en": 原英文全文, "edited_zh": 改后中文全文}
    返回 {"en": 新英文全文}
    对齐规则（防 LLM 擅自润色导致全篇漂移）：
    - 逐句对齐：改动部分重译；未改部分直接抄原英文（不再翻译）
    - 专有名词（Dead Cells 等）与固定句式（Strictly avoid: ...）原样保形
    - 用户不知道模板约束→英文映射也能改：改的就是中文全文本身"""
    _check_token(request, x_token)
    orig_zh = str(body.get("orig_zh") or "").strip()
    orig_en = str(body.get("orig_en") or "").strip()
    edited_zh = str(body.get("edited_zh") or "").strip()
    if not (orig_zh and orig_en and edited_zh):
        raise HTTPException(422, "orig_zh / orig_en / edited_zh 均必填")
    if edited_zh == orig_zh:
        return {"en": orig_en}
    from .tools.llm_client import text_chat
    meta = (
        "You are aligning two versions of an image prompt. This is an "
        "IN-PLACE EDIT, not an append.\n"
        "ORIGINAL Chinese prompt:\n" + orig_zh + "\n\n"
        "ORIGINAL English prompt (its translation):\n" + orig_en + "\n\n"
        "EDITED Chinese prompt (the user revised the Chinese IN PLACE — some "
        "phrases were replaced, added or deleted at their original positions):\n"
        + edited_zh + "\n\n"
        "Task: output the EDITED English prompt — same structure and length "
        "as the ORIGINAL English, with ONLY the changed parts updated.\n"
        "Rules:\n"
        "- CRITICAL: do NOT append the translated changes at the end. Locate "
        "the changed phrase's position in the original and REPLACE it there. "
        "The output must NOT contain both the old and the new version of a "
        "phrase.\n"
        "- The output length must stay close to the original English length "
        "(±20%). If your output is much longer, you appended instead of "
        "replacing.\n"
        "- For parts the user did NOT change, copy the original English "
        "VERBATIM (do not re-translate, do not rephrase, do not 'improve').\n"
        "- Keep proper nouns exactly as-is (e.g. Dead Cells, Blasphemous).\n"
        "- Keep structural formulas verbatim, e.g. the trailing exclusion "
        "sentence 'Strictly avoid: a, b, c.' — update its noun list only if "
        "the user changed it, and keep it at the very end.\n"
        "- Keep the comma-separated prompt format. No explanations, no "
        "quotes, no markdown.\n"
        "Output the edited English prompt ONLY.")
    try:
        raw = text_chat(meta, temperature=0.2)
    except Exception as e:
        raise HTTPException(503, f"整段重译失败（LLM 通道异常）：{e}")
    en = raw.strip().strip('"').strip("`").strip()
    if not en:
        raise HTTPException(502, "LLM 返回为空")
    return {"en": en}


@router.post("/api/translate-prompt")
def translate_prompt(request: Request, body: dict, x_token: str = Header(None)):
    """英文 prompt → 中文全文（精修·整段直改的中文底稿，历史 run 沿用编译时按需补译）。"""
    _check_token(request, x_token)
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(422, "prompt 必填")
    from .tools.llm_client import text_chat
    try:
        raw = text_chat(
            "把下面的英文生图提示词翻译成通顺的中文，直接输出译文，"
            "不要任何解释：\n" + prompt, temperature=0.2)
        zh = raw.strip()
    except Exception as e:
        raise HTTPException(503, f"翻译失败（LLM 通道异常）：{e}")
    if not zh:
        raise HTTPException(502, "LLM 返回为空")
    return {"zh": zh}


@router.post("/api/jobs")
def create_job(request: Request, spec: dict,
               x_token: str = Header(None),
               x_actor_id: str = Header(None), x_actor_name: str = Header(None)):
    _check_token(request, x_token)
    issues = validate_spec(spec)
    errors = [i for i in issues if i["level"] == "error"]
    if errors:
        raise HTTPException(422, {"issues": issues, "message": format_issues(issues)})
    jobs.init_jobs()
    job_id = jobs.submit_job(spec, _actor(x_actor_id, x_actor_name))
    return {"job_id": job_id, "warnings": [i for i in issues if i["level"] == "warn"]}


@router.get("/api/jobs")
def job_list(request: Request, x_token: str = Header(None)):
    _check_token(request, x_token)
    jobs.init_jobs()
    return jobs.list_jobs()


@router.get("/api/jobs/{job_id}")
def job_status(job_id: int, request: Request, x_token: str = Header(None)):
    _check_token(request, x_token)
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404)
    return job


@router.post("/api/runs/{run_id}/regenerate")
def web_regenerate(run_id: str, request: Request, x_token: str = Header(None),
                   x_actor_id: str = Header(None), x_actor_name: str = Header(None)):
    """从已有 run 再生一批（走队列，parent_run 溯源）。"""
    _check_token(request, x_token)
    row = store.db().execute(
        "SELECT spec FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        raise HTTPException(404, "run 不存在")
    spec = json.loads(row["spec"])
    spec["parent_run"] = run_id
    jobs.init_jobs()
    job_id = jobs.submit_job(spec, _actor(x_actor_id, x_actor_name))
    return {"job_id": job_id, "parent_run": run_id}


# ---------- 蒸馏与提案（D22） ----------
@router.post("/api/distill/run")
def api_distill(request: Request, x_token: str = Header(None),
                x_actor_id: str = Header(None), x_actor_name: str = Header(None)):
    """手动触发一轮蒸馏（worker 也会周期跑）。"""
    _check_token(request, x_token)
    from . import distill
    distill.init_distill()
    return distill.run_distill(actor=_actor(x_actor_id, x_actor_name))


@router.get("/api/proposals")
def api_proposals(status: str = None, request: Request = None,
                  x_token: str = Header(None)):
    _check_token(request, x_token)
    from . import distill
    distill.init_distill()
    return distill.list_proposals(status)


@router.post("/api/proposals/{pid}/approve")
def api_approve(pid: int, request: Request, x_token: str = Header(None),
                x_actor_id: str = Header(None), x_actor_name: str = Header(None)):
    _check_token(request, x_token)
    from . import distill
    try:
        return distill.approve(pid, actor=_actor(x_actor_id, x_actor_name))
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.post("/api/proposals/{pid}/reject")
def api_reject_proposal(pid: int, request: Request, body: dict = None,
                        x_token: str = Header(None),
                        x_actor_id: str = Header(None),
                        x_actor_name: str = Header(None)):
    _check_token(request, x_token)
    from . import distill
    try:
        return distill.reject_proposal(pid, actor=_actor(x_actor_id, x_actor_name),
                                       reason=(body or {}).get("reason", ""))
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.post("/api/proposals/{pid}/undo")
def api_undo(pid: int, request: Request, x_token: str = Header(None),
             x_actor_id: str = Header(None), x_actor_name: str = Header(None)):
    """撤销晋升（恢复提案 pending，从 derived 移除）。"""
    _check_token(request, x_token)
    from . import distill
    try:
        return distill.undo(pid, actor=_actor(x_actor_id, x_actor_name))
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.post("/api/proposals/{pid}/undo-reject")
def api_undo_reject(pid: int, request: Request, x_token: str = Header(None),
                    x_actor_id: str = Header(None), x_actor_name: str = Header(None)):
    _check_token(request, x_token)
    from . import distill
    try:
        return distill.undo_reject(pid, actor=_actor(x_actor_id, x_actor_name))
    except ValueError as e:
        raise HTTPException(422, str(e))


# ---------- 角色线（阶段7） ----------
@router.get("/api/characters/{name}/anchor")
def api_get_anchor(name: str, request: Request, x_token: str = Header(None)):
    _check_token(request, x_token)
    from . import character
    a = character.get_anchor(name)
    if not a:
        raise HTTPException(404, "该角色尚无锚点")
    return {"anchor": a, "lineage": character.anchor_lineage(name)}


@router.post("/api/characters/{name}/anchor")
def api_set_anchor(name: str, request: Request, body: dict,
                   x_token: str = Header(None),
                   x_actor_id: str = Header(None), x_actor_name: str = Header(None)):
    """body: {image_path, note?, from_candidate?, kind?}（image_path 相对仓库根）。"""
    _check_token(request, x_token)
    from . import character
    if not body.get("image_path"):
        raise HTTPException(422, "image_path 必填")
    aid = character.set_anchor(name, body["image_path"],
                               body.get("note", ""),
                               actor=_actor(x_actor_id, x_actor_name),
                               from_candidate=body.get("from_candidate"),
                               kind=body.get("kind", "character"))
    return {"anchor_id": aid}


# ---------- 风格锚（画风固化：参考图随 spec 直送生图模型） ----------
STYLE_ANCHOR_MAX = 12   # 锚名数上限（单次生图挂载仍限 3 张防稀释）


@router.get("/api/style-anchors")
def api_list_style_anchors(request: Request, x_token: str = Header(None)):
    """风格锚库（表单选择器数据源）：每风格名最新一张 + 张数 + 上限。"""
    _check_token(request, x_token)
    from . import character
    return {"anchors": character.list_style_anchors(),
            "max": STYLE_ANCHOR_MAX}


@router.post("/api/style-anchors/upload")
async def api_upload_style_anchor(request: Request,
                                  x_token: str = Header(None),
                                  x_actor_id: str = Header(None),
                                  x_actor_name: str = Header(None)):
    """上传本地图建风格锚。multipart/form-data: name, note?, file。
    任意分辨率（方舟对参考图自行缩放，无需与生图尺寸一致）；png/jpg/webp。
    同名追加（演进史保留）；新名受上限守卫。"""
    _check_token(request, x_token)
    from . import character
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        raise HTTPException(422, "风格锚名必填")
    upload = form.get("file")
    if upload is None or not hasattr(upload, "filename"):
        raise HTTPException(422, "file 必填（png/jpg/webp）")
    suffix = Path(upload.filename).suffix.lower()
    if suffix not in (".png", ".jpg", ".jpeg", ".webp"):
        raise HTTPException(422, f"不支持的格式：{suffix}（png/jpg/webp）")
    # 上限守卫：新名才计容量（同名追加不占新位）
    existing = {a["character"] for a in character.list_style_anchors()}
    if name not in existing and len(existing) >= STYLE_ANCHOR_MAX:
        raise HTTPException(422,
                            f"风格锚已达上限 {STYLE_ANCHOR_MAX} 个（删除旧锚或同名覆盖迭代）")
    dest_dir = cfg.root / "exports" / "_style_anchors"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{name}_{int(time.time())}{suffix}"
    content = await upload.read()
    if not content:
        raise HTTPException(422, "空文件")
    dest.write_bytes(content)
    rel = dest.relative_to(cfg.root).as_posix()
    aid = character.set_anchor(name, rel, form.get("note") or "本地上传",
                               actor=_actor(x_actor_id, x_actor_name),
                               kind="style")
    return {"anchor_id": aid, "image_path": rel}


@router.delete("/api/style-anchors/{name}")
def api_delete_style_anchor(name: str, request: Request,
                            anchor_id: int = None,
                            x_token: str = Header(None),
                            x_actor_id: str = Header(None),
                            x_actor_name: str = Header(None)):
    """删除风格锚。?anchor_id= 只删一条（同名的某次迭代）；缺省删该名全部。"""
    _check_token(request, x_token)
    from . import character
    deleted = character.delete_style_anchor(
        name, anchor_id=anchor_id, actor=_actor(x_actor_id, x_actor_name))
    if not deleted:
        raise HTTPException(404, f"风格锚「{name}」不存在")
    return {"deleted": deleted}


@router.get("/api/runs/{run_id}/consistency")
def api_consistency(run_id: str, request: Request, x_token: str = Header(None)):
    """run 候选 vs 角色锚点一致性报告（评审参考，非门禁）。"""
    _check_token(request, x_token)
    from . import character
    row = store.db().execute("SELECT subject FROM runs WHERE run_id=?",
                             (run_id,)).fetchone()
    if not row:
        raise HTTPException(404, "run 不存在")
    return {"subject": row["subject"],
            "reports": character.check_run_consistency(row["subject"], run_id)}


@router.post("/api/characters/{name}/pose-batch")
def api_pose_batch(name: str, request: Request, body: dict,
                   x_token: str = Header(None),
                   x_actor_id: str = Header(None),
                   x_actor_name: str = Header(None)):
    """7-B 动作批量：body={poses:["idle","attack"], count_each?:4, description?}。
    前置：角色须有锚点。数量守卫：poses≤8、count_each≤8 防误烧钱。"""
    _check_token(request, x_token)
    from . import character, jobs as jobs_mod
    poses = body.get("poses", [])
    count_each = body.get("count_each", 4)
    # ---- 守卫 ----
    if not poses or not isinstance(poses, list):
        raise HTTPException(422, "poses 必填且为数组")
    if len(poses) > 8:
        raise HTTPException(422, "单批动作不超过 8 个（防误烧钱）")
    if count_each > 8:
        raise HTTPException(422, "每动作候选不超过 8 张（防误烧钱）")
    pose_dict = cfg.character_poses
    bad = [p for p in poses if p not in pose_dict]
    if bad:
        raise HTTPException(422, f"未知动作：{bad}（现有：{sorted(pose_dict)}）")
    # ---- 锚点前置校验 ----
    if not character.get_anchor(name):
        raise HTTPException(404, f"角色 {name} 尚无锚点，先 set_anchor 再批量生成")
    # ---- 风格锚参考（画风固化：与角色锚合并挂参考图，上限守卫） ----
    style_refs = body.get("style_refs") or []
    if len(style_refs) > 3:
        raise HTTPException(422, "风格参考最多 3 张")
    # ---- 自定义生图 API（worker 执行前剥离，不落库） ----
    override = body.get("provider_override") or None
    if override and not override.get("api_key"):
        raise HTTPException(422, "自定义 API 缺少 api_key")
    # ---- 入队 ----
    spec = {"pose_batch": {
        "character": name,
        "poses": poses,
        "count_each": count_each,
        "description": body.get("description", ""),
        "style_refs": style_refs,
        "provider_override": override,
    }}
    jobs_mod.init_jobs()
    job_id = jobs_mod.submit_job(spec, _actor(x_actor_id, x_actor_name))
    return {"job_id": job_id, "poses": poses, "count_each": count_each}
