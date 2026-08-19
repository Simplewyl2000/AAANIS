"""动作绑定模板：processing 和显式动词命令共用的执行胶水。

动作命令只负责执行并返回“做了什么”的报告；状态检查由独立观测命令和
验证脚本完成。引擎适配层实现 action_run；可观测的动作另实现 action_state。
"""


def get(args, binding, engine):
    """读取动作产出的当前事实，不修改产物。"""
    state = engine.action_state(args, binding)
    return {"status": "ok", **state}


def run(args, binding, engine):
    """执行动作一次，并把引擎给出的细节并入报告。"""
    result = engine.action_run(args, binding)
    report = {
        "status": "ok",
        "action": binding.get(
            "action", binding.get("algorithm", binding.get("procedure", "action"))),
    }
    if isinstance(result, dict):
        report.update(result)
    return report
