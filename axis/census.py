"""AXIS 能力普查（S1）分发器：运行每个软件自己的 Collector。

原则（METHOD.md §S1）：数字必须来自目标软件真实公开的程序化接口，各应用的
六类通道实现放在 apps/<app>/census.py。本模块只负责按实现模型写出的命令模板
运行 Collector；不规定 Collector 必须进入软件进程，也不规定使用哪种语言。

运行命令和环境变量读 apps/<app>/runtime.json，**本模块不认识任何软件名**。
过去这里是一条 if-elif 硬编码链、软件名还写进了 argparse 的 choices，
加一个新软件必须改这个核心文件；现在加软件只需要多一份声明文件。

产物约定：apps/<app>/census/raw_ops.json，顶层结构
{app, engine_version, channels, reconciliation, unavailable}；
通道不可用必须进 unavailable 并附原因，不许静默缺失、不许编造数字。

用法（在 AXIS/ 下）：
  python3 axis/census.py --app <app>
"""
import argparse
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

import runtime  # noqa: E402  （同目录模块，必须在 sys.path 设好之后导入）

def app_command(app):
    """返回 (cmd, env)。脚本产物路径由脚本自己固定到 apps/<app>/census/。

    怎么运行 Collector，读 apps/<app>/runtime.json，本模块不认识任何软件名。
    """
    script = os.path.join(_ROOT, "apps", app, "census.py")
    try:
        command = runtime.build_command(app, script)
        env = runtime.build_env(app)
    except runtime.RuntimeError_ as error:
        raise SystemExit(str(error))
    if not os.path.isfile(script):
        raise SystemExit(
            f"缺 {script}（这个软件的普查适配器）。\n"
            "它由模型按 prompts/census_adapter.md 生成。")
    return command, env


def main():
    ap = argparse.ArgumentParser(description="AXIS S1 能力普查分发器")
    ap.add_argument("--app", required=True,
                    help="软件名。可选项由 apps/*/runtime.json 决定，"
                         f"当前有：{', '.join(runtime.available_apps()) or '无'}")
    args = ap.parse_args()

    cmd, env = app_command(args.app)
    print(f"[census] {args.app}: {' '.join(cmd[:2])} ...", flush=True)
    r = subprocess.run(cmd, env=env, cwd=_ROOT)
    if r.returncode != 0:
        raise SystemExit(f"[census] {args.app} 失败，退出码 {r.returncode}")

    out = os.path.join(_ROOT, "apps", args.app, "census", "raw_ops.json")
    if not os.path.isfile(out):
        raise SystemExit(f"[census] {args.app} 未产出 {out}")
    d = json.load(open(out))
    print(f"[census] {args.app} 完成：引擎 {d.get('engine_version')}，"
          f"通道 {sorted(d.get('channels', {}))}，"
          f"不可用 {len(d.get('unavailable', []))} 个 -> {out}")


if __name__ == "__main__":
    main()
