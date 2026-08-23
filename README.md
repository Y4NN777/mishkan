# MISHKAN

MISHKAN is a local-first, CrewAI-powered engineering organization and control plane. The approved
design follows the
[SWE-BASICS-BEFORE-CODE](https://github.com/MatrixCollab/SWE-BASICS-BEFORE-CODE) chain:

> Intent → Requirement → Contract → Responsibility → Architecture → Code

Implementation is proceeding through the vertical increments in
[`docs/PROJECT/IMPLEMENTATION_PLAN.md`](docs/PROJECT/IMPLEMENTATION_PLAN.md). I00 provides the
contract-bearing Python foundation: strict versioned configuration, layer and field provenance,
credential references, stable error envelopes, explicit schema compatibility, JSON Schema export,
and the `mishkan config` CLI.

CrewAI 1.x is a mandatory core dependency and the sole production coordination runtime. I00 does
not execute agents yet; the first real local CrewAI/Ollama path belongs to I01.

## Development

```bash
uv sync --dev
uv run pytest --cov=mishkan --cov-branch --cov-fail-under=80
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src
```

Python 3.11–3.13 is tested on Linux and macOS in CI.

## Configuration

Create an inspectable preset, then validate or inspect it:

```bash
uv run mishkan config setup --preset local --output .mishkan/config.yaml
uv run mishkan --config .mishkan/config.yaml config validate
uv run mishkan --config .mishkan/config.yaml config show
uv run mishkan config set timezone Africa/Ouagadougou --file .mishkan/config.yaml
```

Repeat `--config` from low to high precedence to compose layers. Alternatively,
`MISHKAN_CONFIG` accepts an operating-system-path-separated list. Presets contain only explicit
credential references; MISHKAN does not resolve or print credential values during configuration
inspection.

Durable project authority starts at [`docs/README.md`](docs/README.md). Working notes and review
checklists are not stored as project documents.
