# AXISRelease

AXISRelease 是 AXIS 的独立源码发布件。它把一个已安装软件逐步转换成可供 Agent
使用的命令行包，同时把软件专用知识限制在 `apps/<app>/` 内。共享控制器只负责
阶段顺序、批次、并行、恢复、验收和发布，不按软件名称执行不同分支。


## 工作流

一次完整运行包含四个外部阶段，内部共 14 道可恢复工序：

1. **能力发现**：确定真实程序化入口，运行能力普查，生成应用自有适配层；
2. **语义过滤**：按用户价值分批判断哪些能力应公开为命令；
3. **命令实现**：按对象组织批次，由隔离的 Coding Agent 实现，再由 Python
   构建和真实验证；
4. **组织与发布**：生成完整命令文档和 Skill，把验证通过的命令冻结成
   `dist/<app>-axis/`。

Coding Agent 只完成边界明确的局部任务。是否进入下一阶段、某条命令是否验证
通过、哪些文件进入发布包，均由 Python 控制器决定。

## 环境要求

- Linux；
- Python 3.10 或更高版本，只使用标准库；
- 一个可通过命令行调用的 Coding Agent，默认是 `codex`；
- 目标软件已经安装，并且当前用户有权启动它和写入测试文件；
- 足够的磁盘空间。真实能力调查会保留结构化中间结果和日志。


## 五分钟开始

在本目录执行。通常只需要给出应用名称，先导程序会让一个独立 Agent 在本机
理解这句话、识别目标软件，再让另一个独立 Agent 寻找并验证本机入口，最后自动
启动原有四阶段流程：

```bash
chmod +x bin/axis-release
chmod +x bin/axis-bootstrap
./bin/axis-release doctor
./bin/axis-bootstrap "你帮我把 FreeCAD 变成一套 Agent 可以使用的 AXIS 能力"
```

`doctor` 检查配置、Python、Coding Agent 和发布件结构。`axis-bootstrap` 不要求
用户提供严格的软件标识或可执行文件路径。第一个只读 Agent 理解自然语言、常见
别名和可能的拼写误差；Python 验证其结构化计划后，第二个 Agent 才检查 PATH、
应用目录、包管理器登记和可编程入口。只有获得本机证据，Python 才会调用
`axis-release run`。全部记录保存在 `bootstrap/runs/`。

先导程序启动后会逐项打印：收到请求、启动观测界面、理解自然语言、发现本机
入口、交给正式 AXIS。它还会自动启动只读 Web 观测台并打印网址：

```text
[bootstrap] 可观测界面：http://127.0.0.1:8765
```


只调查入口、不启动后续流程：

```bash
./bin/axis-bootstrap "看看能不能把 FreeCAD 做成 AXIS 能力" --discover-only
```

默认会恢复 `apps/<app>/onboard/state.json` 中的进度。要明确开始一个全新运行：

```bash
./bin/axis-bootstrap "重新把 FreeCAD 完整 AXIS 化" --fresh
```

只想运行某一道内部工序时，可以继续传递 `onboard.py` 参数：

```bash
./bin/axis-release run --app inkscape --launch inkscape -- --only 过验证门
```

## 配置

默认配置是根目录的 [`axis-config.json`](axis-config.json)。它控制：

- Coding Agent 的可执行命令、模型和全局最大进程数；
- 能力过滤批次大小与并行批次数；
- 命令实现批次大小、并行 Agent 数量和单项重试次数；
- Agent、脚本和验证的超时；
- 命令文档是否生成；
- 最终验证并行度。

可以复制配置到其他位置，并在所有入口中显式传入：

```bash
cp axis-config.json my-axis-config.json
./bin/axis-release doctor --config my-axis-config.json
./bin/axis-release run --app myapp --launch /opt/myapp/bin/myapp \
  --config my-axis-config.json
```

字段、取值和并行关系见 [docs/CONFIGURATION.md](docs/CONFIGURATION.md)。配置会在
运行开始时严格校验；拼错字段类型或使用非法并行数会在调用 Agent 前停止。

## 应用目录边界

一个应用最终可能包含：

```text
apps/<app>/
├── runtime.json          # 如何启动应用自己的 Collector 或探针
├── census.py             # 应用能力普查器
├── census/raw_ops.json   # 原始能力清单
├── engine.py             # 应用专用执行适配层
├── atlas_gen.py          # 可选的候选生成探针
├── axis-app.json         # 构建、验证、文档和发布契约
├── atlas/                # 命令候选
├── commands/             # 已实现命令
├── build/report.json     # 真实验证结果
├── guide/                # 完整精简文档
└── skills/               # 教 Agent 使用最终命令包的 Skill
```


## 输出与恢复

- 总进度：`apps/<app>/onboard/state.json`；
- 先导请求、模型输出和观测服务：`bootstrap/runs/<request-run>/`；
- 过滤批次：`stages/01_capability_discovery/runs/<run-id>/`；
- 实现批次：`stages/02_05_command_release/runs/<run-id>/`；
- 最终应用包：`dist/<app>-axis/`；
- 最终启动器：`bin/<app>-axis`。
