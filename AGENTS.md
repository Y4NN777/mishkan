# MISHKAN Planning Governance

These instructions apply to the entire repository.

## Current stage

MISHKAN has completed the framework's final pre-code stage, Sequence 05, and Implementation Plan
1.1 is approved. Increment I00 has passed its local and remote acceptance gates. Increment I01 is
authorized on `feat/i01-local-crewai`; work outside I01 requires the owning increment to begin under
the progressive delivery protocol in the approved plan.

## Mandatory method

Follow the numbered `SWE-BASICS-BEFORE-CODE` chain without skipping or reordering stages:

0. Foundation and engineering mindset
1. Requirements: PRD
2. Requirements: SRS
3. Design: contract and invariants
4. Transition: requirements to architecture through explicit responsibilities
5. Modeling: behavioral UML, structural C4, and reviewed architecture decisions

The cited framework ends at Sequence 05. Implementation planning and coding follow the approved
architecture but are not additional numbered stages of that framework.

The active stage may use later concepts only as explicitly labeled candidate constraints. It must
not silently turn them into approved architecture.

## Source authority

- The original MISHKAN SPEC and SRS attachments are historical discovery sources.
- `docs/PROJECT/PRD.md` is the approved product authority.
- `docs/PROJECT/SRS.md` is the approved behavioral authority.
- `docs/PROJECT/DECISION_LOG.md` is the only decision-status registry.
- `docs/SYSTEM/CONTRACT.md` owns invariants and refusals after Gate G3 approval.
- ADRs own durable implementation decisions.
- When sources conflict, record the conflict. Never resolve it silently in code.

## Writing rules

- PRD statements describe problems, actors, capabilities, exclusions, and measurable outcomes.
- PRD statements must not prescribe libraries, languages, databases, APIs, processes, or topology.
- Every SRS requirement must be uniquely identified, binary, testable, and state its failure
  behavior where relevant.
- Use RFC 2119 vocabulary consistently: MUST, MUST NOT, SHOULD, MAY.
- Separate requirements, technical constraints, policies, defaults, and implementation decisions.
- Do not call a subjective adjective such as fast, safe, intuitive, reliable, or scalable a
  requirement without a measurable threshold.
- Every guarantee receives one primary responsibility owner before components are designed.
- Model behavior before structure. Draw only diagrams that answer a concrete design question.
- Treat skills as first-class portable procedural memory loaded progressively into authorized
  CrewAI work. Skills do not replace roles, repository-specific plans, CrewAI coordination, or
  deterministic capability enforcement, and skill mutations follow the public capability policy.
- Treat tools as first-class typed atomic capabilities resolved from a public versioned registry.
  Tool availability, grouping, and source do not grant authority; exact plan and policy scope is
  enforced before dispatch, and production invocation remains integrated with CrewAI.

## Change discipline

- Keep planning commits small and reviewable.
- Branch from `develop` using `feat/*`, `fix/*`, `docs/*`, `test/*`, `refactor/*`, or `chore/*`.
- Merge topic branches into `develop` after their gate; promote only `develop` into `main` after the
  integrated gate. Never merge a topic branch directly into `main`.
- Record assumptions and unresolved questions explicitly.
- Do not modify or erase historical source attachments.
- Do not force-push or rewrite repository history without explicit engineer authorization.
- A planning approval does not authorize implementation unless the engineer says to begin coding.

## Documentation discipline

- Version only durable requirements, decisions, contracts, models, procedures, or factual evidence.
- Keep source comparisons, assumptions, self-reviews, gate checklists, and temporary analysis in the
  task, `/tmp`, or an external work area.
- Do not create a separate approval or review document when the central decision log is sufficient.
- Organize durable documentation by authority domain, not by the order in which a task created it.
- Remove or supersede obsolete material instead of maintaining parallel active documents.
