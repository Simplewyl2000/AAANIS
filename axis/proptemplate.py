"""属性绑定模板：属性写命令和独立 getter 命令共用的胶水。

设计（METHOD.md 转向一）：命令的 impl.py 不是"一段实现"，而是"普查项 → 命令
的绑定"。模板把读写逻辑集中在这里，impl.py 只携带 BINDING 数据。
引擎适配层（apps/<app>/engine.py）只需实现三个函数：

  engine.get_raw(args, binding)        -> 当前原始值（选择器未命中抛 AxisError）
  engine.set_raw(args, binding, raw)   -> 写入原始值
  engine.describe_target(args, binding) -> str，目标的人类可读描述

值的归一化/反归一化由本模板负责（engine 只过手原始值），保证"同效果的
不同词汇在命令层塌成一格"（如 CharWeight=150 与 bold=true）。
"""


def normalize(raw, binding):
    """引擎原始值 -> 命令层可比值。"""
    vt = binding["value"]["type"]
    if raw is None:
        return None
    if vt == "bool":
        return raw > binding["value"].get("threshold", 0)
    if vt == "color":
        return "(自动)" if raw in (-1, None) else f"#{int(raw) & 0xFFFFFF:06X}"
    if vt == "enum":
        names = binding["value"].get("names", {})
        if not names:
            return raw  # 引擎直接给字符串枚举：恒等映射
        try:
            key = str(int(raw))
        except (TypeError, ValueError):
            key = str(raw)  # 引擎枚举对象已转成名称字符串
        return names.get(key, str(raw))
    if vt in ("vec3", "vec"):
        return list(raw)
    return raw


def denormalize(desired, binding):
    """命令层目标值 -> 引擎原始值。"""
    v = binding["value"]
    vt = v["type"]
    if vt == "bool":
        return v.get("true", True) if desired else v.get("false", False)
    if vt == "color":
        return int(desired.lstrip("#"), 16)
    if vt == "enum":
        names = v.get("names", {})
        if not names:
            return desired  # 字符串枚举：恒等映射
        for k, name in names.items():
            if name == desired:
                try:
                    return int(k)
                except ValueError:
                    return k  # 字符串键：引擎侧负责还原成枚举对象
        raise ValueError(f"枚举值 {desired!r} 不在 names 表里")
    if vt in ("vec3", "vec"):
        return tuple(desired)
    return desired


def desired_of(args, binding):
    if binding["value"]["type"] == "vec3":
        return [args["x"], args["y"], args["z"]]
    return args["value"]

def get(args, binding, engine):
    """读取并报告当前值，不修改产物。"""
    raw = engine.get_raw(args, binding)  # 选择器未命中时 engine 抛 AxisError
    return {"status": "ok",
            "target": engine.describe_target(args, binding),
            "property": binding["path"],
            "current": normalize(raw, binding)}


def run(args, binding, engine):
    """写入目标值并报告做了什么；不在命令内部读取前态或后态。"""
    desired = desired_of(args, binding)
    engine.set_raw(args, binding, denormalize(desired, binding))
    return {"status": "ok",
            "target": engine.describe_target(args, binding),
            "property": binding["path"],
            "value": desired}
