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
    from artcreate.tools.spec_validate import validate_spec, format_issues
    from artcreate.gates.lint import lint_spec, format_warnings
else:
    from .tools.config import get_config
    from .tools.compiler import compile_prompt
    from .tools.provider import generate
    from .tools.postprocess import process_run
    from .tools.spec_validate import validate_spec, format_issues
    from .gates.lint import lint_spec, format_warnings


def load_spec(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    issues = validate_spec(spec)
    if issues:
        print(format_issues(issues))
        if any(i["level"] == "error" for i in issues):
            raise SystemExit(f"spec 校验失败：{path}")
    return spec


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


def _execute_run(spec: dict, refs=None):
    """CLI 执行体代理 → pipeline.execute_run（与 web worker 共用一条链路）。"""
    from .pipeline import execute_run
    manifest = execute_run(spec, refs)
    if manifest:
        subject = manifest["subject"]
        print(f"挑选入库：python -m artcreate select {subject} {manifest['run_id']} <序号>")
    return manifest


def cmd_run(spec_path: str):
    spec_path = Path(spec_path)
    spec = load_spec(str(spec_path))
    refs = resolve_refs(spec, spec_path.parent)
    return _execute_run(spec, refs)


def cmd_regenerate(subject: str, run_id: str = None):
    """从库读原 run spec → 复制为新 run（同 revision，记 parent_run 溯源链）。"""
    import artcreate.store as store
    if not run_id:
        row = store.latest_run(subject)
        if not row:
            print(f"subject {subject} 无任何 run")
            return
        run_id = row["run_id"]
    run = store.db().execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if not run:
        print(f"run {run_id} 不在库中")
        return
    spec = json.loads(run["spec"])
    spec["parent_run"] = run_id          # 溯源链：新 run 可追到父
    refs = spec.get("ref_images") or None
    print(f"=== 再生：{subject} ← run {run_id}（revision {run['revision']} 不变）===")
    return _execute_run(spec, refs)


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


def cmd_gate(subject: str, run_id: str = None):
    import artcreate.store as store
    from artcreate.gates.mechanical import gate_image
    cfg = get_config()
    if not run_id:
        row = store.latest_run(subject)
        if not row:
            print(f"subject {subject} 无任何 run")
            return
        run_id = row["run_id"]
    run = store.db().execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if not run:
        print(f"run {run_id} 不在库中")
        return
    art_style = json.loads(run["spec"]).get("art_style", cfg.defaults["art_style"])
    print(f"=== 机械门禁 run={run_id}（画风 {art_style}）===")
    rows = [r for r in store.list_candidates(subject=subject) if r["run_id"] == run_id]
    for r in rows:
        if r["status"] in ("accepted", "rejected"):
            print(f"  #{r['idx']} 跳过（已 {r['status']}）")
            continue
        img = cfg.root / "exports" / subject / run_id / r["file"]
        report = gate_image(img, run["size"], art_style)
        passed = report["pass"]
        store.set_status(run_id, r["idx"], "gated" if passed else "rejected",
                         reject_code=report["reject_code"], gate_report=report)
        mark = "PASS" if passed else f"FAIL({report['reject_code']})"
        print(f"  #{r['idx']}: {mark}")
        for c in report["checks"]:
            if not c["pass"] or True:
                print(f"      {c['name']}: {'ok' if c['pass'] else 'FAIL'} — {c['detail']}")
    print("门禁报告已落库（gate 命令完成）")


def cmd_diagnose(subject: str, run_id: str = None, idx: int = None):
    """L2 编译自检 + L3 意图回查组合诊断（D18：诊断必须带一键修复建议）。"""
    import artcreate.store as store
    from artcreate.gates.audit import audit_compilation, audit_intent
    cfg = get_config()
    if not run_id:
        row = store.latest_run(subject)
        if not row:
            print(f"subject {subject} 无任何 run")
            return
        run_id = row["run_id"]
    run = store.db().execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if not run:
        print(f"run {run_id} 不在库中")
        return
    spec = json.loads(run["spec"])

    print(f"=== L2 编译自检（run={run_id}）===")
    l2 = audit_compilation(spec, run["prompt"],
                           segments=json.loads(run["segments"]))
    if l2["verdict"] == "pass":
        print("  通过：编译产物忠实覆盖全部表单项，无越权内容")
    else:
        print(f"  verdict: {l2['verdict']}")
        for item in l2["coverage"]:
            print(f"  ⚠️ 未覆盖/误译：{item}")
            print("     修复：检查编译器对该字段的处理，或改写该项表述后重编译")
        for item in l2["intrusion"]:
            print(f"  ⚠️ 越权引入：{item}")
            print("     修复：这是 LLM 扩展词带入的，考虑加约束轴排除或收紧描述")

    if idx is None:
        print("\n（指定候选序号可继续 L3 意图回查：diagnose subject run_id <序号>）")
        return

    print(f"\n=== L3 意图回查（候选 #{idx}）===")
    row = store.db().execute(
        "SELECT file FROM candidates WHERE run_id=? AND idx=?",
        (run_id, idx)).fetchone()
    if not row:
        print(f"候选 #{idx} 不存在")
        return
    img = cfg.root / "exports" / subject / run_id / row["file"]
    l3 = audit_intent(img, spec)
    for r in l3["results"]:
        if r["ok"] is None:
            print(f"  ? {r['source']}: 查询失败（{r['answer'][:40]}）")
        elif r["ok"]:
            print(f"  ✓ {r['source']}")
        else:
            print(f"  ✗ {r['source']} — 违反：{r['question']}")
            print(f"    回答：{r['answer']}")
    if l3["violation_rate"] is not None:
        rate = l3["violation_rate"]
        print(f"\n  违反率：{rate:.0%}")
        if rate >= 0.5:
            print("  ⛔ 高违反率。大概率原因：负向注入或编译丢失。")
            print("     修复：①检查约束写法（改用约束轴/正向表述，L1 lint 会拦否定式）")
            print("          ②L2 的 coverage 结果对照是否有约束被编译丢失")
        elif rate > 0:
            print("  ⚠️ 部分违反。可对比其他候选或再生成一批。")
        else:
            print("  ✓ 全部约束满足。")
    # 诊断结果落库
    store.db().execute(
        "UPDATE candidates SET gate_report=?" 
        " WHERE run_id=? AND idx=?",
        (json.dumps({"l2": l2, "l3": l3}, ensure_ascii=False), run_id, idx))
    store.db().commit()


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

    p = sub.add_parser("gate")
    p.add_argument("subject")
    p.add_argument("run_id", nargs="?", default=None)

    p = sub.add_parser("diagnose")
    p.add_argument("subject")
    p.add_argument("run_id", nargs="?", default=None)
    p.add_argument("idx", nargs="?", type=int, default=None)

    p = sub.add_parser("stats")
    p.add_argument("--subject", "-s", default=None)

    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, default=8870)

    p = sub.add_parser("regenerate")
    p.add_argument("subject")
    p.add_argument("run_id", nargs="?", default=None)

    p = sub.add_parser("distill")
    p.add_argument("--min-count", type=int, default=3)
    p.add_argument("--no-llm", action="store_true",
                   help="跳过 LLM 判定（调试 harvest/聚类）")

    p = sub.add_parser("proposals")
    p.add_argument("--status", "-s", default=None,
                   choices=["pending", "promoted", "rejected"])

    p = sub.add_parser("promote")
    p.add_argument("pid", type=int)

    p = sub.add_parser("demote")
    p.add_argument("pid", type=int)

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
    elif args.cmd == "gate":
        cmd_gate(args.subject, args.run_id)
    elif args.cmd == "diagnose":
        cmd_diagnose(args.subject, args.run_id, args.idx)
    elif args.cmd == "stats":
        from artcreate.gates.stats import format_stats
        print(format_stats(args.subject))
    elif args.cmd == "serve":
        from artcreate.server import serve
        print(f"评审视图：http://127.0.0.1:{args.port}")
        serve(args.port)
    elif args.cmd == "regenerate":
        cmd_regenerate(args.subject, args.run_id)
    elif args.cmd == "distill":
        from artcreate.distill import run_distill
        stats = run_distill(min_count=args.min_count, use_llm=not args.no_llm)
        print(f"蒸馏完成：采样 {stats['harvested']} · LLM 判定 {stats['judged']}"
              f" · 新提案 {stats['new_proposals']}")
        for text, why in stats["skipped"].items():
            print(f"  跳过「{text[:30]}」：{why}")
    elif args.cmd == "proposals":
        from artcreate.distill import list_proposals
        rows = list_proposals(args.status)
        if not rows:
            print("（无提案）")
        for p in rows:
            print(f"#{p['id']} [{p['status']}] {p['axis_id']} ← "
                  f"“{p['sample_text'][:30]}” ×{p['count']}"
                  f"（置信 {p['confidence']}）")
    elif args.cmd == "promote":
        from artcreate.distill import approve
        r = approve(args.pid)
        print(f"已晋升 → {r['derived']}"
              + (f"（git {r['git_commit']}）" if r.get("git_commit") else "（无 git 提交）"))
    elif args.cmd == "demote":
        from artcreate.distill import undo
        r = undo(args.pid)
        print(f"已撤销晋升" + (f"（git {r['git_commit']}）" if r.get("git_commit") else ""))


if __name__ == "__main__":
    main()
