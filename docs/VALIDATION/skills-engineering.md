# I05 Skills and Engineering Foundation — Validation Evidence

**Status:** Passed — D-042
**Observed:** 2026-09-13
**Branch:** `feat/i05-skills-engineering-foundation`
**Accepted checkpoint:** `8605437`
**GitHub Actions:** [run 34736716007](https://github.com/Y4NN777/mishkan/actions/runs/34736716007)

## Gate result

I05 is accepted. The final GitHub Actions run passed all seven jobs:

- Python 3.11, 3.12, and 3.13 on Linux;
- Python 3.11, 3.12, and 3.13 on macOS;
- one Linux gate executing the real Docker, Docker Compose, Dev Container CLI, and Podman
  lifecycles through MISHKAN.

The Linux 3.11 matrix ran 510 tests with 5 truthful prerequisite skips and reached 80.12 percent
branch-aware coverage. The macOS 3.13 matrix ran 508 tests with 7 truthful prerequisite skips and
reached 80.05 percent. Every matrix also passed Ruff, formatting, strict mypy, deterministic schema
export, and package builds. The final external-environment job passed its three acceptance tests in
70.82 seconds.

A local deterministic checkpoint before the final macOS portability correction passed 509 tests,
skipped 2 unavailable engine fixtures, deselected 3 external-model/performance markers, and reached
80.11 percent branch-aware coverage. The focused skill-learning boundary passes 13 tests; the
CrewAI-backed skill synthesis/review module is covered at 95 percent.

## Skills, context, and recommendations

The observed suite proves:

- Level 0 catalogue, Level 1 `SKILL.md`, Level 2 references, bundles, explicit/slash invocation,
  contextual selection, and hit/partial/miss evidence;
- package provenance, configurable inspection, quarantine, activation, live mutation, reset,
  archival, deletion, restoration, pinning, and compare-and-swap lifecycle behavior;
- `/learn` creates an attributable Research-authored candidate, uses a separate Research evaluator,
  preserves an applicable base version, and never grants authority or activates itself;
- deterministic context-package materialization, bounded layered loading, immutable artifact
  references, source fingerprints, staleness/omission visibility, and rejection of filesystem
  presentation as authority;
- confirmed engineer facts and configured community catalogues remain evidence-bound;
  recommendations are weighted, inspectable, and always return `activation_authorized: false`;
- OpenTelemetry disclosure profiles are bounded and secret-inspected; the optional LangSmith
  projection and imported feedback remain non-authoritative derived evidence.

The public schema catalogue includes all I05 recommendation, skill, context, environment, and
telemetry contracts. CLI and SDK tests prove that their bounded queries and lifecycle commands use
the same authenticated versioned daemon routes.

## Engineering environments

The tracked polyglot acceptance fixture runs through `mishkand`, authenticated application
commands, technical-pack resolution, and the durable session supervisor. It executes each toolchain
that is actually available and reports unavailable Java/Kotlin, Android, Swift, or Maestro engines
without simulating them. Fixtures cover Go, JavaScript/TypeScript, Java/Kotlin, Android, Python,
Rust, C, Swift, and Maestro discovery/selection contracts.

The final Linux external gate observed:

| Surface | Observed version | Governed proof |
|---|---:|---|
| Docker | 28.0.4 | `FROM scratch` image build and execution through the environment/session boundary |
| Docker Compose | 2.38.2 | two-service up, readiness, down, cleanup, and identical second cycle |
| Dev Container CLI | 0.89.0 | existing definition read, materialized twice, and probed with `exec /bin/true` |
| Podman | 4.9.3 | Containerfile build, bounded attached run, readiness, cancellation, stop, cleanup, and repeat cycle |

Every operation is derived from a public environment profile, exact observation, compatible
binding, immutable descriptor Artifact, and versioned application command. Stateful external
effects settle as `uncertain` unless MISHKAN has sufficient after-state proof; read-only readiness
settles as `verified`. Podman readiness is retried through bounded governed attempts because a live
client process does not itself prove that the named container is inspectable.

The remaining acceptance suite proves greenfield descriptor artifacts, compatible existing
definitions, incompatible platform/engine outcomes, secret references, stale-base conflicts,
location fingerprints, cancellation, cleanup, invalidation, and repeatability. A generated
descriptor does not overwrite the project: persistence continues to require the existing I03
Artifact and Edit/Patch authority.

## Gaps found and closed by the gate

The gate found and closed the following implementation or proof gaps:

- public contract export expectations omitted seven implemented recommendation schemas;
- Dev Container subprocesses received an exact `PATH` that omitted their observed Docker
  dependency, and the profile lacked a real readiness operation;
- the policy effective-date test used a ten-minute margin and became time-dependent under the full
  external suite;
- the initial Podman acceptance fixture used stale configuration, repository, cancellation, and
  readiness assumptions instead of the current public contracts;
- macOS can reject process-group termination with `EPERM`; the isolation runner now falls back to
  signalling the proven child process, with a regression test across the final matrix.

No capability was marked available without a real adapter. No community proposal acquired
authority. CrewAI remains the sole production agent coordination runtime, and deterministic
MISHKAN services retain policy, effect, artifact, state, and acceptance authority.

## Decision

D-042 accepts I05 at `8605437`. I05 may be promoted from its topic branch to `develop`. I06 remains
outside the active implementation scope until explicitly authorized after that promotion.
