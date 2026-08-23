# MISHKAN Requirement-to-Responsibility Map

**Status:** Approved — Gate G4
**Version:** 1.0
**Sequence:** SWE-BASICS-BEFORE-CODE 04
**Derived from:** Approved PRD 1.2, SRS 1.3, System Contract 1.2

## 1. Purpose

This document assigns every approved guarantee to one primary responsibility before components are
designed. A responsibility describes logic and state ownership. It is not yet a Python module,
service, daemon, database, or deployment unit.

Rules:

1. Every requirement has exactly one primary responsibility.
2. A responsibility may collaborate with others but cannot delegate away its guarantee.
3. Responsibilities that change for the same reasons are identified as cohesion candidates only;
   Sequence 05 decides structural boundaries.
4. CrewAI is the mandatory production coordination runtime, not an optional provider and not one
   possible implementation among competing MISHKAN runtimes.

## 2. CrewAI runtime boundary

Current CrewAI defines Agents, Tasks, Crews, and Processes for autonomous team collaboration, and
Flows for event-driven execution paths, state, persistence, resumption, conditional routing, and
native Crew integration. MISHKAN uses those production primitives directly.

MISHKAN responsibilities define the product-specific organization, plan, authorization, evidence,
acceptance, repository, and external-effect contracts around that runtime. They must not recreate a
parallel agent, crew, task-process, or flow engine. Deterministic substitutions exist only at test
boundaries and can never be selected as a production runtime.

Official basis:

- [CrewAI documentation](https://docs.crewai.com/) — Agents, Flows, and Tasks & Processes.
- [CrewAI introduction](https://docs.crewai.com/core-concepts/Agents) — Crews for autonomous
  collaboration and Flows for structured control with native Crew integration.

## 3. Primary responsibility catalogue

### RSP-001 — Resolve effective operating context

Validate and normalize layered configuration before work begins; expose value provenance while
keeping credentials secret; refuse undocumented provider, service, repository, worker, or mode
selection.

**Owns:** effective configuration identity, layer provenance, configured route catalogue.

### RSP-002 — Maintain contract identity and compatibility

Issue globally unique identities, preserve unambiguous timestamps and timezone rendering, validate
persisted schema versions, and refuse unsupported automatic mutation.

**Owns:** identity rules, timestamp semantics, schema-version compatibility decisions.

### RSP-003 — Preserve interface and error semantics

Ensure interactive and programmatic paths expose equivalent state-changing capabilities and apply
the same validation and authorization; return stable machine-readable failures.

**Owns:** interface parity rules and error catalogue semantics.

### RSP-004 — Establish repository evidence and lineage

Identify the repository and base revision, discover project characteristics from cited evidence,
represent unknowns honestly, preserve discovery history, and validate produced revision lineage.

**Owns:** repository binding, discovery revision, evidence citations, produced-revision lineage.

### RSP-005 — Compose and revise repository-specific plans

Transform an objective and evidence into a normalized task graph with roles, dependencies,
contracts, capabilities, bounds, and completion criteria. Revise plans through explicit linked
versions while preserving compatible accepted work.

**Owns:** plan contents, fingerprint, revision reason and diff, result-preservation analysis.

### RSP-006 — Decide and record authorization

Evaluate exact plan and capability requests against visible versioned policy using deterministic
matching and precedence; issue allow, require-approval, or deny; bind approvals and revocations to
their exact scopes.

**Owns:** policy evaluation semantics, authorization records, approval evidence, revocation effect.

### RSP-007 — Govern organization definitions

Load and validate the versioned 32-role organization and 15-outcome catalogue; define functional
roles, admissible tools, delegation eligibility, and the subset relevant to a plan.

**Owns:** organization identity, roster, role metadata, outcome contracts, declared tool catalogue.

### RSP-008 — Coordinate production work through CrewAI

Materialize authorized organization and plan definitions as CrewAI Agents, Tasks, Crews, Processes,
and Flows; drive bounded task eligibility, delegation, parallel barriers, retry, cancellation,
resumption, and terminal outcomes using CrewAI's production runtime.

**Owns:** binding from accepted MISHKAN definitions to CrewAI primitives and the resulting runtime
execution lineage. It does not own policy decisions or acceptance validity.

### RSP-009 — Enforce independent evaluation and reporting

Reject producer/evaluator and orchestrator/reporter conflicts, require downstream independent
evaluation for workspace-changing artifacts, and produce the versioned multi-task run report.

**Owns:** separation decisions, evaluation assignment, reporting contract completion.

### RSP-010 — Validate and accept task results

Validate result envelopes and task-specific contracts, confirm producer, attempt, plan, policy, and
revision context, persist accepted results before releasing dependencies, and ignore duplicate
completion effects.

**Owns:** accepted-result record, rejection evidence, accepted attempt uniqueness.

### RSP-011 — Enforce capability effects

Mediate filesystem, command, network, credential, repository, deployment, migration, and future
capabilities at deterministic boundaries. Resolve actual targets, apply exact granted scopes and
isolation limits, validate tool calls and results, preserve uncertain effects, and prevent actors
from enlarging authority.

**Owns:** typed invocation enforcement, resolved-target decision, late credential release, effect
outcome, retry-safety decision, validated tool-result envelope.

### RSP-012 — Sanitize content and record security evidence

Inspect content at persistence and downstream boundaries, contain credentials, produce reviewable
state-change evidence, and emit non-secret audit facts for security decisions.

**Owns:** inspection outcome, redaction/block evidence, state-change evidence envelope.

### RSP-013 — Retrieve attributed project context

Classify episodic, semantic, structural, or literal questions; select applicable sources; attach
provenance and staleness; and declare safe degraded operation when optional sources are absent.

**Owns:** retrieval intent, source selection, context attribution, degraded-context limitation.

### RSP-014 — Govern skill sources, trust, and lifecycle

Resolve configured skill sources and precedence; validate compatibility, provenance locks, trust,
inspection and quarantine decisions; stage and atomically activate policy-authorized mutations;
preserve update, archival, reset, and restoration lineage.

**Owns:** skill-source catalogue, provenance lock, inspection and quarantine record, staged mutation,
active-version pointer, pin and archival state, recoverable lineage.

### RSP-015 — Persist and project events and evidence

Record typed ordered events for every material transition, retain protected evidence, expose
resumable live delivery and bounded snapshots, detect transport gaps, and provide filtered progress.

**Owns:** durable event order, current-state projection, retention/hold application, subscription
cursor and gap evidence.

### RSP-016 — Govern schedules and idempotent triggers

Create and manage persistent timezone-aware schedules, map each trigger occurrence to at most one
run, apply overlap policy, expose history, and preserve equivalent external-scheduler invocation.

**Owns:** schedule state, trigger-occurrence identity, overlap decision request, schedule history.

### RSP-017 — Coordinate bounded remote execution

Enable distributed mode explicitly; manage worker enrollment, identity, capability, heartbeat,
leases, immutable task envelopes, revision checks, recovery, and exactly-once accepted completion.

**Owns:** worker and enrollment records, lease state, task envelope, completion-delivery evidence.

### RSP-018 — Assure release quality and operating limits

Define and verify platform support, local independence, startup and throughput thresholds, advisory
latency, bounded monitoring, coverage, fault injection, and secret-containment acceptance gates.

**Owns:** reference environment definition, acceptance measurements, release-quality evidence.

### RSP-019 — Enforce production runtime constraints

Ensure production coordination uses supported CrewAI 1.x, test doubles cannot become production
mode, product interfaces remain within accepted scope, and policy-governed migration semantics are
preserved while concrete persistence awaits its ADR.

**Owns:** production runtime conformance and technical-constraint compliance evidence.

### RSP-020 — Discover, apply, and learn procedural skills

Validate the portable skill contract; expose Level 0 catalogue metadata; load Level 1 instructions
and Level 2 content progressively; select explicit or applicable skills and bundles for authorized
CrewAI tasks; record hit, partial, and miss outcomes; turn teaching or configured evidence into
staged Research-team proposals.

**Owns:** skill package semantics, selection and composition evidence, loaded-content record, usage
outcome, miss aggregation, learning request and proposal lineage. It does not activate its own
proposal or grant the consuming task capabilities.

### RSP-021 — Resolve and expose atomic tools

Discover versioned tools from configured native and external sources; validate contracts,
namespaces, availability, adapters, and external schema state; resolve nested toolsets into immutable
registry snapshots; search metadata without eagerly loading every schema; bind exact eligible tools
to authorized CrewAI agents without creating another tool-calling runtime.

**Owns:** tool and toolset definitions, source and adapter provenance, registry snapshot, collision
and drift decisions, availability result, task tool binding, CrewAI tool representation. It does not
authorize or dispatch a call.

## 4. Requirement ownership matrix

Each range is inclusive. No requirement appears in more than one row.

| Primary responsibility | Requirements |
|---|---|
| RSP-001 | SYS-001–003 |
| RSP-002 | SYS-004–005, NFR-007 |
| RSP-003 | SYS-006–007 |
| RSP-004 | PRJ-001–006 |
| RSP-005 | PRJ-007, PLN-001–004, PLN-009–011, ORG-012 |
| RSP-006 | PLN-005–008, SAF-003–006 |
| RSP-007 | ORG-001–004, ORG-010–011 |
| RSP-008 | ORG-009, RUN-001–005, RUN-007, RUN-009, RUN-011 |
| RSP-009 | ORG-005–008 |
| RSP-010 | RUN-006, RUN-008, RUN-010 |
| RSP-011 | SAF-001–002, SAF-007, SAF-011, SAF-013, TOL-012–022 |
| RSP-012 | SAF-008–010, SAF-012, OBS-008 |
| RSP-013 | KNW-001–005 |
| RSP-014 | KNW-006, SKL-006–008, SKL-016–025 |
| RSP-015 | RUN-012, OBS-001–007 |
| RSP-016 | AUT-001–007 |
| RSP-017 | DST-001–010 |
| RSP-018 | NFR-001–006, NFR-008–010 |
| RSP-019 | TC-001–007 |
| RSP-020 | SKL-001–005, SKL-009–015 |
| RSP-021 | TOL-001–011, TOL-023–025 |

## 5. Error ownership matrix

| Primary responsibility | Error codes |
|---|---|
| RSP-001 | ERR-CFG-001 |
| RSP-004 | ERR-PRJ-001, ERR-REV-001 |
| RSP-005 | ERR-PLN-001 |
| RSP-006 | ERR-PLN-002, ERR-POL-001, ERR-POL-002 |
| RSP-009 | ERR-ROL-001 |
| RSP-010 | ERR-OUT-001, ERR-RUN-002 |
| RSP-013 | ERR-DEP-001 |
| RSP-008 | ERR-RUN-001, ERR-DEP-002 |
| RSP-012 | ERR-SEC-001 |
| RSP-014 | ERR-SKL-001, ERR-SKL-002 |
| RSP-020 | ERR-SKL-003 |
| RSP-021 | ERR-TOL-001, ERR-TOL-002, ERR-TOL-005 |
| RSP-011 | ERR-TOL-003, ERR-TOL-004 |
| RSP-016 | ERR-SCH-001 |
| RSP-017 | ERR-WRK-001 |
| RSP-002 | ERR-VER-001 |

## 6. Contract ownership matrix

| Primary responsibility | Contract promises and invariants |
|---|---|
| RSP-001 | CTR-001 |
| RSP-002 | INV-001, INV-020 |
| RSP-003 | CTR-011 |
| RSP-004 | INV-008 |
| RSP-005 | CTR-002, CTR-004, INV-007 |
| RSP-006 | CTR-003, INV-002, INV-004–006 |
| RSP-008 | CTR-006, INV-009–010, INV-018 |
| RSP-009 | CTR-008, INV-014 |
| RSP-010 | CTR-005, INV-011–013 |
| RSP-011 | INV-003, INV-015, INV-017, INV-028–030 |
| RSP-012 | CTR-007, INV-016 |
| RSP-013 | CTR-010, INV-021 |
| RSP-014 | INV-025–026 |
| RSP-015 | CTR-009, INV-019 |
| RSP-016 | INV-022 |
| RSP-017 | CTR-012, INV-023 |
| RSP-020 | CTR-013, INV-024 |
| RSP-021 | CTR-014, INV-027 |

`INV-003` is primarily enforced at the capability boundary by RSP-011; RSP-006 supplies the
authorization decision but does not enforce its own decision.

## 7. Required handoffs

| Producer | Consumer | Required handoff |
|---|---|---|
| RSP-001 | RSP-005–021 | Effective configuration identity and provenance |
| RSP-004 | RSP-005, RSP-010, RSP-017 | Repository evidence, base revision, and lineage |
| RSP-005 | RSP-006 | Exact normalized plan fingerprint and capability requests |
| RSP-006 | RSP-008, RSP-011, RSP-016, RSP-017 | Durable authorization decision and exact scope |
| RSP-007 | RSP-005, RSP-008, RSP-009 | Versioned roles, outcomes, tools, and separation metadata |
| RSP-007 | RSP-021 | Role tool eligibility and declared toolset references |
| RSP-021 | RSP-005, RSP-008, RSP-011 | Registry snapshot, exact task binding, and CrewAI tool representation |
| RSP-008 | RSP-010 | CrewAI task result plus execution lineage |
| RSP-011 | RSP-010, RSP-012 | Effect outcome and resolved target evidence |
| RSP-009 | RSP-010 | Independent evaluation result |
| RSP-010 | RSP-008, RSP-015 | Durable acceptance or rejection evidence |
| RSP-013 | RSP-005, RSP-008 | Attributed project context |
| RSP-014 | RSP-020 | Eligible skill catalogue, trust state, and active version |
| RSP-020 | RSP-005, RSP-008 | Selected skill content, composition, and usage evidence |
| RSP-020 | RSP-014 | Staged skill proposal or lifecycle request; never self-activation |
| RSP-015 | Every responsibility | Durable event acknowledgement and current-state projection |
| RSP-016 | RSP-005, RSP-008 | Idempotent run trigger and schedule context |
| RSP-017 | RSP-008, RSP-010 | Remote execution and completion-delivery evidence |
| RSP-018–019 | Release decision | Acceptance and conformance evidence |

## 8. Cohesion candidates for Sequence 05

These groupings are hypotheses to test with behavior and C4 models, not approved components:

| Candidate responsibility family | Responsibilities | Shared reason to change |
|---|---|---|
| Definition and context | RSP-001–004, RSP-007 | Configuration, schemas, organization, and repository evidence evolve |
| Planning and authority | RSP-005–006 | Plan semantics and policy decisions evolve together but require an enforcement boundary |
| CrewAI coordination and acceptance | RSP-008–010 | Runtime orchestration, independent evaluation, and accepted completion interact closely |
| Tools, effects, and evidence | RSP-011–012, RSP-015, RSP-021 | Tool resolution, enforcement, sanitation, audit, and durable reconstruction protect every action |
| Context and skills | RSP-013–014, RSP-020 | Retrieval, procedural selection, and skill lifecycle share provenance but preserve activation separation |
| Automation and distribution | RSP-016–017 | Scheduled and remote triggers extend execution without changing acceptance semantics |
| Product conformance | RSP-018–019 | Release gates and fixed technical constraints change with supported product versions |

Sequence 05 must preserve CrewAI as the production coordination runtime even if responsibilities
around it are grouped differently.
