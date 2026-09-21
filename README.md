# AXISRelease

AXISRelease is a standalone source distribution that converts an installed
application into a command-line package for agents. Application-specific code
lives under `apps/<app>/`. Shared controllers manage stage ordering, batches,
concurrency, resumption, validation, and release.

## Workflow

The workflow has four public stages and 14 resumable steps:

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

Replace the example request with the application's name. `doctor` checks
configuration, Python, the agent executable, and release structure. Bootstrap
first interprets the request, then independently investigates the local
entrypoint. Python validates the structured results before starting
`axis-release run`. Records are stored under `bootstrap/runs/`.

Bootstrap prints progress and starts a local read-only workflow monitor:

```text
[bootstrap] Monitor: http://127.0.0.1:8765
```

To discover the entrypoint without starting the main workflow:

```bash
./bin/axis-bootstrap "Find the entrypoint for my installed application" --discover-only
```

Progress normally resumes from `apps/<app>/onboard/state.json`. Start a new run
with `--fresh`:

```bash
./bin/axis-bootstrap "Convert my installed application into AXIS commands" --fresh
```

When the application identifier and launcher are known:

```bash
./bin/axis-release run --app myapp --launch myapp
./bin/axis-release run --app myapp --launch myapp -- --only "Verify commands"
```

Step names and `--only` selectors are English. Progress files from earlier
releases with different step names are not compatible; use a fresh run.

## Configuration

The default file is [axis-config.json](axis-config.json). It controls the agent
executable and model, global process limit, filtering and implementation batch
sizes, concurrency, retries, timeouts, documentation, and verification jobs.

```bash
cp axis-config.json my-axis-config.json
./bin/axis-release doctor --config my-axis-config.json
./bin/axis-release run --app myapp --launch myapp --config my-axis-config.json
```

See [docs/CONFIGURATION.md](docs/CONFIGURATION.md) for field definitions. The
controller validates configuration before invoking agents.

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

`runtime.json` declares how to run the collector and probes. `census.py` writes
the capability inventory. `engine.py` implements application-specific execution.
`atlas_gen.py` optionally creates initial command candidates. `axis-app.json`
declares build, verification, documentation, and packaging requirements.
See [docs/ATLAS.md](docs/ATLAS.md) for command definition fields.

## Outputs and resumption

- Overall progress: `apps/<app>/onboard/state.json`.
- Bootstrap records and monitor logs: `bootstrap/runs/<request-run>/`.
- Filtering batches: `stages/01_capability_discovery/runs/<run-id>/`.
- Implementation batches: `stages/02_05_command_release/runs/<run-id>/`.
- Released application package: `dist/<app>-axis/`.
- Application launcher: `bin/<app>-axis`.

The main workflow passes its discovery run ID explicitly to command preparation.
Standalone preparation requires `--run-id` and reads that discovery run by
default. Use repeatable `--discovery-run` options to select other runs explicitly.
Optional `--selection` and `--discovery-result` inputs must also be supplied
explicitly.

## Source distribution

Run `make test` to check the source and `sha256sum -c MANIFEST.sha256` to check
release file integrity. Run `make package` to create a source archive containing
only manifest-listed files. The archive does not include version-control
metadata, local configuration overrides, generated application data, or run logs.
