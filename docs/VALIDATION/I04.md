# I04 Web, Browser, MCP, and External Harnesses — Validation Evidence

**Status:** Passed — accepted by D-039

**Branch:** `feat/i04-web-browser-mcp-harnesses`

**Validated locally and remotely:** 2026-08-27

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

Outbound MCP connections persist configured identity, protocol strategy, negotiated version,
server identity, capability fingerprint, discovery revision, primitive schemas, and call journal.
The official MCP SDK executes real STDIO and Streamable HTTP sessions. Discovery drift is explicit;
dynamic primitives are mediated through one gateway adapter; progress is bounded and retained; and
late credentials are resolved only for the authorized connection. Opt-in remote MCP Tasks persist
their remote task identity before polling, support reconnect from a new transport session,
schema-validate the terminal result, and require explicit reconciliation after transport loss.
Negotiated cancellation is durable and can be requested through the daemon without silently
claiming that an unconfirmed remote effect stopped.

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
| Full deterministic suite excluding external-model markers | 275 passed, 2 deselected |
| Global branch coverage | 80.21%, threshold 80% |
| Real Playwright Chromium acceptance fixture | Passed |
| Real MCP STDIO transport fixture | Passed |
| Real MCP Streamable HTTP transport fixture | Passed |
| Remote MCP Task reconnect and cancellation fixtures | Passed |
| Event ingestion | At least 100 accepted events/s under the direct ingestion contract |
| Ruff check and format check | Passed |
| `mypy --strict src` | Passed, 127 source files |
| `uv lock --check` | Passed, 169 packages |
| Source and wheel distributions | Passed |
| Deterministic public schema export | Passed |
| `git diff --check` | Passed |

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

## Live model and remote evidence

The tracked CrewAI/Ollama initialization-and-resume regression passed on the current implementation
against the local Ollama service with `qwen2.5-coder-7b-16k:latest` for planning and
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

## Acceptance boundary

The implementation and evidence satisfy the declared I04 technical gate and were accepted by
D-039 on 2026-08-27. Promotion into `develop` and the beginning of I05 remain paused until the
engineer-requested cross-baseline conformance audit is complete.
