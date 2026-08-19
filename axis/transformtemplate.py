"""一次性变换命令共用的执行和观测胶水。

run 只执行变换并报告动作，不读取前态或后态。get 单独拍状态快照，供外部验证
使用。这样旋转、反色、模糊等命令不会再暗藏读回和计划步骤。
"""


def get(args, binding, engine):
    """读取当前变换指标，不修改产物。"""
    return {
        "status": "ok",
        "target": binding.get("target_desc", "图像"),
        "property": "snapshot",
        "current": engine.measure(args),
    }


def run(args, binding, engine):
    """执行一次变换并报告动作。"""
    engine.transform_run(args, binding)
    return {
        "status": "ok",
        "target": binding.get("target_desc", "图像"),
        "action": binding.get("plan_text") or binding["proc"],
    }
