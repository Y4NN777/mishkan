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
| D-003 | Product | Accepted | Named organizational outcomes are stable while plans adapt to repository evidence, objective, capability, and policy | Workflows are not universal static task chains | SRS PRJ-007, ORG-012 |
| D-004 | Product | Accepted | Plans may evolve through linked, auditable versions inside effective policy | Immutability applies to recorded versions and issued task envelopes, not the whole run | SRS PLN-004–010 |
| D-005 | Security | Accepted | Operational capabilities use a public versioned `allow`, `require_approval`, or `deny` policy | No hidden hardcoded product deny-list; lack of a grant remains lack of authority | SRS SAF-003–006 |
| D-006 | Product | Accepted | Stateful operations, including commit, push, deployment, release, and migration, may execute within exact authorized scopes | These actions are configurable capabilities, not universal prohibitions | SRS SAF-006–008, TC-006 |
| D-007 | Product | Accepted | A run targets one repository and records one immutable base revision; produced revisions retain task and run lineage | Multi-repository runs are outside version 1 | SRS PRJ-001–002 |
| D-008 | Organization | Accepted | Organization version 1 contains the SRS roster of 32 roles and catalogue of 15 outcomes | A later roster requires a new versioned organization definition | SRS ORG-001–012, appendices A–B |
| D-009 | Organization | Accepted | Production, independent evaluation, orchestration, and reporting obey the declared role-separation rules | Self-evaluation and same-task orchestration/reporting conflicts are rejected | SRS ORG-004–008 |
| D-010 | Delivery | Accepted | Distributed execution follows proof of local core operation | Distributed requirements are post-core release scope | PRD and SRS §12 |
| D-011 | Scope | Accepted | Version 1 exposes CLI/SDK, a headless daemon, workers, and a read-only terminal monitor; no web dashboard or Windows target | Architecture and roadmap must remain inside this interface boundary | SRS TC-004, NFR-001 |
| D-012 | Governance | Accepted | PRD 1.0 and SRS 1.0 are canonical; Sequence 03 contract work is authorized but not yet approved | Responsibility assignment and implementation remain blocked | Engineer validation, 2026-08-23 |
| D-013 | Documentation | Accepted | Version only durable authority or factual evidence, organized by domain with this central decision registry | Working registers, self-reviews, per-gate approval files, and speculative checklists are not project documents | Aïobi ID documentation model review, 2026-08-23 |

## Open decisions

| ID | Type | Status | Decision needed | Blocking point |
|---|---|---|---|---|
| D-014 | System | Open | Approve or amend the system contract, including closed-world authority and deterministic policy conflict behavior | Sequence 04 responsibilities |
| D-015 | Technical | Open | Select the concrete persistence design for local and distributed modes | Architecture ADR |
| D-016 | Technical | Open | Select concrete interfaces for inference, memory, knowledge, structure, events, scheduling, and workers | Architecture ADRs |

## Review rules

- Accepted decisions are added here before they drive implementation.
- Superseded decisions remain as rows and name their replacement.
- Open decisions do not hide in system or roadmap prose.
- A PDR or ADR must be referenced from this log when one is created.
- Review notes and approval receipts stay outside the durable documentation set.
