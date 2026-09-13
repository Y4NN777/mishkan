"""Transactional SQLite authority for organizations, missions, Briefs, and crews."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.identity import new_id
from mishkan.domain.time import utc_now
from mishkan.missions.models import (
    CrewAssignmentKind,
    MissionBrief,
    MissionBriefStatus,
    MissionCrewRevision,
    MissionRecord,
    MissionState,
    MissionTaskAssignment,
    MissionTransition,
)
from mishkan.organization.models import OrganizationRosterDefinition
from mishkan.persistence.migration import SchemaManager
from mishkan.persistence.sqlite import (
    MissionAssignmentRow,
    MissionBriefRow,
    MissionCrewRow,
    MissionRow,
    MissionTransitionRow,
    OrganizationRosterRow,
    OutboxRow,
    create_local_engine,
)

RecordT = TypeVar("RecordT", bound=BaseModel)

_MISSION_TRANSITIONS: dict[MissionState, frozenset[MissionState]] = {
    MissionState.PROPOSED: frozenset({MissionState.CLARIFYING, MissionState.CANCELLED}),
    MissionState.CLARIFYING: frozenset(
        {MissionState.PLANNED, MissionState.BLOCKED, MissionState.CANCELLED}
    ),
    MissionState.PLANNED: frozenset(
        {MissionState.ACTIVE, MissionState.PAUSED, MissionState.BLOCKED, MissionState.CANCELLED}
    ),
    MissionState.ACTIVE: frozenset(
        {
            MissionState.PAUSED,
            MissionState.BLOCKED,
            MissionState.EVALUATING,
            MissionState.FAILED,
            MissionState.CANCELLED,
        }
    ),
    MissionState.PAUSED: frozenset(
        {MissionState.ACTIVE, MissionState.BLOCKED, MissionState.CANCELLED}
    ),
    MissionState.BLOCKED: frozenset(
        {MissionState.ACTIVE, MissionState.PAUSED, MissionState.FAILED, MissionState.CANCELLED}
    ),
    MissionState.EVALUATING: frozenset(
        {
            MissionState.REMEDIATING,
            MissionState.COMPLETED,
            MissionState.FAILED,
            MissionState.PAUSED,
        }
    ),
    MissionState.REMEDIATING: frozenset(
        {
            MissionState.ACTIVE,
            MissionState.EVALUATING,
            MissionState.FAILED,
            MissionState.CANCELLED,
        }
    ),
}


class SQLiteMissionRepository:
    """Persist immutable revisions and update only the mission's current projection."""

    def __init__(self, database_path: Path, *, busy_timeout_ms: int = 5_000) -> None:
        SchemaManager(database_path).require_current()
        self._engine = create_local_engine(database_path, busy_timeout_ms=busy_timeout_ms)

    def record_organization(
        self,
        organization: OrganizationRosterDefinition,
        *,
        emit_event: bool = True,
    ) -> OrganizationRosterDefinition:
        payload = self._json(organization)
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        key = (organization.organization_id, organization.organization_version)
        with Session(self._engine) as session, session.begin():
            existing = session.get(OrganizationRosterRow, key)
            if existing is not None:
                return self._idempotent(existing.payload, payload, organization)
            session.add(
                OrganizationRosterRow(
                    organization_id=organization.organization_id,
                    organization_version=organization.organization_version,
                    fingerprint=fingerprint,
                    payload=payload,
                    recorded_at=utc_now().isoformat(),
                )
            )
            if emit_event:
                self._event(
                    session,
                    aggregate_id=organization.organization_id,
                    entity_type="organization",
                    event_type="organization.roster_recorded",
                    payload={
                        "organization_id": organization.organization_id,
                        "organization_version": organization.organization_version,
                        "fingerprint": fingerprint,
                        "identity_count": len(organization.identities),
                    },
                )
        return organization

    def create_mission(self, record: MissionRecord) -> MissionRecord:
        if record.revision != 0 or record.state is not MissionState.PROPOSED:
            raise MishkanError(
                ErrorCode.MISSION,
                "new mission must begin as an unpersisted proposed record",
            )
        durable = record.model_copy(update={"revision": 1})
        payload = self._json(durable)
        with Session(self._engine) as session, session.begin():
            self._require_organization(
                session,
                durable.organization_id,
                durable.organization_version,
            )
            existing = session.get(MissionRow, str(durable.mission_id))
            if existing is not None:
                return self._idempotent(existing.payload, payload, durable)
            session.add(
                MissionRow(
                    id=str(durable.mission_id),
                    state=durable.state.value,
                    revision=durable.revision,
                    organization_id=durable.organization_id,
                    organization_version=durable.organization_version,
                    current_brief_version=None,
                    current_crew_version=None,
                    payload=payload,
                    created_at=durable.created_at.isoformat(),
                    updated_at=durable.updated_at.isoformat(),
                )
            )
            self._event(
                session,
                aggregate_id=str(durable.mission_id),
                entity_type="mission",
                event_type="mission.proposed",
                payload={
                    "mission_id": str(durable.mission_id),
                    "origin_kind": durable.origin.kind.value,
                    "actor_id": durable.origin.actor_id,
                    "revision": durable.revision,
                },
            )
        return durable

    def record_brief(self, brief: MissionBrief, *, expected_revision: int) -> MissionBrief:
        payload = self._json(brief)
        with Session(self._engine) as session, session.begin():
            existing = session.get(MissionBriefRow, str(brief.brief_id))
            if existing is not None:
                return self._idempotent(existing.payload, payload, brief)
            mission_row = self._require_mission_row(session, str(brief.mission_id))
            mission = MissionRecord.model_validate_json(mission_row.payload)
            self._require_revision(mission, expected_revision)
            if mission.state in {
                MissionState.COMPLETED,
                MissionState.FAILED,
                MissionState.CANCELLED,
            }:
                raise MishkanError(ErrorCode.MISSION, "terminal mission cannot accept a new Brief")
            if (
                brief.organization_id != mission.organization_id
                or brief.organization_version != mission.organization_version
            ):
                raise MishkanError(ErrorCode.MISSION, "Mission Brief organization does not match")
            expected_version = (mission.current_brief_version or 0) + 1
            if brief.version != expected_version:
                raise MishkanError(
                    ErrorCode.REVISION_MISMATCH,
                    "Mission Brief version is stale or skips a revision",
                    details={"expected": expected_version, "received": brief.version},
                )
            roster = self._require_organization(
                session,
                mission.organization_id,
                mission.organization_version,
            )
            known = {identity.identity_id for identity in roster.identities}
            unknown = set(brief.proposed_crew) - known
            if unknown:
                raise MishkanError(
                    ErrorCode.MISSION,
                    "Mission Brief proposes unknown professional identities",
                    details={"unknown": sorted(unknown)},
                )
            version_row = session.scalar(
                select(MissionBriefRow).where(
                    MissionBriefRow.mission_id == str(brief.mission_id),
                    MissionBriefRow.version == brief.version,
                )
            )
            if version_row is not None:
                raise MishkanError(
                    ErrorCode.DUPLICATE_RESULT,
                    "Mission Brief version already has another immutable identity",
                )
            session.add(
                MissionBriefRow(
                    brief_id=str(brief.brief_id),
                    mission_id=str(brief.mission_id),
                    version=brief.version,
                    status=brief.status.value,
                    fingerprint=brief.fingerprint,
                    payload=payload,
                    created_at=brief.created_at.isoformat(),
                )
            )
            updated = mission.model_copy(
                update={
                    "revision": mission.revision + 1,
                    "state": MissionState.CLARIFYING,
                    "current_brief_version": brief.version,
                    "updated_at": utc_now(),
                }
            )
            self._update_mission_row(mission_row, updated)
            self._event(
                session,
                aggregate_id=str(brief.mission_id),
                entity_type="mission",
                event_type="mission.brief_recorded",
                payload={
                    "mission_id": str(brief.mission_id),
                    "brief_id": str(brief.brief_id),
                    "brief_version": brief.version,
                    "status": brief.status.value,
                    "fingerprint": brief.fingerprint,
                    "revision": updated.revision,
                },
            )
        return brief

    def record_crew(
        self, crew: MissionCrewRevision, *, expected_revision: int
    ) -> MissionCrewRevision:
        payload = self._json(crew)
        with Session(self._engine) as session, session.begin():
            existing = session.get(MissionCrewRow, str(crew.crew_id))
            if existing is not None:
                return self._idempotent(existing.payload, payload, crew)
            mission_row = self._require_mission_row(session, str(crew.mission_id))
            mission = MissionRecord.model_validate_json(mission_row.payload)
            self._require_revision(mission, expected_revision)
            if crew.organization_id != mission.organization_id or (
                crew.organization_version != mission.organization_version
            ):
                raise MishkanError(ErrorCode.MISSION, "Mission Crew organization does not match")
            if crew.brief_version != mission.current_brief_version:
                raise MishkanError(
                    ErrorCode.REVISION_MISMATCH,
                    "Mission Crew does not reference the current Mission Brief",
                )
            brief = self._brief_row(session, str(crew.mission_id), crew.brief_version)
            if brief.status is not MissionBriefStatus.CONFIRMED:
                raise MishkanError(
                    ErrorCode.MISSION,
                    "Mission Crew requires a jointly confirmed Mission Brief",
                )
            if brief.pm_confirmation is None or brief.cto_confirmation is None:
                raise MishkanError(ErrorCode.MISSION, "Mission Brief confirmations are incomplete")
            if crew.pm_composition_confirmation_id != brief.pm_confirmation.confirmation_id or (
                crew.cto_coverage_confirmation_id != brief.cto_confirmation.confirmation_id
            ):
                raise MishkanError(
                    ErrorCode.MISSION,
                    "Mission Crew confirmation lineage does not match its Brief",
                )
            expected_version = (mission.current_crew_version or 0) + 1
            if crew.version != expected_version:
                raise MishkanError(
                    ErrorCode.REVISION_MISMATCH,
                    "Mission Crew version is stale or skips a revision",
                    details={"expected": expected_version, "received": crew.version},
                )
            if {member.identity_id for member in crew.members} != set(brief.proposed_crew):
                raise MishkanError(
                    ErrorCode.MISSION,
                    "Mission Crew must match the current PM/CTO-confirmed composition",
                )
            roster = self._require_organization(
                session,
                mission.organization_id,
                mission.organization_version,
            )
            identities = {identity.identity_id: identity for identity in roster.identities}
            for member in crew.members:
                identity = identities.get(member.identity_id)
                if identity is None:
                    raise MishkanError(ErrorCode.MISSION, "Mission Crew identity is unknown")
                if (
                    member.identity_id == crew.mission_lead_id
                    and not identity.mission_lead_eligible
                ):
                    raise MishkanError(
                        ErrorCode.ROLE_CONFLICT,
                        "selected professional identity cannot hold Mission Lead responsibility",
                    )
                self._require_assignment_authority(
                    member.assignment_kind,
                    identity.authority_limits,
                )
            version_row = session.scalar(
                select(MissionCrewRow).where(
                    MissionCrewRow.mission_id == str(crew.mission_id),
                    MissionCrewRow.version == crew.version,
                )
            )
            if version_row is not None:
                raise MishkanError(
                    ErrorCode.DUPLICATE_RESULT,
                    "Mission Crew version already has another immutable identity",
                )
            session.add(
                MissionCrewRow(
                    crew_id=str(crew.crew_id),
                    mission_id=str(crew.mission_id),
                    version=crew.version,
                    brief_version=crew.brief_version,
                    fingerprint=crew.fingerprint,
                    payload=payload,
                    created_at=crew.created_at.isoformat(),
                )
            )
            updated = mission.model_copy(
                update={
                    "revision": mission.revision + 1,
                    "current_crew_version": crew.version,
                    "updated_at": utc_now(),
                }
            )
            self._update_mission_row(mission_row, updated)
            self._event(
                session,
                aggregate_id=str(crew.mission_id),
                entity_type="mission",
                event_type="mission.crew_recorded",
                payload={
                    "mission_id": str(crew.mission_id),
                    "crew_id": str(crew.crew_id),
                    "crew_version": crew.version,
                    "brief_version": crew.brief_version,
                    "mission_lead_id": crew.mission_lead_id,
                    "fingerprint": crew.fingerprint,
                    "revision": updated.revision,
                },
            )
        return crew

    def record_assignment(self, assignment: MissionTaskAssignment) -> MissionTaskAssignment:
        payload = self._json(assignment)
        with Session(self._engine) as session, session.begin():
            existing = session.get(MissionAssignmentRow, str(assignment.assignment_id))
            if existing is not None:
                return self._idempotent(existing.payload, payload, assignment)
            mission = MissionRecord.model_validate_json(
                self._require_mission_row(session, str(assignment.mission_id)).payload
            )
            if assignment.crew_version != mission.current_crew_version:
                raise MishkanError(
                    ErrorCode.REVISION_MISMATCH,
                    "task assignment does not reference the current Mission Crew",
                )
            crew = self._crew_row(session, str(assignment.mission_id), assignment.crew_version)
            members = {member.identity_id for member in crew.members}
            assigned = {assignment.accountable_owner, *assignment.contributors}
            unknown = assigned - members
            if unknown:
                raise MishkanError(
                    ErrorCode.MISSION,
                    "task assignment contains identities outside the current Mission Crew",
                    details={"unknown": sorted(unknown)},
                )
            expected_revision = 1 + (
                session.scalar(
                    select(MissionAssignmentRow.assignment_revision)
                    .where(
                        MissionAssignmentRow.mission_id == str(assignment.mission_id),
                        MissionAssignmentRow.task_id == assignment.task_id,
                    )
                    .order_by(MissionAssignmentRow.assignment_revision.desc())
                    .limit(1)
                )
                or 0
            )
            if assignment.assignment_revision != expected_revision:
                raise MishkanError(
                    ErrorCode.REVISION_MISMATCH,
                    "task assignment revision is stale or skips a revision",
                    details={
                        "expected": expected_revision,
                        "received": assignment.assignment_revision,
                    },
                )
            session.add(
                MissionAssignmentRow(
                    id=str(assignment.assignment_id),
                    mission_id=str(assignment.mission_id),
                    task_id=assignment.task_id,
                    assignment_revision=assignment.assignment_revision,
                    accountable_owner=assignment.accountable_owner,
                    payload=payload,
                    created_at=assignment.created_at.isoformat(),
                )
            )
            self._event(
                session,
                aggregate_id=str(assignment.mission_id),
                entity_type="mission",
                event_type="mission.task_assigned",
                payload={
                    "assignment_id": str(assignment.assignment_id),
                    "task_id": assignment.task_id,
                    "assignment_revision": assignment.assignment_revision,
                    "accountable_owner": assignment.accountable_owner,
                    "crew_version": assignment.crew_version,
                },
            )
        return assignment

    def transition(
        self,
        transition: MissionTransition,
        *,
        expected_revision: int,
    ) -> MissionTransition:
        payload = self._json(transition)
        with Session(self._engine) as session, session.begin():
            existing = session.get(MissionTransitionRow, str(transition.transition_id))
            if existing is not None:
                return self._idempotent(existing.payload, payload, transition)
            mission_row = self._require_mission_row(session, str(transition.mission_id))
            mission = MissionRecord.model_validate_json(mission_row.payload)
            self._require_revision(mission, expected_revision)
            if transition.from_state is not mission.state:
                raise MishkanError(
                    ErrorCode.REVISION_MISMATCH,
                    "mission transition starts from a stale state",
                    details={
                        "expected": mission.state.value,
                        "received": transition.from_state.value,
                    },
                )
            allowed = _MISSION_TRANSITIONS.get(mission.state, frozenset())
            if transition.to_state not in allowed:
                raise MishkanError(
                    ErrorCode.MISSION,
                    "mission lifecycle transition is not permitted",
                    details={
                        "from": mission.state.value,
                        "to": transition.to_state.value,
                    },
                )
            if transition.to_state not in {
                MissionState.CLARIFYING,
                MissionState.CANCELLED,
            } and (mission.current_brief_version is None or mission.current_crew_version is None):
                raise MishkanError(
                    ErrorCode.MISSION,
                    "mission cannot advance without a current Brief and Mission Crew",
                )
            if transition.to_state is MissionState.ACTIVE:
                assignment = session.scalar(
                    select(MissionAssignmentRow.id)
                    .where(MissionAssignmentRow.mission_id == str(mission.mission_id))
                    .limit(1)
                )
                if assignment is None:
                    raise MishkanError(
                        ErrorCode.MISSION,
                        "mission cannot become active without an accountable task assignment",
                    )
            updated = mission.model_copy(
                update={
                    "state": transition.to_state,
                    "revision": mission.revision + 1,
                    "updated_at": utc_now(),
                }
            )
            self._update_mission_row(mission_row, updated)
            session.add(
                MissionTransitionRow(
                    id=str(transition.transition_id),
                    mission_id=str(transition.mission_id),
                    from_state=transition.from_state.value,
                    to_state=transition.to_state.value,
                    payload=payload,
                    created_at=transition.created_at.isoformat(),
                )
            )
            self._event(
                session,
                aggregate_id=str(transition.mission_id),
                entity_type="mission",
                event_type="mission.state_transitioned",
                payload={
                    "transition_id": str(transition.transition_id),
                    "from_state": transition.from_state.value,
                    "to_state": transition.to_state.value,
                    "actor_or_cause": transition.actor_or_cause,
                    "reason": transition.reason,
                    "decision_id": (
                        str(transition.decision_id) if transition.decision_id is not None else None
                    ),
                    "affected_scope": list(transition.affected_scope),
                    "evidence_references": list(transition.evidence_references),
                    "revision": updated.revision,
                },
            )
        return transition

    def assignments(
        self, mission_id: str, *, limit: int = 1_000
    ) -> tuple[MissionTaskAssignment, ...]:
        with Session(self._engine) as session:
            self._require_mission_row(session, mission_id)
            rows = session.scalars(
                select(MissionAssignmentRow)
                .where(MissionAssignmentRow.mission_id == mission_id)
                .order_by(
                    MissionAssignmentRow.task_id,
                    MissionAssignmentRow.assignment_revision,
                )
                .limit(limit)
            )
            return tuple(MissionTaskAssignment.model_validate_json(row.payload) for row in rows)

    def transitions(self, mission_id: str, *, limit: int = 1_000) -> tuple[MissionTransition, ...]:
        with Session(self._engine) as session:
            self._require_mission_row(session, mission_id)
            rows = session.scalars(
                select(MissionTransitionRow)
                .where(MissionTransitionRow.mission_id == mission_id)
                .order_by(MissionTransitionRow.created_at)
                .limit(limit)
            )
            return tuple(MissionTransition.model_validate_json(row.payload) for row in rows)

    def mission(self, mission_id: str) -> MissionRecord:
        with Session(self._engine) as session:
            return MissionRecord.model_validate_json(
                self._require_mission_row(session, mission_id).payload
            )

    def brief(self, mission_id: str, version: int | None = None) -> MissionBrief:
        with Session(self._engine) as session:
            mission = MissionRecord.model_validate_json(
                self._require_mission_row(session, mission_id).payload
            )
            selected = version or mission.current_brief_version
            if selected is None:
                raise MishkanError(ErrorCode.MISSION, "mission has no Mission Brief")
            return self._brief_row(session, mission_id, selected)

    def crew(self, mission_id: str, version: int | None = None) -> MissionCrewRevision:
        with Session(self._engine) as session:
            mission = MissionRecord.model_validate_json(
                self._require_mission_row(session, mission_id).payload
            )
            selected = version or mission.current_crew_version
            if selected is None:
                raise MishkanError(ErrorCode.MISSION, "mission has no Mission Crew")
            row = session.scalar(
                select(MissionCrewRow).where(
                    MissionCrewRow.mission_id == mission_id,
                    MissionCrewRow.version == selected,
                )
            )
            if row is None:
                raise MishkanError(ErrorCode.MISSION, "Mission Crew revision does not exist")
            return MissionCrewRevision.model_validate_json(row.payload)

    def list_missions(self, *, limit: int = 100) -> tuple[MissionRecord, ...]:
        if not 1 <= limit <= 1_000:
            raise MishkanError(ErrorCode.OUTPUT_CONTRACT, "mission query limit is out of bounds")
        with Session(self._engine) as session:
            rows = session.scalars(
                select(MissionRow).order_by(MissionRow.updated_at.desc()).limit(limit)
            ).all()
            return tuple(MissionRecord.model_validate_json(row.payload) for row in rows)

    @staticmethod
    def _require_assignment_authority(
        assignment: CrewAssignmentKind, authority_limits: tuple[str, ...]
    ) -> None:
        required_prefix = {
            CrewAssignmentKind.EVALUATION: "evaluation.",
            CrewAssignmentKind.REPORTING: "reporting.",
            CrewAssignmentKind.AUDIT: "audit.",
        }.get(assignment)
        if required_prefix is not None and not any(
            authority.startswith(required_prefix) for authority in authority_limits
        ):
            raise MishkanError(
                ErrorCode.ROLE_CONFLICT,
                "professional profile does not cover the assigned independent responsibility",
                details={"assignment_kind": assignment.value},
            )

    @staticmethod
    def _require_revision(mission: MissionRecord, expected_revision: int) -> None:
        if mission.revision != expected_revision:
            raise MishkanError(
                ErrorCode.REVISION_MISMATCH,
                "mission revision changed before mutation",
                details={"expected": expected_revision, "current": mission.revision},
            )

    @staticmethod
    def _require_mission_row(session: Session, mission_id: str) -> MissionRow:
        row = session.get(MissionRow, mission_id)
        if row is None:
            raise MishkanError(ErrorCode.MISSION, "mission does not exist")
        return row

    @staticmethod
    def _require_organization(
        session: Session, organization_id: str, organization_version: str
    ) -> OrganizationRosterDefinition:
        row = session.get(OrganizationRosterRow, (organization_id, organization_version))
        if row is None:
            raise MishkanError(
                ErrorCode.MISSION,
                "mission organization version is not registered",
            )
        return OrganizationRosterDefinition.model_validate_json(row.payload)

    @staticmethod
    def _brief_row(session: Session, mission_id: str, version: int) -> MissionBrief:
        row = session.scalar(
            select(MissionBriefRow).where(
                MissionBriefRow.mission_id == mission_id,
                MissionBriefRow.version == version,
            )
        )
        if row is None:
            raise MishkanError(ErrorCode.MISSION, "Mission Brief revision does not exist")
        return MissionBrief.model_validate_json(row.payload)

    @staticmethod
    def _crew_row(session: Session, mission_id: str, version: int) -> MissionCrewRevision:
        row = session.scalar(
            select(MissionCrewRow).where(
                MissionCrewRow.mission_id == mission_id,
                MissionCrewRow.version == version,
            )
        )
        if row is None:
            raise MishkanError(ErrorCode.MISSION, "Mission Crew revision does not exist")
        return MissionCrewRevision.model_validate_json(row.payload)

    @staticmethod
    def _update_mission_row(row: MissionRow, record: MissionRecord) -> None:
        row.state = record.state.value
        row.revision = record.revision
        row.current_brief_version = record.current_brief_version
        row.current_crew_version = record.current_crew_version
        row.payload = SQLiteMissionRepository._json(record)
        row.updated_at = record.updated_at.isoformat()

    @staticmethod
    def _idempotent(existing: str, requested: str, record: RecordT) -> RecordT:
        if existing != requested:
            raise MishkanError(
                ErrorCode.DUPLICATE_RESULT,
                "durable mission identity already contains different content",
            )
        return record

    @staticmethod
    def _json(record: BaseModel) -> str:
        return json.dumps(record.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _event(
        session: Session,
        *,
        aggregate_id: str,
        entity_type: str,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        session.add(
            OutboxRow(
                id=str(new_id()),
                schema_version="1.0",
                aggregate_id=aggregate_id,
                entity_type=entity_type,
                run_id=None,
                task_id=None,
                identity_id=None,
                team_id=None,
                security_relevant=False,
                event_type=event_type,
                source="mishkan.missions",
                payload=json.dumps(payload, sort_keys=True, separators=(",", ":")),
                occurred_at=utc_now().isoformat(),
                command_id=None,
                correlation_id=None,
                causation_id=None,
                sensitivity="internal",
                published_at=None,
            )
        )
