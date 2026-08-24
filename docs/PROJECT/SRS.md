# MISHKAN Software Requirements Specification

**Status:** Approved — Gate G2
**Version:** 1.4
**Derived from:** Approved PRD 1.2
**Normative vocabulary:** MUST, MUST NOT, SHOULD, MAY follow RFC 2119 meanings

Version 1.4 clarifies the tool model without changing the approved product scope. It treats
general-purpose file, terminal/process, web, and browser surfaces as real tools whose concrete
inputs are governed at invocation time; makes adapter presence part of availability; and removes
any requirement for a universal capability taxonomy or one tool contract per ecosystem command.

## 1. Purpose and scope

This SRS defines the observable behavior and constraints of MISHKAN product version 1. It does not
assign responsibilities to components or prescribe an implementation except where an explicit
engineer-selected technical constraint is recorded in §17.

Core release requirements cover local, interactive, and headless operation on one machine.
Distributed requirements are identified as post-core and do not block acceptance of the core
release.

## 2. Actors

| Actor | Description |
|---|---|
| Engineer | Submits objectives, controls authorization policy, provides approval where policy requires it, and reviews work |
| Engineering lead | Reviews organization-wide progress, evidence, policy compliance, and outcomes |
| Operator | Configures execution resources, credentials, schedules, retention, and approved remote capacity |
| External scheduler | Requests an already defined run through an idempotent interface |
| Remote worker | Executes an assigned immutable task under coordinator-issued identity and policy |
| External service | Provides optional inference, context, knowledge, or structure capabilities |

## 3. General system behavior

### SYS-001 — Explicit configuration

The system MUST load one versioned effective configuration before accepting a run.

**Verification:** Starting without a resolvable configuration produces a configuration error and no
run record.

### SYS-002 — Configuration provenance

The system MUST expose the source and precedence of every effective configuration layer without
revealing resolved credentials.

**Verification:** A configuration inspection reports each contributing layer and masks secrets.

### SYS-003 — No hidden operational routing

The system MUST NOT select an inference provider, remote service, repository, worker, or execution
mode that is absent from the effective configuration or accepted plan.

**Verification:** Removing each value causes validation failure rather than an undocumented default.

### SYS-004 — Stable identifiers

Every project, repository revision, plan, run, task, approval, event, artifact, schedule, and worker
record MUST have a globally unique identifier.

### SYS-005 — Time representation

Persisted timestamps MUST represent an unambiguous instant. User-facing rendering MUST identify the
applied timezone.

### SYS-006 — Interface parity

Every state-changing capability exposed through an interactive interface MUST also be available
through a documented programmatic interface with the same validation and authorization behavior.

### SYS-007 — Error classification

Expected user, policy, dependency, and system failures MUST use stable machine-readable error codes
and MUST NOT be reported as successful runs.

## 4. Repository establishment

### PRJ-001 — Repository identity

A run MUST target exactly one repository and record one immutable base revision identifier.
Authorized actions MAY produce later revisions, each of which MUST retain lineage to that base and
the responsible task.

### PRJ-002 — Revision validation

The system MUST verify the bound revision before planning and before accepting each externally
executed task result.

### PRJ-003 — Repository discovery

Initialization MUST discover repository characteristics relevant to planning, including languages,
frameworks, test surfaces, deployment surfaces, data-change surfaces, and declared project policy.

### PRJ-004 — Evidence-based discovery

Every discovered characteristic used to select work MUST include evidence identifying its source.

### PRJ-005 — Unknown characteristic handling

The system MUST represent an undiscovered or ambiguous repository characteristic as unknown rather
than infer a definitive value without evidence.

### PRJ-006 — Reinitialization

Reinitializing an existing project MUST preserve accepted run history and MUST produce a new
discovery revision rather than overwriting prior discovery evidence.

### PRJ-007 — Project-specific behavior

Two repositories with materially different discovered characteristics MUST be permitted to produce
different plans for the same named organizational outcome.

## 5. Planning and approval

### PLN-001 — Explicit objective

Every plan MUST identify the engineer-provided objective, repository revision, requested outcome,
and applicable policy revision.

### PLN-002 — Structured plan

Every plan MUST declare its tasks, assigned organizational roles, dependencies, allowed parallelism,
inputs, expected outputs, validation contracts, tool permissions, path scopes, time limits, and
completion conditions.

### PLN-003 — Plan validation

The system MUST reject a plan that references an unknown role, violates role separation, exceeds an
organizational cap, contains an unbounded cycle, lacks an output contract, or requests authority not
granted by policy.

### PLN-004 — Plan identity

The accepted normalized contents of a plan MUST determine a stable plan fingerprint. Any semantic
change MUST produce a different fingerprint.

### PLN-005 — Policy authorization

A plan MUST NOT begin execution until its exact fingerprint is evaluated against the effective
versioned policy. The decision MUST be one of allow, require approval, or deny.

### PLN-006 — Policy pre-authorization

A plan MAY begin without interactive approval when a versioned policy matches its objective class,
repository, workflow outcome, role set, capability authority, path and external-resource scopes,
resource limits, and plan constraints.

### PLN-007 — Pre-authorization mismatch

Any plan property outside an allow rule MUST be re-evaluated. The system MUST pause for interactive
approval when the matching policy requires approval and MUST reject the revision when policy denies
it.

### PLN-008 — Approval evidence

The system MUST persist the authorization decision, deciding identity or policy rule, decision
time, plan fingerprint, policy revision, matched scope, and any interactive approval evidence.

### PLN-009 — Versioned plan evolution

An executing run MAY revise its plan in response to validated evidence. Every semantic revision
MUST create a new plan version and fingerprint, record its reason and difference from the preceding
version, and pass policy authorization before newly introduced work executes.

### PLN-010 — No silent replanning on resume

Resuming an interrupted run MUST use the latest authorized plan version and preserve accepted task
results that remain valid. It MUST NOT hide replanning, discard compatible accepted work, or expand
authority without a recorded policy decision.

### PLN-011 — Reviewability

The engineer MUST be able to inspect every normalized plan version, its authorization decision,
its difference from the preceding version, and the reasons each role, task, dependency, capability,
and completion condition was selected.

## 6. Organization and coordination

### ORG-001 — Versioned organization

The system MUST load one versioned organization definition for a run and persist its identity with
the accepted plan.

### ORG-002 — Version 1 roster

Organization version 1 MUST contain exactly the 32 identities listed in Appendix A.

### ORG-003 — Stable roles, adaptive participation

The organization roster MUST remain stable within version 1, while a plan MUST include only roles
relevant to the repository and objective.

### ORG-004 — Functional roles

Every identity MUST have exactly one functional role: Orchestrator, Specialist, Evaluator,
Reporter, or Advisor.

### ORG-005 — Production and evaluation separation

An identity that produces a task artifact MUST NOT evaluate that artifact for acceptance.

### ORG-006 — Orchestration and reporting separation

An identity that makes the acceptance or routing decision for a task MUST NOT be the Reporter for
that same task.

### ORG-007 — Independent evaluation

Every accepted production artifact that can change the approved workspace MUST receive an
independent evaluation before its run can be declared successful.

### ORG-008 — Structured reporting

Every completed multi-task run MUST produce a report conforming to a versioned report contract.

### ORG-009 — Delegation authority

Only an Orchestrator MAY delegate or reassign planned work, and any reassignment MUST remain within
the accepted plan or trigger replanning.

### ORG-010 — Explicit tool authority

Every participating identity MUST receive an exact resolved set of available tool identities and
versions. A configured shorthand or toolset MAY expand before plan acceptance, but the expansion
MUST be recorded in the plan fingerprint and MUST NOT include tools discovered later.

### ORG-011 — Catalogue outcomes

Organization version 1 MUST expose exactly the 15 named outcomes listed in Appendix B.

### ORG-012 — Adaptive outcome templates

A catalogue outcome MUST define intent, admissible roles, constraints, approvals, inputs, outputs,
and a composition pattern. It MUST NOT require one universal repository-independent task list.

## 7. Run execution and recovery

### RUN-001 — Run lifecycle

A run MUST expose at least: awaiting approval, queued, running, blocked, failed, cancelled, and
completed states.

### RUN-002 — Task lifecycle

A task MUST expose at least: pending, eligible, leased or executing, validating, accepted, rejected,
failed, and cancelled states.

### RUN-003 — Dependency enforcement

A task MUST NOT become eligible until all dependencies declared by the accepted plan have accepted
results.

### RUN-004 — Parallel barriers

A downstream barrier task MUST NOT begin until every required upstream parallel task has reached an
accepted terminal result.

### RUN-005 — Bounded iteration

Every iterative plan section MUST declare a measurable completion condition and maximum iteration
count before approval.

### RUN-006 — Structured task result

Every task result MUST conform to a versioned result envelope and any task-specific output contract
before acceptance.

### RUN-007 — Validation retry

A rejected structured result MAY be retried only within the accepted retry policy. Exhaustion MUST
fail the task with the rejection evidence preserved.

### RUN-008 — Durable task acceptance

An accepted task result MUST be durably persisted before dependent work becomes eligible.

### RUN-009 — Resume boundary

After interruption, execution MUST resume from the earliest task that lacks an accepted result.

### RUN-010 — Duplicate completion

The system MUST accept at most one result for a task attempt. Later duplicate completions MUST be
recorded and ignored.

### RUN-011 — Cancellation

Cancelling a run MUST prevent new task eligibility, request cancellation of executing work, preserve
accepted results, and produce a terminal audit record.

### RUN-012 — Progress

The engineer MUST be able to observe run and task state changes as they occur and query the latest
state after reconnecting.

## 8. Workspace and safety

### SAF-001 — Approved workspace

Every filesystem operation MUST resolve inside the workspace scope accepted with the plan.

### SAF-002 — Resolved-path enforcement

Path authorization MUST evaluate the resolved target, including symbolic links, before access.

### SAF-003 — Least authority

The system MUST deny a tool, path, network destination, credential, or operation unless it is
explicitly authorized by the accepted plan and effective policy.

### SAF-004 — Visible policy

Every policy rule affecting a run MUST come from an inspectable, versioned policy source and be
included in the effective policy fingerprint.

### SAF-005 — Explicit policy evolution

Project or run configuration MUST NOT silently change the effective authorization boundary. Every
policy change MUST create a new version, identify its source and adoption authority, and be applied
only to the scopes it declares. Non-configurable system integrity invariants MUST be specified in
the system contract rather than concealed in a private deny-list.

### SAF-006 — Policy-controlled state changes

Every state-changing capability MUST be declared in the public policy model and configurable as
deny, allow, or require approval with applicable identity, repository, remote, branch, path,
environment, credential, time, and resource scopes. The implementation MUST NOT enforce a hidden
hardcoded action list as product policy.

### SAF-007 — Workspace edits

An agent MAY modify files only inside an approved writable scope and only through an authorized
operation.

### SAF-008 — Reviewable diff

Every accepted state-changing task MUST produce reviewable evidence identifying the intended
change, effective authorization, observed result, and relevant diff or external state reference.

### SAF-009 — Secret handling

Resolved credentials MUST NOT be written to configuration, prompts not requiring them, results,
artifacts, events, logs, snapshots, or diffs.

### SAF-010 — Output inspection

All content MUST pass configured credential and policy inspection before persistence or downstream
use.

### SAF-011 — Isolated execution

Untrusted generated commands MUST execute within the resource, filesystem, process, and network
limits accepted by the plan.

### SAF-012 — Security evidence

Every refused security-relevant action MUST produce a typed audit event containing the responsible
identity, attempted capability, policy decision, and non-secret reason.

### SAF-013 — No prompt-only enforcement

A safety invariant MUST be enforced by a deterministic boundary. An instruction to an AI actor is
not sufficient enforcement.

## 9. Knowledge

### KNW-001 — Context classes

The system MUST distinguish recent episodic context, semantic project knowledge, structural
repository knowledge, and literal repository content.

### KNW-002 — Appropriate retrieval

A context request MUST identify the class of question and MAY query only sources applicable to that
class before falling back to literal content.

### KNW-003 — Attribution

Every returned knowledge item MUST identify its source, scope, retrieval time, and confidence or
ranking basis where available.

### KNW-004 — Staleness

Knowledge whose validity depends on repository state MUST identify the source revision or expose
that it may be stale.

### KNW-005 — Degraded operation

Unavailable optional knowledge sources MUST NOT fail work that can safely proceed without them. The
system MUST expose the unavailable source and resulting limitation.

### KNW-006 — Promotion approval

Knowledge MUST NOT become permanent cross-project guidance without engineer approval and provenance.

## 10. Skills and procedural memory

### SKL-001 — Skill contract

A skill MUST be a versioned portable package with one `SKILL.md` containing machine-readable
identity and discovery metadata plus human-readable operating instructions. A skill MAY include
scripts, references, templates, examples, and tests addressed from that manifest.

### SKL-002 — Distinct semantics

The system MUST distinguish a skill from a tool, prompt, organizational role, workflow outcome,
and repository-specific plan. Selecting a skill MUST NOT grant capabilities or replace CrewAI
coordination, plan authorization, or deterministic enforcement.

### SKL-003 — Level 0 catalogue

The system MUST be able to list, search, and inspect applicable skill identity, summary, version,
source, trust state, and activation status without loading the skill instructions or supporting
content.

### SKL-004 — Level 1 instructions

After selecting a skill, the system MUST load its `SKILL.md` completely before applying the skill.
Selection and load MUST be recorded against the consuming task.

### SKL-005 — Level 2 on-demand content

Supporting skill content MUST be loaded only when referenced by the selected instructions and
needed for the current task. The record MUST identify each loaded item and its content fingerprint.

### SKL-006 — Configured sources

The system MUST discover skills from versioned configured sources that MAY include bundled,
project, operator-managed external, community, URL, or source-control locations. Source locations
and enablement MUST NOT depend on private hardcoded filesystem paths.

### SKL-007 — Deterministic precedence

Skill source precedence MUST be public, versioned, and configurable. Two applicable skills with
the same identity and unresolved equal precedence MUST cause an explicit ambiguity failure rather
than nondeterministic selection.

### SKL-008 — Compatibility and dependencies

Before selection, the system MUST evaluate declared platform, required tools, fallback tools,
environment constraints, organization version, and other declared dependencies. An unmet required
condition MUST prevent activation for the task and identify the condition.

### SKL-009 — Explicit and automatic invocation

The engineer MUST be able to invoke an active skill explicitly through a documented slash form and
an equivalent programmatic interface. The system MAY select an active skill automatically only
when its declared applicability matches the task and the selection is visible in the plan or task
evidence.

### SKL-010 — Composable bundles

The system MUST support versioned named bundles of skills and MUST validate the configured maximum
stack size, order, compatibility, and conflicts before applying a bundle. Bundles enrich a task;
they MUST NOT become hidden static workflow definitions.

### SKL-011 — Usage outcomes

Every attempted skill use MUST emit a typed `hit`, `partial`, or `miss` outcome with skill version,
task class, consuming identity, and non-secret reason.

### SKL-012 — Durable miss evidence

The system MUST retain policy-scoped miss and partial-use evidence across restarts and MUST expose
aggregates by task class without silently turning a configured threshold into an activation grant.

### SKL-013 — Explicit learning request

The engineer MUST be able to request learning through a documented `/learn <source>` interaction
and an equivalent programmatic interface using supplied text, files, URLs, repository evidence, or
execution evidence. The result MUST be a staged proposal or an attributable refusal, never an
immediately trusted active skill.

### SKL-014 — Evidence-triggered proposal

When a configured miss or correction rule is satisfied, the system MAY initiate an authorized
Research-team proposal using the canonical research roles. The triggering evidence and resulting
proposal lineage MUST be recorded.

### SKL-015 — Knowledge-base skills

When supplied source material cannot be safely or usefully embedded in a skill package, the system
MUST allow the skill to retain attributed retrieval instructions and references instead of copying
the entire source.

### SKL-016 — Staged mutations

Create, patch, edit, delete, archive, restore, and supporting-file changes MUST execute as typed
stateful capabilities under effective policy. A proposed mutation MUST remain staged and durable
across restart until allowed, approved, denied, rejected, superseded, or expired.

### SKL-017 — Patch-first evolution

An AI-authored update SHOULD express the smallest reviewable patch against an identified base
version. A full replacement MUST identify why a patch is insufficient and MUST preserve recoverable
lineage.

### SKL-018 — Atomic activation

A skill version and all content it references MUST become active atomically after required
validation, inspection, and policy decision. Failure MUST leave the previously active version
unchanged.

### SKL-019 — Provenance lock

Every installed or active skill version MUST expose source, resolved revision where applicable,
content fingerprints, dependency fingerprints, author claims, acquisition time, trust state, scan
result, and activation decision in a durable provenance record.

### SKL-020 — Mandatory inspection

Before activation, acquired or changed skill content MUST be inspected for configured security,
privacy, Unicode, credential, prompt-injection, and destructive-action findings. Inspection rules
and severity policy MUST be versioned and inspectable rather than hidden in a private list.

### SKL-021 — Quarantine

A skill with an unresolved finding at or above the configured quarantine threshold MUST remain
inactive. Quarantine, override eligibility, and any authorized override MUST be explicit policy
decisions with non-secret evidence.

### SKL-022 — Update and reset

The system MUST detect available updates for configured sources without activating them
automatically, show their provenance and difference, and support a policy-governed reset to a known
source version.

### SKL-023 — Restoration

Rejecting, archiving, deleting, or superseding a skill MUST NOT erase its accepted history. The
engineer MUST be able to restore an eligible prior version through the same validation and policy
path as activation.

### SKL-024 — Lifecycle curation

The system MUST maintain last-use and usage-outcome evidence, MAY propose archival of stale skills
under configured rules, MUST protect pinned skills from automated archival, and MUST NOT
automatically destroy archived skill history.

### SKL-025 — No hosted marketplace dependency

Core skill discovery, creation, review, activation, use, and restoration MUST operate without a
MISHKAN-hosted marketplace. Community acquisition MAY use configured repositories, URLs, or hubs
but MUST pass the same provenance, inspection, and policy path as every other external source.

## 11. Tools and atomic capabilities

### TOL-001 — Distinct semantics

The system MUST distinguish a tool from a skill, prompt, organizational role, workflow outcome,
and task. A tool is one typed atomic capability; registering or selecting it MUST NOT grant
authority or create a competing agent execution loop.

### TOL-002 — Versioned tool contract

Every tool MUST declare a namespaced identity, version, summary, input schema, result schema,
effect class, source, availability conditions, timeout behavior, idempotency semantics, applicable
target scopes, credential references without values, and provenance fingerprint.

### TOL-003 — Configured sources

The registry MUST discover tools only from versioned configured sources that MAY include bundled
implementations, project definitions, operator-managed adapters, and external protocol servers.
Source locations, enablement, and transports MUST NOT depend on private hardcoded values.

### TOL-004 — Namespacing and collision

Tool identities from different sources MUST remain unambiguous. An unresolved identity collision
MUST prevent the conflicting tools from entering an accepted registry snapshot and MUST expose all
claiming sources.

### TOL-005 — Toolsets

The system MUST support versioned named toolsets that compose exact tools or other toolsets.
Toolset ordering, inclusion, exclusion, availability rules, and nesting bounds MUST be public
configuration, and resolution cycles MUST be rejected. A toolset MAY constrain discovery or role,
project, platform, session, or task eligibility, but MUST NOT grant authority by itself.

### TOL-006 — Deferred discovery

The system MUST support listing and searching minimal tool metadata without loading every full
schema into task context. The exact input and result schemas MUST be loaded before a tool is bound
to a task or invoked.

### TOL-007 — Availability

The system MUST verify that a concrete invocable adapter is registered at the intended execution
location and evaluate the tool's configured runtime, platform, credential-reference, service,
resource, health, and dependency conditions before task binding. An abstract interface, adapter
type, or contract without a runnable registered implementation MUST be unavailable. Unavailability
MUST be visible and MUST NOT be represented as authorization denial or successful execution.

### TOL-008 — Registry snapshot

Every accepted plan MUST identify the immutable tool-registry snapshot used to resolve its tools.
Registry changes MUST create a new snapshot and MUST NOT silently alter an accepted task.

### TOL-009 — Task binding

A task MAY use only exact tools present in its accepted plan, permitted for the assigned role,
available in the bound registry snapshot, and supported by the assigned execution location.
Repository evidence, outcome constraints, role eligibility, and toolsets MAY guide selection but
MUST resolve to those exact tool identities before plan acceptance.

### TOL-010 — No implicit authority expansion

Enabling a source, tool, toolset, adapter, credential, or external server MUST NOT grant its tools to
an existing role, plan, task, run, or worker. Expansion MUST pass normal plan and policy decisions.

### TOL-011 — CrewAI tool binding

Every production tool selected for an agent MUST be exposed through a currently supported CrewAI
tool interface. MISHKAN MUST NOT implement a competing production model tool-calling loop.

### TOL-012 — Invocation envelope

Every invocation MUST identify the run, task attempt, acting identity, tool and contract version,
registry and plan fingerprints, normalized arguments, declared targets, applicable authorization,
deadline, and unique call identifier.

### TOL-013 — Input validation

Arguments MUST validate against the exact bound input schema before credentials are resolved or the
tool is dispatched. Invalid arguments MUST produce no tool effect.

### TOL-014 — Authorization before dispatch

The system MUST confirm exact task binding and an effective allow decision or required approval for
the tool, effect class, targets, and scopes before dispatch. Tool source or availability MUST NOT
serve as authorization evidence.

### TOL-015 — Late credential resolution

Credential values MUST be resolved only after validation and authorization, supplied only to the
selected adapter fields that require them, and excluded from model-visible arguments and persisted
evidence.

### TOL-016 — Resolved targets

Filesystem, network, repository, environment, remote, and external-resource scopes MUST be checked
against the actual resolved effect targets at the deterministic invocation boundary.

### TOL-017 — Execution constraints

Each call MUST apply the plan- and policy-authorized timeout, resource, isolation, network, and
concurrency constraints for its effect class. If a required constraint cannot be enforced, the call
MUST be refused before dispatch.

### TOL-018 — Result envelope

Every completed, failed, cancelled, or uncertain tool call MUST return a versioned envelope with
call identity, status, timing, non-secret output or artifact references, actual external-state
references where applicable, retryability, and attributable adapter evidence.

### TOL-019 — Result validation

The result envelope and declared result schema MUST validate before tool output enters task context
or accepted evidence. Invalid output MUST be contained and reported as a tool-contract failure.

### TOL-020 — Retry and idempotency

Automatic retry MUST occur only within accepted bounds and when the tool contract establishes
idempotency, a deduplication key, or an authorized compensation rule. An uncertain state-changing
effect MUST NOT be repeated automatically.

### TOL-021 — Timeout and cancellation

A timeout or cancellation MUST stop local work where possible and record whether the external
effect is absent, completed, or uncertain. Uncertainty MUST block dependent acceptance until
reconciled.

### TOL-022 — Invocation evidence

Selection, binding, authorization, start, completion, refusal, retry, timeout, schema failure, and
uncertain effect MUST produce typed non-secret evidence attributable to the exact tool version and
task attempt.

### TOL-023 — External protocol discovery

For each configured external tool server, the system MUST discover only capabilities supported by
the negotiated session, namespace them by server identity, apply configured filtering, and adapt
their schemas through the supported CrewAI integration boundary.

### TOL-024 — External lifecycle and drift

Connection start, readiness, reconnect, shutdown, authentication change, and schema drift for an
external tool source MUST be observable. Drift from a bound registry snapshot MUST block new calls
through that binding until it is reconciled or replanned.

### TOL-025 — Policy-governed lifecycle

Adding, enabling, disabling, updating, removing, or changing the precedence of a tool source,
adapter, tool, or toolset MUST be a typed stateful capability. Changes MUST be validated and become
effective atomically under policy without rewriting prior registry snapshots or call evidence.

### TOL-026 — General-purpose execution tools

A general-purpose terminal or process tool MAY accept a repository-discovered executable,
task-runner target, arguments, working directory, and environment as validated inputs; the system
MUST NOT require a distinct tool contract for every command. The accepted plan and effective
policy MUST constrain the actual executable or command pattern, arguments, working directory,
environment, network access, resources, and effect class at the invocation boundary. Discovery of
a command MUST NOT grant authority to run it.

### TOL-027 — Repository-controlled extensions

A repository-controlled tool, toolset, source, server, or plugin definition MAY contribute a
candidate only through a configured source and declared trust path. Repository content MUST NOT
activate its own extension, approve its own lifecycle change, expand an existing task binding, or
modify effective policy without the normal validation and authorization decisions.

## 12. Events, evidence, and retention

### OBS-001 — Typed events

Every state transition, approval, rejection, retry, security decision, knowledge promotion, worker
lease, and schedule trigger MUST produce a versioned typed event.

### OBS-002 — Event identity and order

Every event MUST include an identifier, source, timestamp, relevant entity identifiers, and an order
that permits reconstruction within one run.

### OBS-003 — Durable evidence

Accepted plans, approvals, task results, reports, diffs, and terminal run states MUST be durably
stored before being reported as accepted or complete.

### OBS-004 — Live subscription

The system MUST provide a resumable live event subscription and a bounded current-state snapshot.

### OBS-005 — Filtering

The engineer MUST be able to filter persisted and live events by type, run, task, identity, team,
time range, and security relevance.

### OBS-006 — Event transport loss

Temporary loss of the live event transport MUST NOT invalidate an otherwise safe task. Events MUST
be buffered within a configured bound and gaps MUST be detectable.

### OBS-007 — Retention

Retention policy MUST be versioned, inspectable, and applied without removing evidence protected by
an active hold or incomplete run.

### OBS-008 — Secret-safe observability

Event and snapshot serialization MUST apply the same secret-handling guarantees as other persisted
outputs.

## 13. Headless operation and scheduling

### AUT-001 — Headless equivalence

Headless execution MUST apply the same planning, approval, policy, validation, and evidence rules as
interactive execution.

### AUT-002 — Persistent schedule

A schedule MUST survive control-process restart and identify its project, outcome, input, timezone,
trigger, pre-authorization policy, and status.

### AUT-003 — Timezone

A recurring schedule MUST use an identified timezone and MUST reject an ambiguous or invalid
timezone.

### AUT-004 — Overlap prevention

The system MUST prevent concurrent active runs of the same outcome for the same project unless an
explicit policy permits overlap.

### AUT-005 — Idempotent trigger

Repeating the same schedule trigger request MUST NOT create more than one accepted run for that
trigger occurrence.

### AUT-006 — Schedule control

The operator MUST be able to create, inspect, trigger immediately, pause, resume, and remove a
schedule, and inspect its run history.

### AUT-007 — External scheduler compatibility

An external scheduler MUST be able to invoke the same idempotent run interface used internally.

## 14. Distributed execution — post-core

### DST-001 — Explicit distributed mode

Remote execution MUST occur only when the operator explicitly enables distributed mode and
configures its shared coordination store.

### DST-002 — Worker identity

Every worker MUST have an individually issued, renewable, and revocable identity.

### DST-003 — Bounded enrollment

Worker enrollment authority MUST be single-use, expire within a configured short interval, and be
invalid after successful use or revocation.

### DST-004 — Capability advertisement

A worker MUST advertise its supported capabilities and resource limits. It MUST receive only tasks
whose requirements it satisfies.

### DST-005 — Versioned task envelope

A remote task assignment MUST be immutable after issuance and include the repository revision, plan fingerprint, task contract,
role definition fingerprint, inputs, policy fingerprint, path scope, resources, and deadline.

### DST-006 — Lease

A remote task claim MUST have a renewable bounded lease. Expiry MUST make the task eligible for
recovery without accepting more than one completion.

### DST-007 — Heartbeat and loss

Worker availability MUST be derived from bounded heartbeats. Worker loss MUST be visible and MUST
trigger lease-based recovery.

### DST-008 — Revision mismatch

A worker MUST refuse execution or completion when its repository revision differs from the task
envelope.

### DST-009 — Worker authority

A worker MUST NOT expand its task authority or authorize its own plan. It MAY perform a stateful
operation only when that exact capability and scope are present in its coordinator-issued envelope
and the effective policy decision permits it.

### DST-010 — Exactly-once acceptance

The coordinator MUST accept at most one valid completion for a task attempt despite retries,
duplicate delivery, worker loss, or partial network failure.

## 15. Non-functional requirements

### NFR-001 — Supported core platforms

The core release MUST run on supported Linux and macOS versions. Windows is not a version 1 target.

### NFR-002 — Local independence

The core local acceptance workflow MUST complete without a paid hosted service.

### NFR-003 — Startup

On the reference local environment, configuration validation and readiness reporting MUST complete
within 10 seconds, excluding an explicitly requested model download or repository scan.

### NFR-004 — Event capacity

The local control plane MUST accept at least 100 valid events per second for 60 seconds without an
undetected loss.

### NFR-005 — Advisory latency

Any synchronous pre-operation advisory MUST return a decision within 500 milliseconds at the 95th
percentile on the reference local environment or permit the safe fallback and emit a timeout event.

### NFR-006 — Bounded memory

Live monitoring clients MUST maintain configured bounded event buffers and remain responsive when
the event production rate exceeds display capacity.

### NFR-007 — Compatibility

Every persisted contract MUST declare a schema version. An unsupported version MUST fail with an
explicit compatibility error and MUST NOT be migrated automatically.

### NFR-008 — Test coverage

Configuration, coordination, planning, workflow execution, recovery, approval, and safety modules
MUST maintain at least 80 percent branch coverage.

### NFR-009 — Fault testing

Release acceptance MUST include repeatable interruption tests for process loss, optional-service
loss, event-transport loss, and—when distributed mode is released—worker and network loss.

### NFR-010 — Secret-free diagnostics

Automated tests MUST verify that representative credentials cannot enter configuration output,
events, logs, snapshots, artifacts, reports, or diffs.

## 16. Error behavior

| Code | Condition | Required result |
|---|---|---|
| ERR-CFG-001 | Effective configuration is absent, malformed, or incompatible | Refuse the requested operation; identify the invalid field or version |
| ERR-PRJ-001 | Repository or revision cannot be established | Do not plan or run; report evidence failure |
| ERR-PLN-001 | Plan violates schema, organization, or policy | Reject plan with every detected violation |
| ERR-PLN-002 | Plan has no valid authorization decision, or required approval is absent | Remain awaiting decision or approval; do not execute |
| ERR-POL-001 | Requested authority is not granted | Refuse before action and emit audit evidence |
| ERR-POL-002 | Equally specific and prioritized policy rules conflict | Deny without selecting a rule; expose the conflict |
| ERR-ROL-001 | Production/evaluation or orchestration/reporting conflict | Reject task assignment |
| ERR-OUT-001 | Result fails its output contract | Reject, retry within policy, then fail with evidence |
| ERR-REV-001 | Repository revision differs from accepted plan | Block execution or completion and require reconciliation |
| ERR-RUN-001 | Run is interrupted | Preserve accepted results and expose resumable state |
| ERR-RUN-002 | Duplicate result is received | Preserve first accepted result; record and ignore duplicate |
| ERR-DEP-001 | Optional context service is unavailable | Continue only permitted degraded work and expose limitation |
| ERR-DEP-002 | Required inference or coordination dependency is unavailable | Do not start affected work; expose retryable failure |
| ERR-SEC-001 | Secret-like content reaches a persistence boundary | Block persistence and emit non-secret security evidence |
| ERR-SKL-001 | Skill package, manifest, reference, dependency, or mutation contract is invalid | Keep the skill inactive; report every detected validation failure |
| ERR-SKL-002 | Skill provenance, trust, inspection, quarantine, or activation decision is insufficient | Keep the skill inactive or preserve the prior active version; emit non-secret evidence |
| ERR-SKL-003 | Skill selection is ambiguous or incompatible with the task context | Do not apply the skill; identify the conflicting source or unmet condition |
| ERR-TOL-001 | Tool definition, schema, adapter, toolset, or registry entry is invalid | Exclude it from the accepted registry snapshot and report every detected failure |
| ERR-TOL-002 | A required tool or source is unavailable | Do not bind or dispatch it; expose the missing condition and permitted fallback if any |
| ERR-TOL-003 | Tool arguments, result envelope, or result schema is invalid | Produce no effect for invalid input; contain invalid output and fail the call contract |
| ERR-TOL-004 | Tool call failed, timed out, was cancelled, or has uncertain effect | Preserve attributable evidence; retry only when the contract and policy permit it |
| ERR-TOL-005 | Tool identity collides or a bound external schema drifted | Block the conflicting or stale binding until registry reconciliation or replanning |
| ERR-SCH-001 | Schedule time or trigger is invalid | Reject schedule without partial creation |
| ERR-WRK-001 | Worker identity, capability, revision, or lease is invalid | Reject claim or completion and preserve evidence |
| ERR-VER-001 | Persisted schema version is unsupported | Refuse automatic mutation and identify required operator action |

## 17. Approved and candidate technical constraints

This section records implementation constraints separately from behavioral requirements. It does
not assign component responsibilities.

### TC-001 — Mandatory coordination runtime

CrewAI is the mandatory production runtime for constructing and coordinating agents, tasks, teams,
crews, and flows. MISHKAN MUST NOT ship a competing production agent-coordination runtime.

### TC-002 — Supported CrewAI generation

The implementation will use a currently supported CrewAI 1.x release. The obsolete 0.x lock is not
carried forward.

### TC-003 — Test substitution

Tests MAY substitute deterministic doubles at a runtime boundary. Such doubles MUST NOT be
selectable as a production execution mode.

### TC-004 — Interface scope

The planned product interfaces are a Python command-line and programmatic interface, one headless
control daemon, remote workers, and a separate initially read-only terminal monitor. No web
dashboard is planned for version 1.

### TC-005 — Local and distributed metadata

The accepted direction is an embedded relational store for non-distributed operation and PostgreSQL
for distributed operation. This becomes binding only after the persistence ADR is approved.

### TC-006 — Policy-controlled migrations

The system may prepare, verify, or apply schema migrations according to explicit environment-scoped
capability policy. Migration application MUST support approval gates and MUST emit durable evidence;
it is not universally forbidden or universally enabled.

### TC-007 — CrewAI and external tool integration

Native tools use CrewAI's supported tool contract, and configured external protocol tools use its
supported integration boundary. MISHKAN may wrap these interfaces to enforce product contracts but
MUST NOT replace CrewAI's production agent tool-calling runtime.

## Appendix A — Organization version 1 roster

### Orchestration

`PM`, `CTO`

### Frontend

`Frontend_Lead`, `Frontend_DesignLead`, `Frontend_UXExpert`, `Frontend_Engineer`,
`Frontend_A11ySpec`, `Frontend_SecuritySpec`, `Frontend_QA`, `Frontend_Reporter`

### Backend

`Backend_Lead`, `Backend_ArchSpec`, `Backend_StandardsSpec`, `Backend_Engineer`,
`Backend_DatabaseSpec`, `Backend_SecuritySpec`, `Backend_QA`, `Backend_Reporter`

### Infrastructure

`Infra_Lead`, `Infra_PlatformSpec`, `Infra_DeliverySpec`, `Infra_ReliabilitySpec`,
`Infra_SecuritySpec`, `Infra_QA`, `Infra_Reporter`

### Documentation

`Doc_Specialist`

### Research

`Research_Clarificator`, `Research_Formulator`, `Research_Investigator`,
`Research_Summarizer`, `Research_Evaluator`, `Research_Reporter`

## Appendix B — Organization version 1 outcomes

### Organizational outcomes

`mishkan-init`, `sprint-close`, `deep-research`, `codebase-audit`, `architecture-panel`,
`blast-radius`, `release-readiness`, `dep-audit`, `standards-rollout`,
`knowledge-gap-discovery`

### Team outcomes

`frontend-feature-ship`, `backend-api-version`, `backend-schema-migration`, `infra-deploy`,
`infra-dr-drill`

These names identify stable outcomes and constraints. Their executable task plans are generated for
the specific objective and repository revision and are not fixed universal chains.

## Appendix C — PRD traceability

| PRD item | Principal SRS coverage |
|---|---|
| UC-01 Establish organization | PRJ-001–007, ORG-001–003 |
| UC-02 Delegate objective | PLN-001–011, RUN-001–012 |
| UC-03 Coordinate work | ORG-004–012, RUN-003–007 |
| UC-04 Enforce authority | SAF-001–013 |
| UC-05 Review evidence | OBS-001–008, RUN-012 |
| UC-06 Survive interruption | RUN-008–011, OBS-003 |
| UC-07 Preserve knowledge and grow skills | KNW-001–006, SKL-001–025 |
| UC-08 Operate headlessly | AUT-001–007 |
| UC-09 Use remote capacity | DST-001–010 |
| UC-10 Extend controlled capabilities | TOL-001–027 |
| SC-01 Repository adaptation | PRJ-003–007, ORG-012 |
| SC-02 End-to-end delegation | PLN, ORG, and RUN sections |
| SC-03 Human control | SAF-003–013 |
| SC-04 Recovery | RUN-008–010 |
| SC-05 Degraded usefulness | KNW-005, ERR-DEP-001 |
| SC-06 Auditability | PLN-008–011, OBS-001–008 |
| SC-07 Headless continuity | AUT-001–007 |
| SC-08 Distributed recovery | DST-001–010, post-core |
| SC-09 Local operation | NFR-001–003 |
| SC-10 Skill learning loop | SKL-011–024 |
| SC-11 Controlled tool extension | TOL-003–027 |
