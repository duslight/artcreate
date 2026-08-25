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


def execute_run(spec: dict, refs=None, actor: dict = None):
    """run 与 regenerate 共用的执行体。refs 已解析为路径/URL 列表或 None。
    返回 manifest；block 级 lint 失败返回 None（未产生费用）。"""
    cfg = get_config()
    d = cfg.defaults
    size = spec.get("size", d["size"])
    count = spec.get("count", d["count"])
    cfg.validate_size(size, with_ref=bool(refs))

    result = compile_prompt(spec)
    print("=== 编译产物 ===")
    print(result["prompt"][:300] + ("..." if len(result["prompt"]) > 300 else ""))

    warnings = lint_spec(spec, result["prompt"])
    print("\n=== L1 lint ===")
    print(format_warnings(warnings))
    if any(w["level"] == "block" for w in warnings):
        print("\n⛔ 存在 block 级问题，链路中止（未产生任何费用）")
        return None

    print(f"\n=== 生成（provider={cfg.active_provider}, count={count}）===")
    raw_urls = generate(result["prompt"], size, count, ref_images=refs)
    print(f"获得 {len(raw_urls)} 张候选")

    run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    subject = spec.get("subject", "unnamed")
    out_dir = cfg.root / "exports" / subject / run_id
    post_meta = process_run(raw_urls, out_dir)

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
