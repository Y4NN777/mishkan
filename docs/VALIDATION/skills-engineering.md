# I05 Skills and Engineering Foundation — Validation Evidence

**Status:** Open; deterministic I05 capability checkpoint passed, external environment gate pending  
**Observed:** 2026-08-30  
**Branch:** `feat/i05-skills-engineering-foundation`  
**Checkpoints:** `d462ac2`, `501a657`

## Observed capability checkpoint

The focused I05 suite passed with **56 tests** in 12.84 seconds. It covers the skill catalogue,
progressive loading, learning, lifecycle, maintenance, context packages, confirmed engineer facts,
candidate-only community recommendations, environment observation/resolution/adapters/repositories,
technical packs, telemetry, and the polyglot acceptance fixture.

The tracked polyglot fixture was executed through `mishkand`, application-command authorization,
technical-pack resolution, and the I03 session supervisor. On this host, Go, JavaScript/TypeScript,
Python, Rust, and C commands executed successfully. Java/Kotlin, Android, Swift, and Maestro remained
visible without being presented as executable where their required project engines were absent.

The real run exposed and closed three implementation gaps:

- pack commands now receive public, bounded workspace-local cache configuration and an exact
  `PATH` derived from observed engine dependencies;
- executable observation preserves the invoked path for multicall/symlink tools such as Cargo
  instead of silently changing `cargo` into `rustup`;
- a pack alternative is selected only when its engine is both eligible on the machine and observed
  as used by the project, preventing CMake from replacing Make in a Makefile-only project.

Configured community catalogues are queryable through CLI, SDK, and HTTP. Recommendation requests
carry explicit project evidence and weighted criteria. Failed or unknown compatibility, policy,
trust, or execution constraints exclude a candidate. Every result contains
`activation_authorized: false`; the recommendation surface exposes no install or activation effect.

Ruff, format check, strict mypy, schema export, and `git diff --check` passed at the capability
checkpoint. A complete regression run inside the restricted task sandbox reached the loopback MCP
tests and was blocked by `socket(AF_INET) -> EPERM`; this is an execution-environment restriction,
not accepted full-suite evidence. The same full run must be repeated in CI or an unrestricted local
test process before closing I05.

## Observed host state

| Surface | Evidence |
|---|---|
| Go | `go1.25.0 linux/amd64` |
| Node/npm | `v24.14.0` / `11.9.0` |
| Java | OpenJDK `25.0.3`; Maven and Gradle unavailable |
| Python | host `3.12.3`; test environment `3.11.15` |
| Rust | Cargo `1.97.1`; rustc `1.97.1` |
| C | GCC-compatible `cc 13.3.0` |
| Swift / Maestro | unavailable |
| Dev Container CLI | unavailable |
| Podman | unavailable |
| Docker / Compose | Docker `29.7.2`; Compose `v5.4.0`; daemon reachable only outside the restricted task sandbox |

## Remaining gate evidence

I05 is not accepted yet. The following normative acceptance evidence remains unobserved:

- a real compatible worker with Podman performing the Containerfile build, bounded run,
  interruption settlement, cleanup, and repeatability scenario;
- a real Dev Container CLI reusing an existing definition and verifying materialization;
- the Docker/Compose lifecycle fixture through the same governed operation/session boundary;
- the complete deterministic regression suite in an environment that permits its loopback HTTP/MCP
  fixtures;
- the required Linux/macOS and Python 3.11–3.13 remote matrix and the scoped branch-coverage gate.

No D-042 acceptance decision is recorded while these proofs are missing. The implementation remains
truthful: unavailable engines are not simulated, and descriptor validation is not reported as
runtime readiness.
