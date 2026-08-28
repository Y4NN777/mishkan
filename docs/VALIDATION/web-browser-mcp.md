# I04 Web, Browser, MCP, and External Harnesses — Validation Evidence

**Status:** Passed — D-039 current baseline revalidated

**Branch:** `feat/i04-web-browser-mcp-harnesses`

**Original D-039 gate:** 2026-08-27

**Cross-baseline audit checkpoint:** `6a73e98`, validated locally 2026-08-28

**Current remote gate and promoted checkpoint:** `3481ca5`, validated 2026-08-28

**Identity:** `Y4NN777 <axel.studiesmail@gmail.com>`

## Observed runnable result

The I04 implementation extends the same loopback `mishkand` authority accepted in I03. Web,
Browser, and outbound MCP operations are concrete capability adapters invoked through the existing
registry, public policy authority, schema validation, credential resolver, and effect gateway.
HTTP/OpenAPI, SSE, the inbound MCP facade, and the local STDIO bridge remain clients of that
authority. None owns policy, durable state, or an agentic runtime. CrewAI remains the only internal
production runtime.

The Web surface separates search, fetch, general HTTP request, extraction, map, and crawl. Brave is
a credentialed direct source; SearXNG is a broker whose observed upstreams remain distinct; HTTPX
owns bounded transport; Trafilatura owns attributable extraction; and the configured native crawler
uses the governed fetch/extract surfaces. Search routes preserve source and upstream provenance.
Fetched, extracted, cached, and citation evidence uses immutable artifacts and content hashes.
Redirects and every connected peer pass the configured scheme, port, address-class, DNS-answer,
credential-origin, response-size, and decompression controls. Unsupported or unavailable adapters
remain unavailable or visibly degraded rather than being inferred from a configured component name.

Browser sessions have durable isolated, project-persistent, and attached-existing profile
contracts. The concrete Playwright Chromium adapter runs on its engine-owned thread, mediates
network access, creates immutable tree and screenshot evidence, and exposes bounded Chrome DevTools
diagnostics for console, network, performance, storage, and service workers. Actions bind to an
exact observation and target revision, authorize the resolved interaction effect, reject stale
targets, resolve credentials late, and move a session to an explicit uncertain state when certainty
is lost after dispatch. The real acceptance fixture proved observation, form actions, upload,
JavaScript, navigation, diagnostics, screenshots, and restoration of authenticated cookie state
from a project-persistent profile.

The conformance checkpoint additionally authorizes the exact destination of link, form, and
coordinate-triggered effects; treats JavaScript as `script.execute`; governs persistent-profile and
attached-CDP filesystem/network effects; and proves browser-engine readiness before publishing the
capability. Persistent Chromium state is archived as a sensitive immutable Artifact, restored
through descriptor-safe extraction, referenced through CAS, and removed from its raw directory
after settlement. Unresolved raw state after a crash blocks reuse pending reconciliation.
Diagnostics and downloads are bounded and expose cursor gaps rather than silently losing evidence.

Outbound MCP connections persist configured identity, protocol strategy, negotiated version,
server identity, capability fingerprint, discovery revision, primitive schemas, and call journal.
The official MCP SDK executes real STDIO and Streamable HTTP sessions. Discovery drift is explicit;
dynamic primitives are mediated through one gateway adapter; progress is bounded and retained; and
late credentials are resolved only for the authorized connection. Opt-in remote MCP Tasks persist
their remote task identity before polling, support reconnect from a new transport session,
schema-validate the terminal result, and require explicit reconciliation after transport loss.
Negotiated cancellation is durable and can be requested through the daemon without silently
claiming that an unconfirmed remote effect stopped.

STDIO MCP execution now requires a configured isolation builder, suppresses untrusted child stderr,
and receives only explicitly mapped credential environment variables. Streamable HTTP rejects
routing and framing header overrides. Discovery, progress, retention, pagination, primitive counts,
messages, and total bytes are bounded by public configuration. Server annotations cannot grant
retry authority. Discovered resources and prompts are reported as unsupported for invocation
instead of being advertised as executable; an inbound profile cannot expose unsupported prompts.

The inbound MCP facade exposes only configured operations. It authenticates the harness identity,
requires the command actor to match it, validates the application-command envelope, and delegates
to the same daemon command/query surfaces. The STDIO process is a stateless bridge to that facade.
The public `run.initialize` command carries one bounded objective for the daemon's already
configured repository; it cannot select a runtime, internal agent, arbitrary repository path, or
authority. `mishkand` creates the durable run identity and accepts the idempotent command before it
starts the CrewAI Flow outside the transaction. In-flight duplicate UUIDs join the same task, a
later replay returns the initial receipt, and interruption before acceptance remains a stable
refusal rather than an automatic effect replay. The acceptance fixture traverses the official MCP
SDK and proves accepted command → CrewAI plan → governed evidence → independent review → durable
completed run. A harness therefore cannot bypass application acceptance or acquire authority from
discovery alone.

## Local automated evidence

| Check | Observed result |
|---|---|
| Full deterministic suite excluding external-model markers | 400 passed, 1 skipped, 2 deselected |
| Global branch coverage | 80.24%, threshold 80% |
| Real Playwright Chromium acceptance fixture | Passed |
| Real MCP STDIO transport fixture | Passed |
| Real MCP Streamable HTTP transport fixture | Passed |
| Remote MCP Task reconnect and cancellation fixtures | Passed |
| Event ingestion | At least 100 accepted events/s under the direct ingestion contract |
| Ruff check and format check | Passed |
| `mypy --strict src` | Passed, 146 source files |
| `uv lock --check` | Passed, 169 packages |
| Source and wheel distributions | Passed |
| Deterministic public schema export | Passed |
| `git diff --check` | Passed |

The declarative Alembic revision scripts are excluded from line/branch instrumentation because
Alembic loads them through its migration runtime. Their observable behavior is not excluded from
acceptance: black-box tests cover empty-database setup, exact I02 recognition and preservation,
unknown/lookalike-schema refusal, and monotone event cursors across retention and upgrade. Runtime
persistence, migration management, events, artifacts, edits, sessions, Browser, Web, and MCP remain
inside the global 80% coverage gate.

## Cross-baseline conformance audit closure

The audit did not rely on the old green gate. It rechecked requirements against execution
boundaries and closed verified gaps in:

- command revision serialization, schema-version validation, SQLite cursor high-water preservation,
  exact legacy recognition, and Web-cache races;
- browser destination/effect authority, attached/persistent profile effects, secret-safe durable
  state, path/symlink handling, bounded evidence, crash uncertainty, and real-engine availability;
- MCP process containment, credential and header boundaries, retry authority, bounded discovery and
  progress, retention, primitive support truthfulness, and secret-safe progress;
- public Web concurrency, Git remote identity, descriptor-safe file/AST reads, Artifact publication
  serialization, immediate secret inspection, bounded change/command bodies, and asynchronous daemon
  query dispatch.

The tracked tests include adversarial destinations, JavaScript authority, persistent-profile
settlement/recovery, missing MCP isolation, stderr canaries, malicious HTTP headers, infinite/cyclic
discovery, progress pruning/pagination, Web concurrency, stale application revisions, event
retention, schema lookalikes, symlink races, CAS publication races, and oversized public inputs.

The performance gate now measures one authoritative command/event acceptance transaction per
ingested event. Effect reservation and later acceptance remain a separate atomicity test because
they are two deliberately durable transactions around an external effect. The job readiness gate
uses an explicit continuation signal after `READY`; it no longer depends on a fixed sleep racing a
loaded runner.

## Contract, fault, and security evidence

The tracked tests exercise:

- direct, aggregate, verification, explicit-source, and compatible fallback search selection;
- source/upstream provenance, score-scale separation, citation spans, cache freshness, and visible
  lost coverage;
- mixed DNS answers, connected-peer mismatch, private/link-local refusal, redirect escape,
  credential forwarding, bounded bodies, decompression, and loopback policy;
- bounded map/crawl depth, count, delay, patterns, robots behavior, extraction failure, and stop
  conditions;
- browser owner isolation, profile scope, stale observations, target-revision drift, resolved
  effects, late credentials, output inspection, uncertain dispatch, close/reconcile, and retention;
- persisted authenticated browser state and bounded CDP evidence from a real Chromium process;
- MCP protocol negotiation, identity mismatch, discovery pagination, schema drift, unavailable
  primitives, connection loss, progress retention, output-schema refusal, and effect uncertainty;
- real STDIO and Streamable HTTP MCP discovery and calls, reconnect to an existing remote Task,
  confirmed cancellation, daemon restart reconciliation, and cancellation after transport loss;
- harness authentication, actor mismatch, exposure-profile refusal, malformed arguments, HTTP/SSE
  command delegation, the stateless STDIO facade, and an idempotent objective accepted durably
  before CrewAI planning, evidence synthesis, and independent review.

No test treats a search snippet as page proof, a configured adapter as installed, a browser
screenshot as backend proof, MCP discovery as authority, or an indeterminate remote effect as a
successful completion.

## Historical live-model and remote evidence

The tracked CrewAI/Ollama initialization-and-resume regression passed on the original D-039
implementation against the local Ollama service with `qwen2.5-coder-7b-16k:latest` for planning and
`deepseek-coder-v2:16b` for execution: 1 passed in 213.62 seconds. It used no paid provider and
proved an accepted result followed by deterministic resume with the same plan fingerprint and
results.

GitHub Actions run
[`33079214948`](https://github.com/Y4NN777/mishkan/actions/runs/33079214948) passed at commit
`55b01c3255d24a5db525ae3f7e91aa02d10558d8`. All six Linux/macOS jobs on Python 3.11, 3.12, and
3.13 passed tests with branch coverage, Ruff, formatting, strict typing, deterministic schema
export, and distribution build. Chromium and ripgrep are provisioned explicitly so the remote gate
executes rather than skips their acceptance fixtures. Matrix caches use a Python-version suffix,
preventing different jobs from racing to publish the same cache key.

Observed warnings were upstream CrewAI/OpenTelemetry deprecations and a GitHub macOS runner warning
about an unrelated preinstalled Homebrew tap. They did not suppress tests or alter the results. A
previous green run was not used to waive the two timing-sensitive I03 tests exposed later: their
contracts were corrected and the complete six-job matrix was rerun successfully. The first remote
run of the new harness objective contract exposed one stale expected-schema catalogue; that
catalogue was corrected, the full local gate was rerun, and run 33079214948 then passed all six
jobs.

These live-model and remote runs validate commit `55b01c3`; they are retained as historical evidence
and do not validate checkpoint `6a73e98`. The external-model gates were not rerun during the
cross-baseline gap closure.

## Current remote evidence

GitHub Actions run
[`33186236919`](https://github.com/Y4NN777/mishkan/actions/runs/33186236919) passed at commit
`c69e192867413afc07f30992f8ad7f6fa2eaaae2`. All six Linux/macOS jobs on Python 3.11, 3.12, and
3.13 passed the deterministic suite with branch coverage, Ruff, formatting, strict typing,
deterministic schema export, and distribution build. This run contains code checkpoint `6a73e98`
and the first corrected validation baseline; it is retained as the audit-checkpoint evidence.

The first promotion run on `develop`,
[`33195052261`](https://github.com/Y4NN777/mishkan/actions/runs/33195052261), exposed a transient
Chromium `Page.captureScreenshot` refusal on Python 3.13/Ubuntu. The same real-browser test passed
alone, proving a capture race rather than an unsupported platform or deterministic contract error.
Checkpoint `3481ca5` retries only that exact read-only Chromium refusal once; other Playwright errors
remain immediate failures. Regression tests prove both behaviors. The full Python 3.13 local gate
then passed with 400 tests and 80.24% branch coverage.

The corrected topic matrix
[`33203400309`](https://github.com/Y4NN777/mishkan/actions/runs/33203400309) and promoted `develop`
matrix [`33203711009`](https://github.com/Y4NN777/mishkan/actions/runs/33203711009) both passed all
six Linux/macOS Python 3.11–3.13 jobs at `3481ca59ca663512d801a5dc5170599785bf2e9f`.

## Acceptance boundary

D-039 records the original I04 acceptance on 2026-08-27 and the corrected current-baseline
revalidation on 2026-08-28. The requested conformance audit is implemented at `6a73e98`; the
promotion correction, 400-test local gate, topic matrix, and `develop` matrix are green at
`3481ca5`. I04 is promoted to `develop`. I05 awaits explicit engineer authorization.
