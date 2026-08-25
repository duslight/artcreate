"""artcreate · CLI 入口

用法：
  python -m artcreate run specs/xxx.yaml          # 跑一次完整链路
  python -m artcreate compile specs/xxx.yaml      # 只编译不生成（调试用）
  python -m artcreate lint specs/xxx.yaml         # 只跑 L1 lint
"""
import argparse
import datetime
import json
import sys
import uuid
from pathlib import Path

import yaml

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from artcreate.tools.config import get_config
    from artcreate.tools.compiler import compile_prompt
    from artcreate.tools.provider import generate
    from artcreate.tools.postprocess import process_run
    from artcreate.gates.lint import lint_spec, format_warnings
else:
    from .tools.config import get_config
    from .tools.compiler import compile_prompt
    from .tools.provider import generate
    from .tools.postprocess import process_run
    from .gates.lint import lint_spec, format_warnings


def load_spec(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_refs(spec: dict, spec_dir: Path):
    """spec 里 ref_images 相对路径 → 基于 spec 文件目录的绝对路径。"""
    refs = spec.get("ref_images")
    if not refs:
        return None
    if isinstance(refs, str):
        refs = [refs]
    out = []
    for r in refs:
        p = Path(r)
        out.append(str(p if p.is_absolute() else spec_dir / p))
    return out


def cmd_compile(spec_path: str):
    spec = load_spec(spec_path)
    result = compile_prompt(spec)
    print("=== 编译产物 ===")
    print(result["prompt"])
    print("\n=== 分段（segments，供消融/溯源）===")
    for k, v in result["segments"].items():
        print(f"  {k}: {v}")
    return result


def cmd_lint(spec_path: str):
    spec = load_spec(spec_path)
    result = compile_prompt(spec)
    warnings = lint_spec(spec, result["prompt"])
    print(format_warnings(warnings))
    return warnings


def cmd_run(spec_path: str):
    cfg = get_config()
    spec_path = Path(spec_path)
    spec = load_spec(str(spec_path))
    d = cfg.defaults
    size = spec.get("size", d["size"])
    count = spec.get("count", d["count"])
    refs = resolve_refs(spec, spec_path.parent)
    cfg.validate_size(size, with_ref=bool(refs))

    # 1. 编译
    result = compile_prompt(spec)
    print("=== 编译产物 ===")
    print(result["prompt"][:300] + ("..." if len(result["prompt"]) > 300 else ""))

    # 2. L1 lint（block 级警告中止）
    warnings = lint_spec(spec, result["prompt"])
    print("\n=== L1 lint ===")
    print(format_warnings(warnings))
    if any(w["level"] == "block" for w in warnings):
        print("\n⛔ 存在 block 级问题，链路中止（未产生任何费用）")
        return None

    # 3. 生成
    print(f"\n=== 生成（provider={cfg.active_provider}, count={count}）===")
    raw_urls = generate(result["prompt"], size, count, ref_images=refs)
    print(f"获得 {len(raw_urls)} 张候选")

    # 4. 后处理 + 资产契约落盘
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
    manifest_path = out_dir / "manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"\n=== 完成 ===")
    print(f"资产目录：{out_dir}")
    print(f"manifest：{manifest_path}")
    return manifest


def main():
    ap = argparse.ArgumentParser(prog="artcreate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "compile", "lint"):
        p = sub.add_parser(name)
        p.add_argument("spec", help="spec yaml 路径")
    args = ap.parse_args()
    if args.cmd == "run":
        cmd_run(args.spec)
    elif args.cmd == "compile":
        cmd_compile(args.spec)
    else:
        cmd_lint(args.spec)


if __name__ == "__main__":
    main()
