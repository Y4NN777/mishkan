# MISHKAN System Model

**Status:** Proposed amendment — awaiting D-035
**Version:** 1.2
**Derived from:** Proposed PRD 1.4, SRS 1.6, System Contract 1.4, Responsibility Map 1.2

## 1. Purpose and modeling discipline

This document answers only the behavioral questions that constrain architecture. A participant
names an external actor or approved responsibility, not automatically a process, service, package,
database, or protocol. CrewAI is the sole production runtime for agents and teams; MISHKAN owns
deterministic state, policy, effects, evidence, and acceptance around it.

## 2. System context

**Question:** Who interacts with MISHKAN, and which authorities remain outside it?

```mermaid
flowchart LR
    CEO["Human CEO / engineer"]
    Operator["Operator"]
    Harness["Codex, Claude, or compatible harness"]
    Scheduler["External scheduler"]
    Worker["Approved remote worker"]
    subgraph M["MISHKAN system boundary"]
        App["Persistent organization, missions, conversations, and control plane"]
        CrewAI["CrewAI 1.x production coordination"]
    end
    Repo["Repositories and source-control hosts"]
    Models["Configured inference services"]
    Knowledge["Memory, knowledge, and structure sources"]
    MCP["Configured MCP peers"]
    Artifact["Artifact and blob stores"]
    Credentials["Credential resolver"]
    Isolation["Execution and browser isolation"]
    CEO <-->|"conversation, objectives, decisions, inspection"| App
    Operator -->|"configuration, policy, capacity"| App
    Harness <-->|"governed HTTP or MCP contract"| App
    Scheduler -->|"idempotent trigger"| App
    Worker <-->|"identity, lease, envelope, result"| App
    App --> CrewAI
    App <-->|"evidence and authorized effects"| Repo
    App <-->|"inference"| Models
    App <-->|"attributed retrieval"| Knowledge
    App <-->|"mediated discovery and calls"| MCP
    App <-->|"immutable content and references"| Artifact
    App -->|"late secret resolution"| Credentials
    App -->|"bounded effects"| Isolation
```

MISHKAN does not become the repository host, credential authority, model provider, artifact store,
isolation engine, or MCP peer. Availability and instructions from those systems confer no MISHKAN
authority.

## 3. Complete mission lifecycle

**Question:** Which durable states make mission progress and interruption unambiguous?

```mermaid
stateDiagram-v2
    [*] --> Proposed
    Proposed --> Clarifying: origin accepted
    Clarifying --> Planned: Mission Brief confirmed
    Clarifying --> Paused: executive disagreement
    Planned --> Active: plan authorized and crew ready
    Planned --> Paused: approval or dependency pending
    Active --> Blocked: required dependency or decision absent
    Active --> Paused: authorized intervention
    Active --> Evaluating: production results accepted
    Evaluating --> Remediating: evaluation rejects
    Remediating --> Active: corrected plan authorized
    Evaluating --> Completed: evaluation and report accepted
    Paused --> Planned: brief or plan changes
    Paused --> Active: resume authorized
    Blocked --> Active: limitation reconciled
    Proposed --> Cancelled: stop accepted
    Clarifying --> Cancelled: stop accepted
    Planned --> Cancelled: stop accepted
    Active --> Cancelled: effects settle
    Active --> Failed: terminal failure
    Evaluating --> Failed: terminal acceptance failure
    Completed --> [*]
    Failed --> [*]
    Cancelled --> [*]
```

Every transition records actor or cause, reason, command or decision, organization and Mission Brief
versions, and evidence. Cancellation releases no new task; uncertain effects settle or reconcile
before the terminal record claims completion.

## 4. Mission Brief and PM/CTO agreement

**Question:** What becomes durable before mission planning may begin?

```mermaid
sequenceDiagram
    actor CEO
    participant M as Mission governance (RSP-022)
    participant PM as PM agent
    participant CTO as CTO agent
    participant O as Organization definitions (RSP-007)
    participant V as Evidence and events (RSP-015)
    CEO->>M: free-form problem, objective, or strategic constraint
    M->>PM: clarify value, scope, priority, and functional acceptance
    M->>CTO: clarify feasibility, risks, technical and assurance coverage
    O-->>M: exact organization version and eligible persistent identities
    PM-->>M: product confirmation or cited disagreement
    CTO-->>M: technical confirmation or cited disagreement
    alt agreement within authority
        M->>V: persist Mission Brief, confirmations, proposed crew, and event
        V-->>M: mission becomes planned
    else unresolved or outside authority
        M->>V: persist scoped pause and actionable escalation
        V-->>CEO: options, consequences, recommendations, and required decision
    end
```

An optional template may contribute guidance and provenance before confirmation. It never supplies
implicit authority, a fixed crew, or a mandatory task graph.

## 5. Disagreement, partial pause, and CEO escalation

**Question:** How does disagreement avoid freezing unrelated work or becoming an implicit decision?

```mermaid
flowchart TD
    D["PM/CTO disagreement recorded"] --> Scope["Compute dependent disputed scope"]
    Scope --> Pause["Pause only dependent tasks and decisions"]
    Scope --> Continue["Continue independent eligible work"]
    Pause --> Resolve{"Can PM and CTO resolve within authority?"}
    Resolve -->|"yes"| Decision["Durable executive decision"]
    Resolve -->|"no"| Escalation["Actionable CEO escalation"]
    Escalation --> CEO["CEO command: decide, accept risk, change scope, or stop"]
    CEO --> Decision
    Decision --> Replan["Revalidate Brief, crew, plan, and policy"]
```

A conversation message or recommendation is not the decision. Only an authorized command changes
mission state.

## 6. Contextual Mission Crew composition

**Question:** How are permanent professional identities composed without a static team matrix?

```mermaid
sequenceDiagram
    participant M as Mission governance (RSP-022)
    participant O as Organization profiles (RSP-007)
    participant E as Professional evidence (RSP-026)
    participant P as Planning (RSP-005)
    participant PM
    participant CTO
    M->>O: Mission Brief and organization version
    O-->>M: 59 identities, branches, pools, authority, independence
    M->>E: request scoped competence, availability, conflict, and freshness evidence
    E-->>M: attributable profile evidence and unknowns
    M->>P: eligible identities plus risk and assurance needs
    P-->>M: proposed owners, contributors, evaluators, reporter, and Mission Lead
    CTO->>M: confirm technical, security, and assurance coverage
    PM->>M: confirm formal composition
    M->>M: fingerprint and persist crew revision
```

The Mission Lead is a temporary responsibility assigned to one of the 59 identities. Tools and
skills are selected later for exact tasks; professional identity never implies ambient capability.

## 7. Production, independent evaluation, reporting, and acceptance

**Question:** How does CrewAI coordinate work without owning deterministic acceptance?

```mermaid
sequenceDiagram
    participant C as CrewAI coordination (RSP-008)
    participant P as Producer agent
    participant A as Result acceptance (RSP-010)
    participant Q as Independent assurance (RSP-009)
    participant E as Independent evaluator
    participant R as Reporter
    participant V as Evidence and events (RSP-015)
    C->>P: execute authorized production task
    P-->>C: structured candidate result and artifact references
    C->>A: submit candidate with execution lineage
    A->>V: persist valid candidate acceptance and event
    V-->>C: release downstream evaluation task
    C->>E: execute independent evaluation
    E-->>Q: evaluation result and evidence
    Q->>A: submit separation-checked evaluation
    alt evaluation rejects
        A->>V: persist rejection and correction eligibility
        V-->>C: release authorized remediation or replanning
    else evaluation accepts
        A->>V: persist evaluation acceptance
        V-->>C: release separate reporting task
        C->>R: produce report from accepted evidence
        R-->>A: structured report
        A->>V: persist report and mission completion if all conditions hold
    end
```

CrewAI completion is always a candidate result. Production, evaluation, reporting, and evidence
audit remain separately attributable.

## 8. Common governed capability invocation

**Question:** How do native tools, skills, engines, Web, Browser, artifacts, and MCP share one
authority boundary without becoming one static taxonomy?

```mermaid
sequenceDiagram
    participant C as CrewAI task
    participant R as Registry resolution (RSP-021/025)
    participant G as Effect gateway (RSP-011)
    participant P as Policy decision (RSP-006)
    participant X as Selected capability adapter
    participant S as Security evidence (RSP-012)
    participant V as Evidence and events (RSP-015)
    C->>R: exact task intent and observed context
    R-->>C: concrete available binding or truthful unavailable state
    C->>G: typed call with actual targets and declared effects
    G->>G: validate binding, schema, scopes, limits, and session ownership
    G->>P: decide exact normalized request
    alt deny or approval absent
        P-->>G: stable refusal
        G->>V: non-secret refusal evidence
        G-->>C: classified failure
    else allow
        P-->>G: exact grant
        G->>X: dispatch outside state transaction
        X-->>G: completed, failed, cancelled, or uncertain
        G->>S: inspect output and effect evidence
        S-->>G: validated, contained, or rejected
        G->>V: terminal attributable record and degradation if any
        G-->>C: validated bounded result or artifact reference
    end
```

Discovery, installation, health, instructions, and credentials never grant authority. Project
commands remain process/Bash inputs, not invented tool identities.

## 9. PTY, job, browser, and MCP session lifecycle

**Question:** How are long-lived state and uncertain effects contained?

```mermaid
stateDiagram-v2
    [*] --> Requested
    Requested --> Starting: binding, policy, and limits valid
    Requested --> Refused: invalid or unauthorized
    Starting --> Ready: readiness evidence observed
    Starting --> Failed: start failure settles
    Ready --> Active: command, action, or call accepted
    Active --> Ready: operation settles without closing session
    Active --> Cancelling: cancellation requested
    Active --> Uncertain: contact lost after possible effect
    Ready --> Closing: close requested or deadline reached
    Cancelling --> Closing: effect settlement observed
    Cancelling --> Uncertain: settlement cannot be established
    Uncertain --> Ready: reconciliation proves session reusable
    Uncertain --> Closing: reconciliation requires termination
    Closing --> Closed: resources and effects settled
    Refused --> [*]
    Failed --> [*]
    Closed --> [*]
```

Each session has an owner, execution location, scope, cursors, deadlines, resource bounds,
credential references, and lifecycle evidence. Browser authenticated state and MCP/terminal
sessions are never shared implicitly.

## 10. Immutable artifacts and concurrent working references

**Question:** How can large results be durable and collaborative without silent overwrite?

```mermaid
sequenceDiagram
    participant P as Producer
    participant A as Artifact governance (RSP-023)
    participant B as Content-addressed store
    participant M as Metadata transaction
    participant C as Concurrent writer
    P->>A: stream content plus media and provenance contract
    A->>B: write and verify immutable content
    B-->>A: content identity and size
    A->>M: persist manifest and validation state
    P->>A: advance working reference, expected revision r1
    C->>A: advance same reference, expected revision r1
    A->>M: compare r1 and atomically set r2
    M-->>A: success
    A->>M: compare stale r1 with current r2
    M-->>A: conflict; current r2 retained
    A-->>C: deterministic conflict with both immutable revisions
```

Storage success does not equal task acceptance. Missing or corrupt content enters recovery state;
retention and garbage collection preserve protected manifests and evidence.

## 11. Skill and professional-competence evolution

**Question:** How can Hermes-like live improvement remain fluid, reversible, and evidence-based?

```mermaid
stateDiagram-v2
    [*] --> Observed: teaching, miss, correction, or evaluated outcome
    Observed --> Candidate: attributable skill/profile change created
    Candidate --> Rejected: invalid, incompatible, or denied
    Candidate --> Quarantined: configured inspection or trust threshold
    Candidate --> Active: policy allows immediate validated native mutation
    Candidate --> Staged: policy requires later review or approval
    Staged --> Active: required decision accepted
    Quarantined --> Candidate: finding resolved
    Active --> Superseded: newer version activated
    Active --> Restored: prior eligible version restored
    Superseded --> Restored: restoration accepted
```

Activation policy depends on source, trust, scope, effect, and risk; staging is not universal.
Community discovery remains a candidate. Agent-profile promotion additionally requires scoped
attributable evaluation and cannot alter authority, independence, identity, or failure history.

## 12. External harness request through MISHKAN and CrewAI

**Question:** Where does a machine client request become governed organizational work?

```mermaid
sequenceDiagram
    participant H as External harness
    participant I as HTTP or MCP facade (RSP-024)
    participant M as Mission governance (RSP-022)
    participant P as Planning and policy (RSP-005/006)
    participant C as CrewAI coordination (RSP-008)
    participant V as Evidence and events (RSP-015)
    H->>I: authenticated versioned objective, query, or intervention
    I->>I: validate identity, schema, scope, and transport session
    I->>M: translate to application command
    M->>P: Mission Brief, direct request, or authorized intervention context
    P->>V: persist decision and accepted command
    alt governed work accepted
        V-->>C: authorized CrewAI execution envelope
        C-->>V: candidate results and runtime lineage
        V-->>I: durable state, artifacts, and resumable events
        I-->>H: structured result or cursor
    else refused
        V-->>I: stable refusal evidence
        I-->>H: typed refusal
    end
```

The facade does not expose a runtime selector or a second agent coordinator.

## 13. Resume, scheduling, and worker lease

**Question:** How does asynchronous and distributed execution preserve acceptance semantics?

```mermaid
sequenceDiagram
    participant S as Schedule governance (RSP-016)
    participant V as State and outbox (RSP-015)
    participant C as CrewAI coordination (RSP-008)
    participant D as Worker coordination (RSP-017)
    participant W1 as Worker A
    participant W2 as Worker B
    participant A as Result acceptance (RSP-010)
    S->>V: atomically deduplicate occurrence and create run
    V-->>C: durable eligible task
    C->>D: request eligible remote placement
    D-->>W1: immutable envelope and bounded lease
    Note over W1,D: heartbeat stops; lease expires
    D-->>W2: new attempt envelope and lease
    W2->>A: valid completion
    A->>V: atomically accept first valid completion and release dependencies
    W1->>A: late completion
    A->>V: record stale or duplicate delivery; accepted result unchanged
    Note over C,V: after restart, resume from earliest required result not accepted
```

Scheduling and placement create new delivery and recovery states, never new policy or acceptance
semantics.

## 14. Behavioral conclusions constraining structure

1. One authoritative application domain owns missions, conversations, commands, policy, evidence,
   artifacts, and acceptance for every client.
2. CrewAI remains directly integrated as the only production agent/team runtime.
3. State changes and outbox facts use short consistency boundaries; model execution and external
   effects occur outside them.
4. Mission governance, planning, authorization, effect enforcement, acceptance, artifact storage,
   and projections remain distinct ownership boundaries.
5. Sessions, artifacts, worker leases, attempts, and results have separate identities and
   lifecycles.
6. Optional templates, skills, packs, tools, and engines are contextually resolved inputs, not
   authority or static workflows.
7. Read models and live streams may lag; commands validate against authoritative current state.
8. The accepted structural direction remains a transactional modular monolith with an outbox, not
   full event sourcing or service-first decomposition.

These conclusions supersede the version 1.1 behavior amendment only after D-035. They do not
authorize implementation.
