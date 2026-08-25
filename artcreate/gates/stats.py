"""artcreate · L4 跨批统计（阶段3-C，D18）：个案变信号

从库聚合 L3 意图回查历史：某约束的跨 run 违反率。
"过去 3 批 24 张图，'无水体'约束违反率 42%" → 不是运气差，是字典词组失效。
"""
import json

from ..store import db


def constraint_stats(subject: str = None):
    """按约束来源聚合违反率。返回 [{source, judged, violated, rate}]。"""
    sql = ("SELECT c.gate_report, r.subject FROM candidates c"
           " JOIN runs r ON c.run_id = r.run_id"
           " WHERE c.gate_report IS NOT NULL AND c.gate_report LIKE '%l3%'")
    args = []
    if subject:
        sql += " AND r.subject=?"
        args.append(subject)
    rows = db().execute(sql, args).fetchall()

    agg = {}  # source → [judged, violated]
    for r in rows:
        try:
            report = json.loads(r["gate_report"])
            l3 = report.get("l3") or {}
        except Exception:
            continue
        for item in l3.get("results", []):
            src = item.get("source")
            if not src or item.get("ok") is None:
                continue
            j, v = agg.get(src, (0, 0))
            agg[src] = (j + 1, v + (0 if item["ok"] else 1))

    out = [{"source": s, "judged": j, "violated": v,
            "rate": (v / j) if j else 0.0}
           for s, (j, v) in agg.items()]
    out.sort(key=lambda x: -x["rate"])
    return out


def format_stats(subject: str = None) -> str:
    stats = constraint_stats(subject)
    if not stats:
        return "（暂无 L3 回查历史——跑 diagnose 带 idx 后这里会有数据）"
    lines = [f"{'约束来源':32} {'判定':>4} {'违反':>4} {'违反率':>6} 信号"]
    for s in stats:
        level = ("⛔ 失效词组" if s["rate"] >= 0.5 else
                 "⚠️ 不稳定" if s["rate"] >= 0.25 else "✓ 健康")
        lines.append(f"{s['source']:32} {s['judged']:>4} {s['violated']:>4}"
                     f" {s['rate']:>6.0%}  {level}")
    return "\n".join(lines)
