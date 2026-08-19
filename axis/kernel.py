"""AXIS 内核：固定运行时，人工维护，Builder 不得改动。

Builder 为每条命令提供 spec.json + impl.py（一个 run 函数）。内核只做三件事：
按 spec 校验参数、调用命令、原样输出命令的执行报告。读取当前状态是独立的
getter/observe 命令，不再暗藏在写命令前后。

输出信封：stdout 只放一个 JSON，诊断走 stderr。
退出码：0 成功；2 参数错误；3 命令内声明的错误；4 实现缺陷。
"""
import importlib.util
import json
import os
import sys

EXIT_OK = 0
EXIT_BAD_ARGS = 2
EXIT_CMD_ERROR = 3
EXIT_UNEXPECTED = 4

_TRUE_VALUES = {"1", "true", "yes"}
_FALSE_VALUES = {"0", "false", "no"}


class AxisError(Exception):
    """命令实现里的可恢复错误：code 必须在 spec.errors 里声明，fix 告诉 agent 下一步做什么。"""

    def __init__(self, code, message, fix):
        super().__init__(message)
        self.code = code
        self.fix = fix


def emit(payload, exit_code):
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    sys.exit(exit_code)


def _error(e, exit_code):
    emit({"status": "error", "code": e.code, "message": str(e), "fix": e.fix}, exit_code)


def _coerce(name, aspec, value):
    t = aspec.get("type", "string")
    try:
        if t == "int":
            value = int(value)
        elif t == "float":
            value = float(value)
        elif t == "bool":
            if isinstance(value, bool):
                pass
            else:
                normalized = str(value).strip().lower()
                if normalized in _TRUE_VALUES:
                    value = True
                elif normalized in _FALSE_VALUES:
                    value = False
                else:
                    raise ValueError
        elif t == "vec":
            parts = (value if isinstance(value, list)
                     else [p for p in str(value).strip().strip("[]").split(",")])
            elem = aspec.get("elem", "float")
            cast = {"float": float, "int": int,
                    "bool": lambda p: str(p).strip().lower() in ("1", "true", "yes")}[elem]
            value = [cast(p) for p in parts]
    except ValueError:
        raise AxisError("INVALID_ARGS", f"参数 --{name} 需要 {t}，收到 {value!r}",
                        "加 --schema 查看参数类型")
    if t == "vec":
        size = aspec.get("size")
        if size is not None and len(value) != size:
            raise AxisError("INVALID_ARGS",
                            f"--{name} 需要 {size} 个分量，收到 {len(value)} 个",
                            "逗号分隔，如 --value 1,2,3,4")
    if t == "enum" and value not in aspec.get("values", []):
        raise AxisError("INVALID_ARGS", f"--{name} 取值 {value!r} 不合法，可选 {aspec.get('values')}",
                        "加 --schema 查看合法取值")
    if t in ("int", "float"):
        lo, hi = aspec.get("min"), aspec.get("max")
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            raise AxisError("INVALID_ARGS", f"--{name}={value} 超出范围 [{lo}, {hi}]",
                            "加 --schema 查看取值范围")
    return value


def cli_value(value):
    """Encode one JSON-compatible value using the public AXIS CLI contract."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def argument_tokens(values, include=None):
    """Encode command parameters; every parameter, including bool, has a value."""
    allowed = set(include) if include is not None else None
    tokens = []
    for name, value in values.items():
        if value is None or (allowed is not None and name not in allowed):
            continue
        tokens.extend([f"--{name.replace('_', '-')}", cli_value(value)])
    return tokens


def validate(spec, raw):
    known = spec.get("args", {})
    for k in raw:
        if k not in known:
            raise AxisError("INVALID_ARGS", f"未知参数 --{k}", "加 --schema 查看参数表")
    args = {}
    for name, aspec in known.items():
        if name in raw:
            args[name] = _coerce(name, aspec, raw[name])
        elif "default" in aspec:
            args[name] = aspec["default"]
        elif aspec.get("required"):
            raise AxisError("INVALID_ARGS", f"缺少必填参数 --{name}", "加 --schema 查看参数表")
        else:
            args[name] = None
    return args


def load_spec(cmd_dir):
    with open(os.path.join(cmd_dir, "spec.json"), encoding="utf-8") as f:
        return json.load(f)


def load_impl(cmd_dir):
    path = os.path.join(cmd_dir, "impl.py")
    mod_spec = importlib.util.spec_from_file_location("axis_impl", path)
    mod = importlib.util.module_from_spec(mod_spec)
    mod_spec.loader.exec_module(mod)
    if not callable(getattr(mod, "run", None)):
        raise RuntimeError("impl.py 缺少 run()，内核拒绝注册该命令")
    return mod


def run(cmd_dir, raw_args):
    spec = load_spec(cmd_dir)
    try:
        args = validate(spec, raw_args)                      # 第 1 步
    except AxisError as e:
        _error(e, EXIT_BAD_ARGS)
    try:
        impl = load_impl(cmd_dir)
        report = impl.run(args)                              # 第 2 步
        if not isinstance(report, dict):
            raise TypeError(f"run() 必须返回 JSON 对象，收到 {type(report).__name__}")
        emit(report, EXIT_OK)                                # 第 3 步
    except AxisError as e:
        _error(e, EXIT_CMD_ERROR)
    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        emit({"status": "error", "code": "UNEXPECTED",
              "message": f"{type(e).__name__}: {e}",
              "fix": "这是命令实现的缺陷。产物状态未验证，请勿假定操作成功；可用 observe 查看当前状态。"},
             EXIT_UNEXPECTED)
