# MISHKAN Decision Log

**Owner:** Y4NN777
**Status:** Living authority

This is the single registry for product, system, technical, operational, and documentation
decisions. A decision may later receive a detailed PDR or ADR when its context and alternatives
cannot be represented clearly in one row.

## Statuses

| Status | Meaning |
|---|---|
| Accepted | Active and authoritative |
| Proposed | Candidate awaiting explicit acceptance |
| Open | Required but unresolved |
| Superseded | Replaced by another recorded decision |

## Decisions

| ID | Type | Status | Decision | Consequence | Source |
|---|---|---|---|---|---|
| D-001 | Product | Accepted | MISHKAN is a persistent, local-first engineering organization and control plane | Core value cannot depend on a paid hosted control plane | PRD 1.0 |
| D-002 | Technical | Accepted | Supported CrewAI 1.x is the sole production coordination runtime for agents, tasks, teams, crews, and flows | No competing production coordination runtime; deterministic substitutes are test-only | SRS TC-001–003 |
| D-003 | Product | Superseded | Named organizational outcomes are stable while plans adapt to repository evidence, objective, capability, and policy | Superseded by D-032: missions accept free-form objectives and optional templates without a mandatory outcome catalogue | SRS PRJ-007, ORG-012; D-032 |
| D-004 | Product | Accepted | Plans may evolve through linked, auditable versions inside effective policy | Immutability applies to recorded versions and issued task envelopes, not the whole run | SRS PLN-004–010 |
| D-005 | Security | Accepted | Operational capabilities use a public versioned `allow`, `require_approval`, or `deny` policy | No hidden hardcoded product deny-list; lack of a grant remains lack of authority | SRS SAF-003–006 |
| D-006 | Product | Accepted | Stateful operations, including commit, push, deployment, release, and migration, may execute within exact authorized scopes | These actions are configurable capabilities, not universal prohibitions | SRS SAF-006–008, TC-006 |
| D-007 | Product | Accepted | A run targets one repository and records one immutable base revision; produced revisions retain task and run lineage | A multi-repository mission coordinates multiple context-bound runs under D-032; one run never conflates repository revisions | SRS PRJ-001–002; D-032 |
| D-008 | Organization | Superseded | Organization version 1 contains the SRS roster of 32 roles and catalogue of 15 outcomes | Superseded by D-032 and the accepted 59-identity organization with free-form missions | SRS ORG-001–012, appendices A–B; D-032 |
| D-009 | Organization | Accepted | Production, independent evaluation, orchestration, and reporting obey the declared role-separation rules | Self-evaluation and same-task orchestration/reporting conflicts are rejected | SRS ORG-004–008 |
| D-010 | Delivery | Accepted | Distributed execution follows proof of local core operation | Distributed requirements are post-core release scope | PRD and SRS §14 |
| D-011 | Scope | Superseded | Version 1 exposes CLI/SDK, a headless daemon, workers, and a read-only terminal monitor; no web dashboard or Windows target | Superseded by D-032 for the operational TUI; the no-web-dashboard and no-Windows exclusions remain in PRD 1.4 and SRS 1.6 | SRS TC-004, NFR-001; D-032 |
| D-012 | Governance | Superseded | PRD 1.0, SRS 1.1, and System Contract 1.0 were the approved baseline before the skills and tools amendment | Superseded by D-019; retained as approval history | Engineer validation, 2026-08-23 |
| D-013 | Documentation | Accepted | Version only durable authority or factual evidence, organized by domain with this central decision registry | Working registers, self-reviews, per-gate approval files, and speculative checklists are not project documents | Aïobi ID documentation model review, 2026-08-23 |
| D-014 | System | Accepted | Authority is closed-world; equal-precedence policy conflict denies; plans evolve through linked versions; action names are not universal prohibitions; evidence meaning is append-only | These five laws govern the responsibility and architecture stages | Engineer validation, 2026-08-23 |
| D-015 | Technical | Accepted | Use SQLite/WAL for non-distributed metadata and PostgreSQL for distributed metadata behind the same repository, transaction, lease, and outbox semantics | Local operation needs no database service; distributed operation cannot depend on SQLite locking behavior | Engineer validation of `SYSTEM/ARCHITECTURE.md` ADR-002, 2026-08-23 |
| D-016 | Technical | Accepted | Use one versioned control API, a narrow mandatory CrewAI production boundary, and typed configurable ports for external adapters | All interfaces share application semantics; adapter availability grants no authority; external schema drift is explicit | Engineer validation of `SYSTEM/ARCHITECTURE.md` ADR-003–005, 2026-08-23 |
| D-017 | System | Superseded | The original requirement-to-responsibility map, CrewAI runtime boundary, and separate skills/tools ownership boundaries are approved | Superseded by D-034–D-035; the CrewAI and skills/tools separation principles are retained in the accepted 1.2 documents | Engineer validation, 2026-08-23; D-034–D-035 |
| D-018 | Product | Accepted | MISHKAN has a first-class Hermes-inspired skills system for portable procedural memory, progressive disclosure, learning from reviewed experience, provenance, and recoverable evolution | Skills enrich authorized CrewAI tasks; they are not tools, static workflows, a competing runtime, or a MISHKAN-hosted marketplace | Engineer correction; official [Hermes Agent](https://github.com/NousResearch/hermes-agent) and [skills guide](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/skills.md), 2026-08-23 |
| D-019 | Governance | Superseded | PRD 1.2, SRS 1.3, System Contract 1.2, and Responsibility Map 1.0 were canonical before the general-tool amendment | Superseded by D-029; retained as approval history | Engineer validation, 2026-08-23 |
| D-020 | System | Accepted | MISHKAN has a first-class versioned tool registry and toolset model for native and configured external atomic capabilities | Tool availability never grants authority; exact tools bind to accepted tasks and execute through CrewAI plus MISHKAN validation and policy enforcement | Engineer correction; official [CrewAI tools](https://github.com/crewAIInc/crewAI/blob/main/lib/crewai-tools/README.md), [Hermes tools](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/tools.md), and [Hermes toolsets](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/toolsets-reference.md), 2026-08-23 |
| D-021 | Architecture | Accepted | Use a transactional modular control plane with relational authoritative state and an append-only event outbox written atomically with state | Model execution, external effects, projections, and delivery stay outside short state transactions; C4 structure may now be derived | Engineer validation following Sequence 05 behavior analysis and `codex-design` risk consultation, 2026-08-23 |
| D-022 | Governance | Superseded | System Model 1.0 and Architecture 1.0 form the approved Sequence 05 baseline | Superseded by D-035; retained architecture laws are incorporated in Model and Architecture 1.2 | Engineer validation, 2026-08-23; D-035 |
| D-023 | Delivery | Superseded | Deliver MISHKAN through the vertical increments, acceptance gates, repository baseline, and progressive Git protocol in `PROJECT/IMPLEMENTATION_PLAN.md` | Superseded by D-036; progressive vertical delivery remains incorporated in Implementation Plan 1.4 | Engineer validation, 2026-08-23; D-036 |
| D-024 | Delivery | Accepted | Begin implementation increment I00 on its dedicated branch under Implementation Plan 1.0 | Only the contract-bearing foundation is authorized; later increments retain their dependency and acceptance gates | Engineer authorization, 2026-08-23 |
| D-025 | Delivery | Accepted | Use topic branches (`feat/*`, `fix/*`, and other applicable change prefixes) into `develop`, then promote `develop` into `main` after the integrated gate | Direct topic-to-main merges are prohibited; the already-published I00 merge is retained as history rather than silently rewritten | Engineer correction, 2026-08-23 |
| D-026 | Delivery | Accepted | Begin I01 on `feat/i01-local-crewai` using progressive tested commits | I01 is authorized; I02 and later increments remain outside the active implementation scope | Engineer validation, 2026-08-24 |
| D-027 | Delivery | Accepted | Begin I02 on `feat/i02-policy-tools` using the approved Policy Authority, immutable Tool Registry, and deterministic Capability Gateway boundaries | I02 is authorized; operational policy remains public and configurable, integrity-stage ordering remains fixed, and I03 and later increments stay outside the active implementation scope | Engineer continuation authorization, 2026-08-24 |
| D-028 | Delivery | Superseded | Reopen the I02 increment gate and replace the assumed universal SWE catalogue with a capability-first model resolved to concrete repository- and environment-available adapters | Superseded by D-029 because capability-family matrices introduced false precision and per-command tool contracts | Engineer correction and validation, 2026-08-24 |
| D-029 | Governance | Superseded | PRD 1.2, SRS 1.4, System Contract 1.3, and Responsibility Map 1.1 define a small general-tool model with configurable toolsets, dynamic extensions, concrete adapter availability, and input-level policy | Superseded as a document baseline by D-032–D-034; its general-tool, public-policy, and sole-CrewAI principles remain incorporated | Engineer direction informed by official [Hermes tools and toolsets](https://github.com/hermes-agent-org/hermes/blob/main/website/docs/user-guide/features/tools.md) and [Claude Code tools and permissions](https://code.claude.com/docs/en/tools-reference), 2026-08-24; D-032–D-034 |
| D-030 | Delivery | Superseded | Accept System Model 1.1, Architecture 1.1, and Implementation Plan 1.3 as the amended delivery baseline | Superseded without acceptance by D-035 and D-036, which approve Model/Architecture 1.2 and Implementation Plan 1.4 | Derived documentation amendment, 2026-08-24; D-035–D-036 |
| D-031 | Product | Accepted | MISHKAN provides evidence-based assistance for consequential engineering choices while the engineer retains durable decision authority | PRD 1.3 and SRS 1.5 require project-grounded criteria, credible alternatives, explicit evidence and uncertainty, proportionate validation, independent challenge, configurable explanation depth, and staged acceptance | Engineer validation, 2026-08-24 |
| D-032 | Governance | Accepted | PRD 1.4 and SRS 1.6 are the reconciled product and behavioral baseline | Free-form missions, mission-scoped environment decisions, 59 persistent identities, PM/CTO governance, operational clients, contextual capabilities, and all WD requirement families supersede D-003, D-008, D-029, and the conflicting scope in D-007/D-011 | Engineer validation after assistant coherence audit, 2026-08-25 |
| D-033 | Governance | Accepted | System Contract 1.4 is authoritative | Mission authority, durable communication, artifact/CAS integrity, isolated sessions, compatible fallback, MCP/harness mediation, and evidence-based evolution are active invariants and refusals | Engineer validation after assistant coherence audit, 2026-08-25 |
| D-034 | Governance | Accepted | Requirement-to-Responsibility Map 1.2 is authoritative | Every SRS 1.6 requirement, error, promise, and invariant has exactly one primary owner and explicit handoffs through RSP-001–026 | Engineer validation after assistant coherence audit, 2026-08-25 |
| D-035 | Architecture | Accepted | System Model 1.2 and Architecture 1.2 are authoritative | The mission-centered behavioral model and single-authority transactional modular monolith supersede D-017, D-022, and the unaccepted D-030 model/architecture amendment while retaining CrewAI as the sole internal production runtime | Engineer validation after assistant coherence audit, 2026-08-25 |
| D-036 | Delivery | Accepted | Implementation Plan 1.4 is authoritative and I02 resumes on `feat/i02-policy-tools` | It supersedes D-023 and the unaccepted D-030 plan amendment; delivery follows I02–I11 and their complete SRS ownership; I00/I01 evidence remains accepted and later increments retain their gates | Engineer validation after assistant coherence audit, 2026-08-25 |
| D-037 | Architecture / Delivery | Accepted | System Model 1.3, Architecture 1.3, and Implementation Plan 1.5 are authoritative without changing PRD 1.4 or SRS 1.6 | Makes the agent-authored plan, per-context binding lifecycle, Dev Container/Podman/Compose descriptor boundaries, ordinary Artifact/Edit/Process effects, work packages, and acceptance scenarios explicit; preserves D-036's I02 authority and the later increment gates | Engineer confirmation after assistant coherence audit, 2026-08-25 |

## Working-decision promotion matrix

The local register remains non-normative. This matrix proves that no validated working decision is
left solely in that register; authority transferred through accepted decisions D-032–D-036.

| Working decision | Durable promotion target |
|---|---|
| WD-001 | PRD UC-12/PP-15; SRS CTX-001; Contract CTR-020; RSP-025; Model §6/§8; I05 |
| WD-002 | SRS CTX-002–003; Contract INV-016/038; RSP-004; Model §8–9; I05 |
| WD-003 | PRD PP-06/PP-15; SRS CTX-006 and NFR-002; Contract CTR-010/020; RSP-025; I05/I11 |
| WD-004 | PRD UC-10; SRS CTX-008; Contract INV-002/025; RSP-006/025; Model §8/§11; I05 |
| WD-005 | SRS CTX-004–005 and ENG-001; Contract CTR-020; RSP-025; Architecture §6; I05 |
| WD-006 | PRD PP-11/PP-15; SRS CTX-006; Contract INV-039; RSP-025; Model §8; I05 |
| WD-007 | SRS CTX-001/005; Contract CTR-001/020; RSP-001/025; I05 |
| WD-008 | SRS CTX-007–008; Contract INV-025/031; RSP-025; I05 |
| WD-009 | PRD UC-14; SRS MCP-009; Contract CTR-019/INV-041; RSP-024; Model §12; I04 |
| WD-010 | SRS SYS-006, MSN-015, MCP-001–003; Contract CTR-011/019; RSP-003/024; Architecture §2/§4; I04 |
| WD-011 | PRD PP-14–15; SRS ENG-001–003; Contract CTR-020; RSP-025; Architecture §6; I05 |
| WD-012 | SRS ENG-004–006; Contract CTR-020; RSP-025; Architecture §6; I05 |
| WD-013 | SRS FIL-007, EDT-007, ENG-001; Contract Tool §10; RSP-011/025; I02/I03/I05 |
| WD-014 | SRS ENG-008; Contract INV-039; RSP-025; Model §8; I05 |
| WD-015 | SRS EXE-001–008; Contract Execution §12; RSP-011; Model §9; I02/I03 |
| WD-016 | PRD SC-15; SRS ENG-001/005; Contract CTR-020; RSP-025; I05/I11 |
| WD-017 | SRS EXE-001–008; Contract INV-030/038; RSP-011; Model §9; I02/I03 |
| WD-018 | SRS WEB-001–007; Contract INV-039; RSP-011; Model §8; I04 |
| WD-019 | SRS BRW-001–008; Contract INV-038; RSP-011; Model §9; I04 |
| WD-020 | SRS ART-001–005; Contract CTR-017/INV-036–037; RSP-023; Model §10; I03 |
| WD-021 | SRS ART-006–008 and TC-009; Contract Artifact §13; RSP-023; Architecture §7; I03 |
| WD-022 | SRS MCP-001–009; Contract MCP §14; RSP-024; Model §12; Architecture §2/§4; I04 |
| WD-023 | SRS FIL-001–007; Contract Tool §10; RSP-011; Model §8; I02 |
| WD-024 | SRS EDT-001–008; Contract INV-015/030; RSP-011; I03 |
| WD-025 | PRD UC-07; SRS SKL-001–025; Contract Skill §9; RSP-014/020; Model §11; I05 |
| WD-026 | PRD SC-15; SRS ENG-001–008; Contract CTR-020; RSP-025; Architecture §6; I05/I11 |
| WD-027 | PRD UC-12–15; SRS ORG-001–016 and MSN-001–015; Contract CTR-015–018; RSP-007/022/026; Model §3–7; I06 |
| WD-028 | PRD UC-12/PP-16/SC-13; SRS PLN-021, MSN-016, and ENG-009–013; Contract CTR-015/020 and INV-042; RSP-005/008/022/025; Model §4/§8.1; Architecture §5–6; I05/I06/I09/I11 |

## Open decisions

No documentary-baseline decision remains open. D-030 was superseded without acceptance; D-032
through D-037 were accepted in order on 2026-08-25. I02 remains resumed under Implementation Plan
1.5 and subject to its implementation scope and acceptance gate.

## Review rules

- Accepted decisions are added here before they drive implementation.
- Superseded decisions remain as rows and name their replacement.
- Open decisions do not hide in system or roadmap prose.
- A PDR or ADR must be referenced from this log when one is created.
- Review notes and approval receipts stay outside the durable documentation set.
