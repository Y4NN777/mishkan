# MISHKAN

[![CI](https://github.com/Y4NN777/mishkan/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Y4NN777/mishkan/actions/workflows/ci.yml)

MISHKAN is a local-first control plane for governed CrewAI software-engineering runs. Give it a
repository and an objective; CrewAI coordinates the work while MISHKAN authorizes tool effects,
persists evidence, and makes interrupted work recoverable.

> **Pre-release:** MISHKAN is under active development and currently installs from source. The
> implemented product is a local, single-daemon system for Linux and macOS. The organization,
> skills, knowledge stack, scheduler, distributed workers, and operational TUI remain roadmap work.

## What MISHKAN does

An agentic coding run mixes two different kinds of work: model-driven decisions and effects on a
real system. MISHKAN keeps those responsibilities separate.

- **CrewAI 1.x** plans and coordinates agents and tasks.
- **`mishkand`** accepts commands, owns durable run state, and publishes events.
- **Public configuration and policy** decide which capability may act on which target and with
  which effect.
- **The capability gateway** executes approved file, shell, Git, browser, Web, and MCP operations
  and records their evidence.
- **Artifacts and recovery journals** preserve large outputs and reconcile interrupted effects
  without silently replaying uncertain work.

Operational CLI, Python, HTTP/SSE, and MCP clients all enter through the same daemon authority:

```text
CLI · Python SDK · HTTP/SSE · MCP bridge
                    │
                    ▼
                mishkand
       ┌────────────┼────────────┐
       ▼            ▼            ▼
  CrewAI flow   Policy and    Durable state,
               capabilities  events and artifacts
```

## Available today

| Area | Implemented surface |
|---|---|
| Repository work | Scoped file reads and search, direct processes, Bash, exact change sets, and governed Git effects |
| Long-running work | PTY sessions and managed jobs with cursors, signals, cancellation, settlement, and recovery evidence |
| Control plane | Loopback HTTP API, idempotent commands, SQLite/WAL, explicit Alembic migrations, snapshots, JSONL export, and resumable SSE |
| Artifacts | Chunked uploads, content-addressed immutable bodies, collections, compare-and-swap references, holds, retention, and reconciliation |
| Web and browser | Bounded search/fetch/extract/crawl plus governed Playwright Chromium sessions and diagnostic evidence |
| MCP and harnesses | Governed STDIO and Streamable HTTP clients, a filtered MCP facade, and a stateless local STDIO bridge |

The detailed implementation status and evidence are maintained in the
[documentation index](docs/README.md), not duplicated here.

## Quick start

### Requirements

- Linux or macOS
- Python 3.11, 3.12, or 3.13
- [uv](https://docs.astral.sh/uv/)
- Git
- For model-backed runs: a reachable model provider. The local preset uses
  [Ollama](https://ollama.com/) on `127.0.0.1:11434`.

Some capability and test paths also require `ripgrep` or a Playwright Chromium installation. They
are not required to start the daemon.

### 1. Install from source

```bash
git clone https://github.com/Y4NN777/mishkan.git
cd mishkan
uv sync --locked --dev
uv run mishkan --help
```

### 2. Create the local control plane

Run these commands from the repository that MISHKAN should govern. For this first run, that can be
the MISHKAN checkout itself.

```bash
uv run mishkan config setup --preset local --output .mishkan/config.yaml
uv run mishkan --config .mishkan/config.yaml config validate
uv run mishkan --config .mishkan/config.yaml daemon setup
uv run mishkand --config .mishkan/config.yaml
```

`daemon setup` creates the SQLite database and an instance token at
`.mishkan/runtime/api-token.json`. The token file is written with mode `0600`; operational CLI and
SDK requests use it for authentication. `mishkand` does not create or migrate a database silently.

### 3. Verify the daemon

Leave `mishkand` running and use another terminal:

```bash
curl --fail-with-body http://127.0.0.1:8888/v1/health

uv run mishkan --config .mishkan/config.yaml --json events list --limit 5
```

The health endpoint is intentionally minimal and unauthenticated. Other daemon operations require
the instance token.

### 4. Submit a repository objective

The generated local preset routes planning and execution to `qwen2.5-coder:7b`. Before submitting
a run, make sure that exact model exists in `ollama list`, or replace both model values in
`.mishkan/config.yaml` with exact names installed on your Ollama instance. Validate the edited file
again before continuing.

```bash
ollama list
uv run mishkan --config .mishkan/config.yaml config validate

uv run mishkan --config .mishkan/config.yaml init \
  "Inspect this repository and propose an evidence-based initialization plan" \
  --repository .
```

Inspect the accepted run and follow its durable event stream:

```bash
uv run mishkan --config .mishkan/config.yaml run list
uv run mishkan --config .mishkan/config.yaml events tail --after 0
```

`init` submits work to the daemon; it does not bypass policy or run a second agent runtime. The
repository passed with `--repository` must match the workspace configured for that daemon instance.

## Interfaces

### CLI

Run `uv run mishkan --help` for the authoritative command tree.

| Group | Purpose |
|---|---|
| `config`, `schema` | Generate, inspect, validate, edit, and export public configuration and schemas |
| `daemon`, `db` | Initialize the local instance, rotate its token, and inspect or explicitly upgrade its schema |
| `init`, `run`, `events` | Submit repository work, inspect or recover runs, and query or stream evidence |
| `artifact` | Upload, inspect, reference, retain, reconcile, and collect immutable artifacts |
| `change`, `git` | Apply recoverable filesystem change sets and explicit Git effects |
| `terminal`, `job` | Operate daemon-owned interactive sessions and managed processes |
| `mcp` | Connect, inspect, call, cancel, and reconcile governed MCP peers |

The package also installs:

- `mishkand` — the local application authority and HTTP/SSE server;
- `mishkan-mcp-stdio` — a stateless STDIO bridge to the daemon's configured MCP facade.

### Python SDK

The synchronous SDK reads the same instance token and calls the same HTTP API as the CLI:

```python
from pathlib import Path

from mishkan import Mishkan

with Mishkan(
    "http://127.0.0.1:8888",
    token_file=Path(".mishkan/runtime/api-token.json"),
) as client:
    print(client.health())
    print(client.snapshot())
```

### HTTP, SSE, and MCP

The daemon exposes its versioned API under `/v1`, including commands, health, snapshots, events,
runs, tasks, artifacts, change sets, and execution sessions. Event streaming resumes from
`Last-Event-ID`; retained-history gaps require a fresh snapshot. The configured MCP facade exposes
only its allowlisted application operations and does not grant authority through discovery.

See the exported [public schemas](definitions/schemas/) for wire contracts and the
[system contract](docs/SYSTEM/CONTRACT.md) for invariants and refusal semantics.

## Configuration and credentials

MISHKAN configuration is versioned YAML. Multiple `--config` flags are merged from lowest to
highest precedence; `MISHKAN_CONFIG` may provide the same ordered list using the operating system's
path separator.

```bash
uv run mishkan --config base.yaml --config project.yaml config show
uv run mishkan --config base.yaml --config project.yaml config validate
```

Operational limits, tool sources, policy sources, provider routes, network profiles, session
profiles, and MCP connections belong in public configuration. Secret values do not: configuration
stores credential references, and providers resolve their values at the execution boundary.

## Common failures

**`mishkand` refuses to start because the schema is absent or outdated**

Run `mishkan daemon setup` for a new instance. For an existing instance, inspect it with
`mishkan db status` and apply the explicit `mishkan db upgrade`; the daemon never migrates itself.

**A model-backed run reports that all candidates failed**

Check that the provider is running, compare configured model names with `ollama list`, and run
`config validate` after editing the routes. Configuration validity does not claim that an external
service is healthy.

**`ollama serve` reports that `127.0.0.1:11434` is already in use**

An Ollama server is normally already listening on that address. Confirm it with `ollama list`
instead of starting another process.

## Development

CI runs on Linux and macOS with Python 3.11–3.13. Install Chromium and `ripgrep` before running the
same complete gate locally.

```bash
uv run playwright install chromium

uv run pytest --cov=mishkan --cov-branch --cov-fail-under=80
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src
uv run mishkan schema export --output definitions/schemas
git diff --exit-code -- definitions/schemas
uv build
```

External-model acceptance tests are marked separately and require an explicitly configured model
service. The normal deterministic CI gate does not treat an unavailable paid provider as success.

## Documentation

- [Documentation index and authority rules](docs/README.md)
- [Product requirements](docs/PROJECT/PRD.md)
- [Software requirements](docs/PROJECT/SRS.md)
- [System contract](docs/SYSTEM/CONTRACT.md)
- [Architecture](docs/SYSTEM/ARCHITECTURE.md)
- [Implementation plan and roadmap](docs/PROJECT/IMPLEMENTATION_PLAN.md)
- [Decision log](docs/PROJECT/DECISION_LOG.md)
- [Observed validation evidence](docs/VALIDATION/)

The requirements-to-architecture chain follows
[SWE-BASICS-BEFORE-CODE](https://github.com/MatrixCollab/SWE-BASICS-BEFORE-CODE). Working notes and
temporary reviews are intentionally kept outside the versioned documentation baseline.

## Support

Use [GitHub Issues](https://github.com/Y4NN777/mishkan/issues) for reproducible bugs and feature
requests. Include the MISHKAN version, Python version, operating system, redacted configuration,
and the relevant event or error envelope.
