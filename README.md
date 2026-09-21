# AXISRelease

AXISRelease is a standalone source distribution that converts an installed
application into a command-line package for agents. 

## Workflow

The workflow has four main stages:

1. **Capability discovery:** identify programmatic entrypoints, collect runtime
   capabilities, and create the application adapter.
2. **Semantic filtering:** decide which capabilities support useful user commands.
3. **Command implementation:** implement object-family batches in isolated working
   copies, then build and validate commands through Python controllers.
4. **Documentation and release:** generate command documentation and an agent
   skill, then package passing commands under `dist/<app>-axis/`.

Coding agents perform scoped tasks. Python controllers determine progression,
validation results, and release contents.

## Requirements

- Linux and Python 3.10 or later; Python code uses the standard library.
- A command-line coding agent; the default executable is `codex`.
- An installed target application that the current user can launch.
- Permission to create test files and enough disk space for results and logs.

## Quick start

Run from this directory:

```bash
chmod +x bin/axis-release bin/axis-bootstrap
./bin/axis-release doctor
./bin/axis-bootstrap "Convert my installed application into AXIS commands"
```

## Application directory

```text
apps/<app>/
├── runtime.json
├── census.py
├── census/raw_ops.json
├── engine.py
├── atlas_gen.py
├── axis-app.json
├── atlas/
├── commands/
├── build/report.json
├── guide/
└── skills/
```
