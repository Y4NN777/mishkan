# MISHKAN

MISHKAN is a local-first engineering organization and durable control plane powered by CrewAI.
CrewAI 1.x is the sole internal production runtime for agents and crews. MISHKAN provides the
deterministic authority around it: configuration, policy, capability execution, durable state,
evidence, recovery, and external client mediation.

The design follows the
[SWE-BASICS-BEFORE-CODE](https://github.com/MatrixCollab/SWE-BASICS-BEFORE-CODE) chain:

> Intent → Requirement → Contract → Responsibility → Architecture → Code

## Current status

The integrated baseline through I04 is on `main` and accepted by D-040. It includes:

- strict versioned configuration, credential references, public schemas, and stable errors;
- real CrewAI/Ollama planning, production, independent review, and deterministic resume;
- a public configurable policy authority, truthful capability registry, and governed gateway;
- File, Search, Process, Bash, Git effects, recoverable change sets, PTY, and managed jobs;
- one loopback `mishkand` authority with SQLite/WAL, explicit migrations, commands, events, SSE,
  snapshots, idempotence, and optimistic concurrency;
- immutable and streamable Artifacts, compare-and-swap references, retention, and reconciliation;
- typed Web search/fetch/extract/crawl, governed Playwright browser sessions, and durable evidence;
- governed MCP client/server mediation over STDIO and Streamable HTTP, including external harness
  objectives that enter the same MISHKAN and CrewAI authority.

I05–I11 remain planned work. Their presence in the implementation plan is not a claim that skills,
the 59-agent organization, knowledge services, scheduling, distributed workers, or the Go TUI are
already implemented.

## Authority model

- `mishkand` owns authoritative application state and command acceptance.
- CLI, Python SDK, HTTP/SSE, MCP, and external harnesses are clients of that same authority.
- CrewAI owns agent and crew coordination; MISHKAN does not introduce a competing agent runtime.
- Capability availability never grants permission. Exact targets, effects, credentials, and
  execution context are evaluated through public policy before dispatch.
- Stateful or uncertain effects are reconciled from durable evidence instead of being silently
  replayed.

## Local setup

Python 3.11–3.13 on Linux and macOS is supported.

```bash
uv sync --locked --dev

uv run mishkan config setup --preset local --output .mishkan/config.yaml
uv run mishkan --config .mishkan/config.yaml config validate
uv run mishkan --config .mishkan/config.yaml daemon setup
uv run mishkand --config .mishkan/config.yaml
```

In another terminal, submit a repository-specific objective to the daemon:

```bash
uv run mishkan --config .mishkan/config.yaml init \
  "Inspect the repository evidence and initialize the project" \
  --repository .
```

Configuration layers are passed from low to high precedence by repeating `--config`.
`MISHKAN_CONFIG` may instead contain an operating-system-path-separated list. Presets store only
credential references; credential values are resolved late and must not be written to YAML.

## Available CLI surfaces

Run `uv run mishkan --help` for the complete command tree. The integrated baseline exposes:

- `config`, `schema`, `daemon`, and `db` for local control-plane administration;
- `init`, `run`, and `events` for durable execution and observability;
- `artifact`, `change`, and `git` for governed stateful effects;
- `terminal` and `job` for daemon-owned execution sessions;
- `mcp` for governed external connections and remote-task reconciliation.

The package also installs `mishkand` and the stateless `mishkan-mcp-stdio` bridge.

## Verification

```bash
uv run pytest --cov=mishkan --cov-branch --cov-fail-under=80
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src
uv run mishkan schema export --output definitions/schemas
git diff --exit-code -- definitions/schemas
uv build
```

CI executes the complete matrix on Linux and macOS with Python 3.11, 3.12, and 3.13. The current
I04 evidence records 400 deterministic tests and 80.24% branch coverage before the final green
integration matrices.

## Documentation

- [Documentation authority index](docs/README.md)
- [Product requirements](docs/PROJECT/PRD.md)
- [Software requirements](docs/PROJECT/SRS.md)
- [System contract](docs/SYSTEM/CONTRACT.md)
- [Architecture](docs/SYSTEM/ARCHITECTURE.md)
- [Implementation plan](docs/PROJECT/IMPLEMENTATION_PLAN.md)
- [Decision log](docs/PROJECT/DECISION_LOG.md)
- [I04 validation and integration evidence](docs/VALIDATION/web-browser-mcp.md)

Working notes and temporary reviews are not project authority and are not versioned as durable
documentation.
