# MISHKAN Planning Governance

These instructions apply to the entire repository.

## Current stage

MISHKAN has completed the framework's final pre-code stage, Sequence 05. Increments I00 and I01
have passed their local and remote acceptance gates and their code and evidence remain unchanged.
Production implementation is paused while proposed PRD 1.4, SRS 1.6, Contract 1.4,
Responsibilities 1.2, System Model 1.2, Architecture 1.2, and Implementation Plan 1.4 pass decisions
D-032 through D-036 in that order. D-036 is the only decision that resumes I02 on
`feat/i02-policy-tools`. The existing registry, policy, gateway, and deterministic enforcement
mechanisms remain accepted narrow evidence; I02 must remove false adapter bindings and add truthful
File/Read/Search, process, and full Bash execution. The rejected universal workflow, mandatory
outcome catalogue, capability-family matrix, static role/tool matrix, competing runtime, and
private operational deny-list must not return.

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
- `docs/PROJECT/PRD.md` is product authority at the version accepted in the decision log; proposed
  PRD 1.4 is non-authoritative until D-032.
- `docs/PROJECT/SRS.md` is behavioral authority at the version accepted in the decision log;
  proposed SRS 1.6 is non-authoritative until D-032.
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
  CrewAI work. Skills do not replace roles, execution-context-specific plans, CrewAI coordination,
  or deterministic capability enforcement, and skill mutations follow the public capability policy.
- Treat tools as first-class typed atomic capabilities resolved from a public versioned registry.
  Tool availability, grouping, and source do not grant authority; exact plan and policy scope is
  enforced before dispatch, and production invocation remains integrated with CrewAI.
- Prefer a small set of general file, terminal/process, web, and browser tools plus configurable
  toolsets and dynamic extensions. Project commands are governed inputs, not synthetic tool types;
  do not introduce a universal capability-family taxonomy or static role/outcome tool matrix.
- Treat the 59 identities as persistent professional profiles and Mission Crews as temporary
  contextual compositions. PM and CTO are agents in the organization; free-form missions may use
  optional templates but never require a universal workflow.
- Require an explicit execution-environment decision for environment-dependent mission work.
  Reuse existing project definitions when compatible; treat Dev Container, Podman, Docker, Nix,
  native-host, and other formats as contextual versioned inputs. Generated descriptors are
  artifacts or governed change sets and never prove runtime readiness by themselves.
- Treat CLI, SDK, chat, TUI, HTTP/SSE, MCP, schedules, Codex, Claude, and other harnesses as clients
  of the same MISHKAN application authority. The TUI may issue governed interventions; no client
  replaces CrewAI or owns stronger policy or authoritative state.
- Keep artifacts immutable, working references compare-and-swap, and terminal, PTY, job, browser,
  and MCP sessions explicitly owned. Availability, discovery, credentials, and instructions never
  grant authority.

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
