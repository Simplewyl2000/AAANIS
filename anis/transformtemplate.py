def get(args, binding, engine):
    return {
        "status": "ok",
        "target": binding.get("target_desc", "image"),
        "property": "snapshot",
        "current": engine.measure(args),
    }


def run(args, binding, engine):
    engine.transform_run(args, binding)
    return {
        "status": "ok",
        "target": binding.get("target_desc", "image"),
        "action": binding.get("plan_text") or binding["proc"],
    }
