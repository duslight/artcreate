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

    # 5. 注册进资产库（阶段2）
    import artcreate.store as store
    store.register_run(manifest)

    print(f"\n=== 完成 ===")
    print(f"资产目录：{out_dir}")
    print(f"manifest：{manifest_path}")
    print(f"库注册：{len(post_meta)} 候选（run_id={run_id}）")
    print(f"挑选入库：python -m artcreate select {subject} {run_id} <序号>")
    return manifest


def cmd_list(subject=None, status=None):
    import artcreate.store as store
    rows = store.list_candidates(subject=subject, status=status)
    if not rows:
        print("（无记录）")
        return
    print(f"{'run_id':28} {'idx':>3} {'subject':20} {'rev':>3} {'status':10} {'reject':16} file")
    for r in rows:
        print(f"{r['run_id']:28} {r['idx']:>3} {r['subject']:20} {r['revision']:>3}"
              f" {r['status']:10} {r['reject_code'] or '-':16} {r['file']}")


def cmd_select(subject: str, run_id: str = None, idx: int = None):
    """挑选候选入库。缺省 run_id 取该 subject 最新 run；缺省 idx 需显式给出。"""
    import artcreate.store as store
    if not run_id:
        row = store.latest_run(subject)
        if not row:
            print(f"subject {subject} 无任何 run")
            return
        run_id = row["run_id"]
    if idx is None:
        # 展示该 run 的候选供挑选
        rows = store.list_candidates(subject=subject)
        rows = [r for r in rows if r["run_id"] == run_id]
        print(f"run {run_id} 候选（用 select {subject} {run_id} <序号> 挑选）：")
        for r in rows:
            print(f"  #{r['idx']}  {r['file']}  [{r['status']}]")
        return
    store.accept(run_id, idx)
    print(f"已接受：{subject} run={run_id} 候选#{idx}"
          f"（同 subject 同 revision 旧 accepted 已自动降级）")


def cmd_reject(subject: str, run_id: str, idx: int, code: str = "manual"):
    import artcreate.store as store
    store.reject(run_id, idx, code)
    print(f"已拒绝：{subject} run={run_id} 候选#{idx}（拒绝码 {code}）")


def main():
    ap = argparse.ArgumentParser(prog="artcreate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "compile", "lint"):
        p = sub.add_parser(name)
        p.add_argument("spec", help="spec yaml 路径")

    p = sub.add_parser("list")
    p.add_argument("--subject", "-s", default=None)
    p.add_argument("--status", "-t", default=None,
                   choices=["generated", "gated", "accepted", "rejected"])

    p = sub.add_parser("select")
    p.add_argument("subject")
    p.add_argument("run_id", nargs="?", default=None)
    p.add_argument("idx", nargs="?", type=int, default=None)

    p = sub.add_parser("reject")
    p.add_argument("subject")
    p.add_argument("run_id")
    p.add_argument("idx", type=int)
    p.add_argument("--code", default="manual")

    args = ap.parse_args()
    if args.cmd == "run":
        cmd_run(args.spec)
    elif args.cmd == "compile":
        cmd_compile(args.spec)
    elif args.cmd == "lint":
        cmd_lint(args.spec)
    elif args.cmd == "list":
        cmd_list(args.subject, args.status)
    elif args.cmd == "select":
        cmd_select(args.subject, args.run_id, args.idx)
    elif args.cmd == "reject":
        cmd_reject(args.subject, args.run_id, args.idx, args.code)


if __name__ == "__main__":
    main()
