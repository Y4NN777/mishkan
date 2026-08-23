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
| [`PROJECT/PRD.md`](PROJECT/PRD.md) | Approved 1.2 | Product problem, scope, success criteria, skills, and controlled capabilities |
| [`PROJECT/SRS.md`](PROJECT/SRS.md) | Approved 1.3 | Canonical verifiable requirements including first-class skills and tools |
| [`PROJECT/DECISION_LOG.md`](PROJECT/DECISION_LOG.md) | Living | Central registry for accepted, open, and superseded decisions |
| [`PROJECT/IMPLEMENTATION_PLAN.md`](PROJECT/IMPLEMENTATION_PLAN.md) | Approved 1.0 | Post-architecture vertical delivery and acceptance plan |
| [`SYSTEM/CONTRACT.md`](SYSTEM/CONTRACT.md) | Approved 1.2 | Sequence 03 promises, invariants, refusals, and abstract dependencies |
| [`SYSTEM/RESPONSIBILITIES.md`](SYSTEM/RESPONSIBILITIES.md) | Approved 1.0 | Sequence 04 primary responsibility ownership and handoffs |
| [`SYSTEM/MODEL.md`](SYSTEM/MODEL.md) | Approved 1.0 | Sequence 05 context, lifecycle, and interaction behavior basis |
| [`SYSTEM/ARCHITECTURE.md`](SYSTEM/ARCHITECTURE.md) | Approved 1.0 | Sequence 05 C4 structure and compact architecture decisions |

## Authority rules

- PRD owns product intent; SRS owns observable requirements.
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
