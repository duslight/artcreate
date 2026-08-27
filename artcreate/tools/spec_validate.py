"""artcreate · spec 校验层（3.5-D）：未知字段拦截 + 枚举值合法性

两种模式：
- lenient（默认）：未知字段/未知枚举 → warning（不阻断，兼容旧 spec 与快速试验）
- strict：未知顶层字段 → error（阻断）；未知枚举仍为 warning（枚举可来自 derived，动态性高）

为什么需要：字典开放扩展（derived 晋升选项）后，spec 写错字段名（如 axis_sel
拼错、约束轴 id 写错）会静默失效——用户以为选了约束，实际编译器根本没读到。
校验层把"静默失效"变成"显式报错"。
"""
from .config import get_config

# spec 顶层合法字段（与 compiler/__main__ 消费侧对齐维护）
TOP_FIELDS = {
    "subject", "revision", "description", "extra_prompt",
    "asset_type", "mood", "art_style", "size", "count",
    "ref_images", "constraints", "parent_run", "character",
    "asset_name", "asset_suffix_key",
    "compiled_prompt", "compiled_segments",   # 审核直通（位置A：所见即所执行）
    "prompt_reused", "compiled_prompt_zh",    # 历史编译词复用（精修底稿）
    "provider_override",                     # 自定义生图 API（worker 执行前剥离，不落库）
    "ref_role_note",                         # 三槽位参考图分工说明句（编译时注入 prompt 尾部）
}
CONSTRAINTS_FIELDS = {"axis_sel", "free_text", "free_text_negative"}


def validate_spec(spec: dict, strict: bool = False) -> list:
    """返回 issues 列表：[{level: warn|error, field, message}]。strict 下未知字段升 error。"""
    cfg = get_config()
    issues = []

    # 1. 未知顶层字段（typo 主灾区）
    for key in spec:
        if key not in TOP_FIELDS:
            level = "error" if strict else "warn"
            issues.append({
                "level": level, "field": key,
                "message": f"未知字段 '{key}'（合法字段：{sorted(TOP_FIELDS)}），"
                           f"将被静默忽略"})

    # 2. constraints 子字段
    cons = spec.get("constraints") or {}
    if not isinstance(cons, dict):
        issues.append({"level": "error", "field": "constraints",
                       "message": f"constraints 应为字典，实际 {type(cons).__name__}"})
    else:
        for key in cons:
            if key not in CONSTRAINTS_FIELDS:
                level = "error" if strict else "warn"
                issues.append({
                    "level": level, "field": f"constraints.{key}",
                    "message": f"未知约束字段 '{key}'（合法：{sorted(CONSTRAINTS_FIELDS)}），"
                               f"将被静默忽略"})

        # 3. 轴 id / 选项 id 合法性（写错 = 约束静默失效，必须显式暴露）
        axis_sel = cons.get("axis_sel") or {}
        axes = {a["id"]: a for a in cfg.constraint_axes}
        for axis_id, opt_id in axis_sel.items():
            if axis_id not in axes:
                issues.append({
                    "level": "error", "field": f"constraints.axis_sel.{axis_id}",
                    "message": f"未知约束轴 '{axis_id}'（现有轴：{sorted(axes)}），"
                               f"该约束已静默失效"})
                continue
            valid_opts = [o["id"] for o in axes[axis_id].get("options", [])]
            if opt_id not in valid_opts:
                issues.append({
                    "level": "error", "field": f"constraints.axis_sel.{axis_id}",
                    "message": f"轴 '{axis_id}' 无选项 '{opt_id}'"
                               f"（合法选项：{valid_opts}），该约束已静默失效"})

    # 4. 类型粗检（高频手误：count 写字符串数字、revision 非整数）
    for field, want in (("count", int), ("revision", int)):
        v = spec.get(field)
        if v is not None and not isinstance(v, int):
            issues.append({
                "level": "error", "field": field,
                "message": f"{field} 应为整数，实际 {type(v).__name__}：{v!r}"})
    if spec.get("description") is not None and \
            not isinstance(spec["description"], str):
        issues.append({"level": "error", "field": "description",
                       "message": f"description 应为字符串，实际 "
                                  f"{type(spec['description']).__name__}"})

    # 5. character 子结构（7-B：pose 写错 = 动作词静默失效，同轴选错原则必须显式拦）
    ch = spec.get("character")
    if ch is not None:
        if not isinstance(ch, dict):
            issues.append({"level": "error", "field": "character",
                           "message": f"character 应为字典，实际 {type(ch).__name__}"})
        else:
            for key in ch:
                if key not in ("anchor", "sheet", "pose"):
                    issues.append({
                        "level": "warn", "field": f"character.{key}",
                        "message": f"未知 character 子字段 '{key}'，将被静默忽略"})
            pose = ch.get("pose")
            if pose is not None:
                poses = cfg.character_poses or {}
                if pose not in poses:
                    issues.append({
                        "level": "error", "field": "character.pose",
                        "message": f"未知动作 '{pose}'（现有动作：{sorted(poses)}），"
                                   f"动作词已静默失效"})

    # 6. 资产命名（拍板发布 exports/final/ 用；格式错误会导致发布失败，error 拦）
    name = spec.get("asset_name")
    if name is not None:
        if not isinstance(name, str) or not name.strip():
            issues.append({"level": "error", "field": "asset_name",
                           "message": "asset_name 应为非空字符串"})
        elif not all(c.isalnum() or c in "-_" for c in name):
            issues.append({
                "level": "error", "field": "asset_name",
                "message": f"asset_name '{name}' 含非法字符（只允许字母/数字/-/_，"
                           f"避免路径穿越与跨平台文件名问题）"})

    return issues


def format_issues(issues: list) -> str:
    if not issues:
        return "spec 校验通过，无问题"
    lines = []
    for i in issues:
        mark = "⛔" if i["level"] == "error" else "⚠️"
        lines.append(f"{mark} [{i['field']}] {i['message']}")
    return "\n".join(lines)
