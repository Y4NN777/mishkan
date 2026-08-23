# MISHKAN Product Requirements Document

**Status:** Approved — Gate G1
**Version:** 1.0
**Scope:** Product problem and outcomes only

**Approval recorded:** 2026-08-23
**Approved by:** Y4NN777

## 1. Problem

Software engineers increasingly delegate parts of software work to AI assistants, but those
assistants commonly operate as isolated, short-lived sessions. Their behavior, knowledge, quality
controls, and access boundaries vary by tool and disappear when the session ends.

This creates five observable problems:

1. Complex engineering outcomes must repeatedly be decomposed and coordinated by the engineer.
2. Work context, decisions, and lessons are lost or become difficult to recover between sessions.
3. The same actor may generate, evaluate, and summarize its own work, weakening independent
   quality control.
4. Permission boundaries are often expressed as instructions rather than enforceable rules,
   exposing repositories and external systems to unintended state changes.
5. Long-running or multi-project work is difficult to resume, audit, operate unattended, or
   distribute without binding the engineer to one interactive development tool.

MISHKAN exists to provide a persistent, inspectable, and safety-constrained software engineering
organization that accepts engineering objectives, organizes the work, preserves context, and
returns reviewable results while the engineer retains control over consequential actions.

## 2. Target users

### 2.1 Primary user: software engineer

An engineer working alone or in a small team who needs to delegate multi-step engineering work
without surrendering repository, release, or infrastructure authority.

### 2.2 Secondary user: engineering lead

A lead who needs consistent role separation, evidence, progress visibility, and reusable knowledge
across projects or contributors.

### 2.3 Operational actor: system operator

The person responsible for configuring execution resources, credentials, schedules, retention,
and remote capacity. This may be the same person as the engineer.

## 3. Main use cases

### UC-01 — Establish the organization for a repository

The engineer can introduce MISHKAN to an existing repository. The system discovers the repository's
actual characteristics, identifies relevant disciplines and constraints, and proposes an initial
work context without assuming that all projects share the same structure.

### UC-02 — Delegate an engineering objective

The engineer can submit an outcome such as investigating a defect, designing a change, implementing
a feature, reviewing architecture, preparing a release, or researching an unknown. The system
turns that outcome into an explicit, reviewable plan appropriate to the repository.

Each run is anchored to one recorded repository revision. The effective policy decides whether a
plan may execute automatically, requires interactive approval, or must be rejected. Plans may
adapt during execution inside pre-authorized boundaries; material changes outside those boundaries
pause for a policy decision or engineer approval.

### UC-03 — Coordinate specialized work

The system assigns production, evaluation, reporting, research, documentation, and coordination
responsibilities to distinct organizational roles. Relevant roles collaborate and exchange
artifacts without requiring the engineer to manually coordinate every handoff.

### UC-04 — Enforce human authority

The engineer can define what the organization may read, write, execute, or access. Capabilities are
explicit, versioned, inspectable, and configurable by scope. The same mechanism can deny an action,
allow it unattended, or require approval according to its context and risk.

Within an authorized scope, the organization may edit files and perform state-changing operations,
including routine source-control work, when policy grants those capabilities. Every such operation
must remain attributable and reviewable; agents cannot grant themselves additional authority.

### UC-05 — Review evidence and progress

The engineer can observe current work, decisions, failures, security findings, resource health,
and completed outputs. The system provides enough evidence to understand what happened and why.

### UC-06 — Survive interruption

Work can continue after a process, machine, service, or network interruption without discarding
already accepted results or silently accepting the same completion more than once.

### UC-07 — Preserve and reuse knowledge

The organization can recall recent context, retrieve project knowledge, understand repository
structure, and propose reusable procedures. Permanent cross-project promotion remains subject to
engineer approval.

### UC-08 — Operate headlessly

The engineer can run approved work immediately or on a schedule without keeping an interactive IDE
session open. Headless operation obeys the same safety and review boundaries as interactive work.

### UC-09 — Use available execution capacity

The operator can run the organization on one machine or distribute eligible work across approved
machines while preserving identity, authorization, revision consistency, and accepted-result
semantics.

## 4. Out of scope

MISHKAN will not:

1. Replace the engineer's IDE, source-control host, deployment platform, or incident-management
   system.
2. Bypass the configured authorization policy or the protections of an external source-control,
   deployment, release, or data platform.
3. Become a hosted software-as-a-service product or require a vendor-hosted control plane.
4. Provide a web dashboard.
5. Operate an unrestricted marketplace for procedures or extensions.
6. Guarantee correctness solely because an AI actor declared its own output correct.
7. Hide failed, degraded, retried, rejected, or security-relevant actions from the engineer.
8. Treat every repository as having the same teams, technologies, paths, or task sequence.
9. Target Windows in the initial product scope.

## 5. Product principles

### PP-01 — Engineer authority

The system may produce plans, analysis, source changes, tests, documentation, review artifacts, and
authorized state changes. The engineer controls the policy that grants, scopes, or approval-gates
consequential operations.

### PP-02 — Independent evaluation

Production and evaluation are different responsibilities. Reporting is also independent from the
actor making orchestration decisions.

### PP-03 — Repository-specific planning

A named organizational outcome remains stable, but its execution plan adapts to the repository,
requested objective, available capabilities, and approved policy.

### PP-04 — Inspectable operation

Plans, decisions, events, results, failures, approvals, and provenance are available for review.

### PP-05 — Durable progress

Accepted work and decisions survive interruption. Resumption is explicit and reproducible.

### PP-06 — Local ownership

The product can provide its core value without requiring a paid hosted control plane.

### PP-07 — Enforced boundaries

Safety rules are enforced at system boundaries and cannot depend solely on instructions given to an
AI actor.

### PP-08 — Progressive knowledge

The organization uses the least expensive adequate source of context and promotes reusable
knowledge only through visible provenance and approval.

## 6. Success criteria

The product succeeds when all of the following outcomes can be demonstrated:

### SC-01 — Repository adaptation

Given materially different repositories, initialization produces materially different relevant
work plans while preserving the same organizational safety rules.

### SC-02 — End-to-end delegation

An engineer can submit a multi-discipline engineering objective and receive production artifacts,
an independent evaluation, and a structured report without manually coordinating each handoff.

### SC-03 — Human-control enforcement

In an adversarial acceptance suite, each consequential operation is denied, approval-gated, or
executed exactly as the effective policy specifies. No actor can exceed its granted capability, and
every decision and resulting state change is visible to the engineer.

### SC-04 — Recovery

After interruption at every supported work boundary, execution resumes without losing accepted
results and without accepting a duplicate result.

### SC-05 — Degraded usefulness

Loss of optional context or knowledge services does not prevent safe work that can proceed without
them; the loss and resulting limitations are visible.

### SC-06 — Auditability

For any completed or failed run, the engineer can identify the accepted plan, participating roles,
inputs, outputs, evaluations, policy decisions, retries, failures, approvals, and final status.

### SC-07 — Headless continuity

An approved scheduled objective can start and complete without an open interactive development
session, and the engineer can inspect the result afterward.

### SC-08 — Distributed recovery

Eligible work can execute on more than one approved machine and recover from the loss of one machine
without accepting a stale or duplicate completion.

This is a post-core product milestone. It is not required to declare the first local product slice
usable.

### SC-09 — Local operation

The core organizational workflow can complete on one supported local machine without requiring a
paid external inference or hosted data service.

## 7. Product boundaries requiring later specification

The PRD intentionally does not decide:

- How plans are represented or executed.
- Which components own orchestration, persistence, security, or scheduling.
- Which libraries, databases, protocols, or process model are used.
- Exact performance thresholds.
- Exact organization roster and workflow catalogue.
- Exact configuration, event, report, skill, or worker schemas.

Those decisions follow only after this product problem and scope are approved.

## 8. Approved product decisions

1. One run targets one repository and records one immutable base revision; authorized actions may
   produce later revisions whose lineage is captured by the run.
2. Plan execution and revision are risk- and policy-bound. Matching work may proceed unattended;
   only boundary crossings or policy-designated actions require interactive approval.
3. Stateful capabilities, including commit and push, are configurable rather than universally
   forbidden. When granted, they are narrowly scoped, attributable, and reviewable.
4. Distributed execution follows proof of local operation and is not a first-slice acceptance gate.
5. The 32-agent organization is canonical for product version 1 and may evolve only through a new
   versioned organization definition.
