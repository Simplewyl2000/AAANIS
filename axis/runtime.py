"""读取每个软件的 Collector 运行声明 apps/<软件>/runtime.json。

为什么要有这个模块：过去"用什么解释器拉起这个软件"是硬编码在
axis/census.py 里的 if-elif 链，软件名还写进了 argparse 的 choices。
结果加一个新软件必须改核心脚本——这正是"每来一个新软件就改一次流水线"
的病根。

现在核心脚本不认识任何软件名，也不规定 Collector 必须进入软件进程或使用
Python。它只读实现模型写出的声明文件。Collector 可以通过内嵌脚本、进程间桥、
命令行登记表/交互入口、扩展系统或其他真实的程序化接口调查软件。

声明文件长这样：

    {
      "runner": ["application-python", "--headless", "{script}"],
      "env": {},
      "env_from_prefix": null,
      "notes": "Use the application's embedded interpreter"
    }

`runner` 是完整命令模板，必须含一个 ``{script}`` 占位符；核心脚本把应用目录
中的 Collector 或探针路径填进去。旧声明的 `interpreter` 字段继续兼容：它的
末尾仍自动追加脚本路径。`env` 是要额外设置的环境变量，值里可以用
``{prefix}`` 占位符引用 `prefix`。
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


class RuntimeError_(Exception):
    """运行时声明缺失或不合格。"""


def declaration_path(app):
    return os.path.join(ROOT, "apps", app, "runtime.json")


def available_apps():
    """扫描 apps/ 下所有带运行时声明的软件。核心脚本靠它列出可选项，
    而不是靠一份写死的名单。"""
    apps_dir = os.path.join(ROOT, "apps")
    if not os.path.isdir(apps_dir):
        return []
    out = []
    for name in sorted(os.listdir(apps_dir)):
        if name.startswith("_") or name.startswith("."):
            continue
        if os.path.isfile(os.path.join(apps_dir, name, "runtime.json")):
            out.append(name)
    return out


def load(app):
    path = declaration_path(app)
    if not os.path.isfile(path):
        known = available_apps()
        raise RuntimeError_(
            f"缺 {path}。\n"
            "这个文件声明「怎么用正确的解释器拉起这个软件」，由模型在探测\n"
            "运行时之后写出来（见 prompts/census_adapter.md 的环境冒烟一节）。\n"
            f"当前有声明的软件：{known or '一个都没有'}")
    with open(path, encoding="utf-8") as handle:
        declaration = json.load(handle)

    runner = declaration.get("runner")
    interpreter = declaration.get("interpreter")
    if runner is not None:
        if not isinstance(runner, list) or not runner:
            raise RuntimeError_(
                f"{path} 的 runner 必须是非空列表，例如\n"
                '  ["python3", "{script}"]')
        placeholders = sum(str(token).count("{script}") for token in runner)
        if placeholders != 1:
            raise RuntimeError_(
                f"{path} 的 runner 必须恰好包含一个 {{script}} 占位符；"
                f"当前有 {placeholders} 个")
    elif not isinstance(interpreter, list) or not interpreter:
        raise RuntimeError_(
            f"{path} 必须声明 runner；旧版 interpreter 也暂时兼容。例如\n"
            '  {"runner": ["python3", "{script}"]}')
    return declaration


def _prefix_of(declaration):
    prefix = declaration.get("prefix")
    return os.path.expanduser(prefix) if prefix else None


def build_command(app, script):
    """拼出运行应用 Collector/探针的完整命令。

    script 可以是一个脚本路径，也可以是「脚本路径加上它自己的参数」的列表。
    """
    declaration = load(app)
    prefix = _prefix_of(declaration)
    script_parts = ([script] if isinstance(script, str)
                    else [str(x) for x in script])
    script_path = str(script_parts[0])
    script_args = script_parts[1:]
    template = declaration.get("runner")
    legacy = template is None
    template = template or declaration["interpreter"]
    parts = []
    for token in template:
        text = str(token)
        if "{prefix}" in text:
            if not prefix:
                raise RuntimeError_(
                    f"{declaration_path(app)} 的 interpreter 用了 {{prefix}}，"
                    "但没有声明 prefix 字段")
            text = text.replace("{prefix}", prefix)
        text = text.replace("{script}", script_path)
        parts.append(text)
    if legacy:
        parts.append(script_path)
    return parts + script_args


def build_env(app):
    """构造环境变量。返回 None 表示继承当前环境不做改动。"""
    declaration = load(app)
    extra = declaration.get("env") or {}
    if not extra:
        return None

    prefix = _prefix_of(declaration)
    env = dict(os.environ)
    for key, value in extra.items():
        text = str(value)
        if prefix:
            text = text.replace("{prefix}", prefix)
        # 以 : 开头或结尾表示"接在原值前面/后面"，用于 PATH、PYTHONPATH
        if text.endswith(":"):
            env[key] = text + env.get(key, "")
        elif text.startswith(":"):
            env[key] = env.get(key, "") + text
        else:
            env[key] = text

    for directory in declaration.get("mkdirs") or []:
        expanded = os.path.expanduser(str(directory))
        if prefix:
            expanded = expanded.replace("{prefix}", prefix)
        os.makedirs(expanded, exist_ok=True)
    return env


def document_extension(app):
    """返回该软件演示文档的扩展名，由应用声明而不是核心代码列软件表。"""
    declaration = load(app)
    extension = declaration.get("document_extension")
    if not isinstance(extension, str) or not extension.startswith(".") \
            or os.path.basename(extension) != extension:
        raise RuntimeError_(
            f"{declaration_path(app)} 缺合法 document_extension，"
            "例如 '.svg' 或 '.odt'")
    return extension
