# MISHKAN Documentation

This index is the entry point for durable MISHKAN documentation.

## Domains

| Domain | Authority |
|---|---|
| `PROJECT` | Product intent, approved requirements, decisions, and—when ready—the delivery roadmap |
| `SYSTEM` | Approved contracts, models, architecture, and system design derived from requirements |
| `VALIDATION` | Factual test and runtime evidence created only when execution evidence exists |
| `OPERATIONS` | Deployment, migration, recovery, and runbooks created only for real environments |

## Current documents

| Document | Status | Role |
|---|---|---|
| [`PROJECT/PRD.md`](PROJECT/PRD.md) | Accepted 1.4 — D-032 | Product problem, free-form missions, mission-scoped environments, 59-identity organization, executive control, and contextual capabilities |
| [`PROJECT/SRS.md`](PROJECT/SRS.md) | Accepted 1.6 — D-032 | Canonical verifiable requirements and complete PRD traceability |
| [`PROJECT/DECISION_LOG.md`](PROJECT/DECISION_LOG.md) | Living | Central registry for accepted, open, and superseded decisions |
| [`PROJECT/IMPLEMENTATION_PLAN.md`](PROJECT/IMPLEMENTATION_PLAN.md) | Accepted 1.4 — D-036 | I00/I01 retention and vertical I02–I11 delivery, acceptance, and Git-flow plan |
| [`SYSTEM/CONTRACT.md`](SYSTEM/CONTRACT.md) | Accepted 1.4 — D-033 | Sequence 03 promises, invariants, refusals, sessions, artifacts, and mediation |
| [`SYSTEM/RESPONSIBILITIES.md`](SYSTEM/RESPONSIBILITIES.md) | Accepted 1.2 — D-034 | Sequence 04 exact primary ownership and handoffs for RSP-001–026 |
| [`SYSTEM/MODEL.md`](SYSTEM/MODEL.md) | Accepted 1.2 — D-035 | Mission-centered Sequence 05 behavior, including agent-authored environment planning and governed generation |
| [`SYSTEM/ARCHITECTURE.md`](SYSTEM/ARCHITECTURE.md) | Accepted 1.2 — D-035 | Transactional modular-monolith structure, mission environment boundary, and architecture decisions |
| [`VALIDATION/I00.md`](VALIDATION/I00.md) | Passed | Observed local and remote acceptance evidence for the I00 foundation |
| [`VALIDATION/I01.md`](VALIDATION/I01.md) | Passed | Observed local and remote acceptance evidence for real CrewAI execution |
| [`VALIDATION/I02.md`](VALIDATION/I02.md) | Reopened | Narrow mechanism evidence and remaining corrected I02 acceptance conditions |

## Authority rules

- PRD owns product intent; SRS owns observable requirements after their recorded gates. Proposed
  amendments never silently replace the last accepted baseline.
- The decision log owns decision status. A separate approval file is not created for each gate.
- System documents derive from accepted requirements and decisions; they do not replace them.
- Validation contains observed evidence, not self-review or speculative checklists.
- Operations documents are created only when tied to an actual environment or procedure.
- Working notes, source comparisons, gate checklists, and drafting reviews stay in the task, `/tmp`,
  or an external work area and are not versioned as project documentation.
- Obsolete material is removed from the active tree. Git history is not a documentation domain.

## Documentation lifecycle

1. Discuss and investigate without creating a durable file.
2. Promote only an accepted requirement, decision, contract, model, procedure, or observed result.
3. Put it in the domain that owns its authority.
4. Update the central decision log when its status or meaning changes.
5. Supersede or remove obsolete documents instead of retaining parallel versions.
