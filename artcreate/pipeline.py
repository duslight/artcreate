"""artcreate · 生成管线执行体（CLI 与 web worker 共用）

编译 → L1 lint → 生成 → 后处理 → manifest → 库注册，一条龙。
actor 透传 register_run 进入事件日志（谁发起的这次 run）。
"""
import datetime
import json
import uuid
from pathlib import Path

from .tools.config import get_config
from .tools.compiler import compile_prompt
from .tools.provider import generate
from .tools.postprocess import process_run
from .gates.lint import lint_spec, format_warnings


def execute_run(spec: dict, refs=None, actor: dict = None,
                provider_override: dict = None):
    """run 与 regenerate 共用的执行体。refs 已解析为路径/URL 列表或 None。
    provider_override: 自定义生图 API 配置（不落库——spec 入 store 前由
    调用方剥离）。返回 manifest；block 级 lint 失败返回 None（未产生费用）。"""
    cfg = get_config()
    d = cfg.defaults
    size = spec.get("size", d["size"])
    count = spec.get("count", d["count"])
    cfg.validate_size(size, with_ref=bool(refs))

    # 编译：审核直通（spec 带 compiled_prompt=用户已在中英审核卡确认过的编译结果，
    # 跳过重编译杜绝"审核的和执行的不一致"——LLM 重新编译有随机性）
    if spec.get("compiled_prompt"):
        result = {"prompt": spec["compiled_prompt"],
                  "segments": spec.get("compiled_segments") or {}}
        print("=== 编译产物（审核直通，跳过重编译）===")
        print(result["prompt"][:300] + ("..." if len(result["prompt"]) > 300 else ""))
    else:
        result = compile_prompt(spec)
        print("=== 编译产物 ===")
        print(result["prompt"][:300] + ("..." if len(result["prompt"]) > 300 else ""))

    warnings = lint_spec(spec, result["prompt"])
    print("\n=== L1 lint ===")
    print(format_warnings(warnings))
    if any(w["level"] == "block" for w in warnings):
        print("\n⛔ 存在 block 级问题，链路中止（未产生任何费用）")
        return None

    print(f"\n=== 生成（provider={cfg.active_provider}"
          f"{' +自定义API' if provider_override else ''}, count={count}）===")
    raw_urls = generate(result["prompt"], size, count, ref_images=refs,
                        override=provider_override)
    print(f"获得 {len(raw_urls)} 张候选")

    run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    subject = spec.get("subject", "unnamed")
    out_dir = cfg.root / "exports" / subject / run_id
    post_meta = process_run(raw_urls, out_dir,
                            asset_type=spec.get("asset_type", ""))

    manifest = {
        "schema": 1,
        "run_id": run_id,
        "subject": subject,
        "spec_revision": spec.get("revision", 1),
        "spec": spec,
        "prompt": result["prompt"],
        "prompt_segments": result["segments"],
        "lint_warnings": warnings,
        "provider": cfg.active_provider,
        "model": cfg.provider_conf().get("model"),
        "size": size,
        "count": count,
        "ref_images": refs,
        "candidates": [
            {"index": m["index"], "file": f"out_{m['index']}.png",
             "w": m["w"], "h": m["h"],
             "grid_w": m["grid_w"], "grid_h": m["grid_h"],
             "status": "generated"}
            for m in post_meta
        ],
    }
    if spec.get("parent_run"):
        manifest["parent_run"] = spec["parent_run"]

    manifest_path = out_dir / "manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    import artcreate.store as store
    store.register_run(manifest, actor=actor)

    print(f"\n=== 完成 ===")
    print(f"资产目录：{out_dir}")
    print(f"manifest：{manifest_path}")
    print(f"库注册：{len(post_meta)} 候选（run_id={run_id}）")
    if spec.get("parent_run"):
        print(f"父 run：{spec['parent_run']}（再生来源）")
    return manifest


def execute_pose_batch(character: str, poses: list, description: str = "",
                       count_each: int = 4, style_refs: list = None,
                       provider_override: dict = None,
                       actor: dict = None, on_progress=None) -> dict:
    """7-B 动作批量执行体：逐 pose 生成 + 自动一致性挂载。

    poses：动作 id 列表。on_progress(pose, i, total, run_id) 供 worker
    更新 job progress。style_refs：风格锚路径列表（与角色锚合并挂参考图，
    角色锚在前保形象权重）。返回 {runs: [{pose, run_id, candidates, consistency}]}。
    单 pose 失败不中断整批（记 error 继续），block 级 lint 失败同理。
    """
    from .character import pose_batch_spec, attach_consistency
    items = pose_batch_spec(character, poses, description, count_each,
                            style_refs=style_refs)
    total = len(items)
    runs = []
    for i, item in enumerate(items, 1):
        pose, spec = item["pose"], item["spec"]
        if on_progress:
            on_progress(pose, i, total)
        print(f"\n=== 动作 {i}/{total}：{pose} ===")
        try:
            manifest = execute_run(spec, refs=spec.get("ref_images"),
                                   actor=actor,
                                   provider_override=provider_override)
        except Exception as e:
            print(f"动作 {pose} 执行失败：{e}")
            runs.append({"pose": pose, "run_id": None, "error": str(e)})
            continue
        if manifest is None:
            runs.append({"pose": pose, "run_id": None,
                         "error": "block 级 lint 未通过"})
            continue
        consistency = attach_consistency(character, manifest["run_id"])
        runs.append({"pose": pose, "run_id": manifest["run_id"],
                     "candidates": manifest["candidates"],
                     "consistency": consistency})
    from .store import log_event
    log_event("pose_batch", actor, target_type="character",
              target_id=character,
              detail={"poses": [r["pose"] for r in runs],
                      "run_ids": [r.get("run_id") for r in runs]})
    return {"character": character, "runs": runs}
