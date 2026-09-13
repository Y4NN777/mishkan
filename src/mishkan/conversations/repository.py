"""Transactional authority for durable communication and CEO interventions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from mishkan.conversations.models import (
    ChannelClass,
    ConversationChannel,
    ConversationMessage,
    EscalationState,
    InterventionKind,
    InterventionTargetKind,
    MissionDecision,
    MissionEscalation,
    MissionIntervention,
)
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.identity import new_id
from mishkan.domain.time import utc_now
from mishkan.missions import MissionRecord, MissionState
from mishkan.persistence.migration import SchemaManager
from mishkan.persistence.sqlite import (
    ConversationChannelRow,
    ConversationMessageRow,
    MissionDecisionRow,
    MissionEscalationRow,
    MissionInterventionRow,
    MissionRow,
    OutboxRow,
    create_local_engine,
)

RecordT = TypeVar("RecordT", bound=BaseModel)


class SQLiteConversationRepository:
    def __init__(self, database_path: Path, *, busy_timeout_ms: int = 5_000) -> None:
        SchemaManager(database_path).require_current()
        self._engine = create_local_engine(database_path, busy_timeout_ms=busy_timeout_ms)

    def create_channel(self, channel: ConversationChannel) -> ConversationChannel:
        payload = self._json(channel)
        with Session(self._engine) as session, session.begin():
            existing = session.get(ConversationChannelRow, str(channel.conversation_id))
            if existing is not None:
                return self._idempotent(existing.payload, payload, channel)
            if channel.mission_id is not None:
                self._require_mission(session, str(channel.mission_id))
            if channel.channel_class is ChannelClass.EXECUTIVE:
                prior = session.scalar(
                    select(ConversationChannelRow).where(
                        ConversationChannelRow.channel_class == ChannelClass.EXECUTIVE.value
                    )
                )
                if prior is not None:
                    raise MishkanError(
                        ErrorCode.MISSION,
                        "the organization already has a durable Executive channel",
                        details={"conversation_id": prior.id},
                    )
            session.add(
                ConversationChannelRow(
                    id=str(channel.conversation_id),
                    channel_class=channel.channel_class.value,
                    mission_id=str(channel.mission_id) if channel.mission_id is not None else None,
                    branch_id=channel.branch_id,
                    payload=payload,
                    created_at=channel.created_at.isoformat(),
                )
            )
            self._event(
                session,
                aggregate_id=str(channel.conversation_id),
                entity_type="conversation",
                event_type="conversation.created",
                payload={
                    "conversation_id": str(channel.conversation_id),
                    "channel_class": channel.channel_class.value,
                    "mission_id": (
                        str(channel.mission_id) if channel.mission_id is not None else None
                    ),
                },
            )
        return channel

    def post_message(self, message: ConversationMessage) -> ConversationMessage:
        payload = self._json(message)
        with Session(self._engine) as session, session.begin():
            existing = session.get(ConversationMessageRow, str(message.message_id))
            if existing is not None:
                return self._idempotent(existing.payload, payload, message)
            channel = self._require_channel(session, str(message.conversation_id))
            contract = ConversationChannel.model_validate_json(channel.payload)
            if message.author_identity not in contract.participants:
                raise MishkanError(
                    ErrorCode.AUTHORITY_NOT_GRANTED,
                    "message author is not a participant in the conversation",
                )
            if message.reply_to_message_id is not None:
                replied = session.get(ConversationMessageRow, str(message.reply_to_message_id))
                if replied is None or replied.conversation_id != str(message.conversation_id):
                    raise MishkanError(
                        ErrorCode.MISSION,
                        "reply target is not part of this conversation",
                    )
            session.add(
                ConversationMessageRow(
                    id=str(message.message_id),
                    conversation_id=str(message.conversation_id),
                    author_identity=message.author_identity,
                    payload=payload,
                    created_at=message.created_at.isoformat(),
                )
            )
            self._event(
                session,
                aggregate_id=str(message.conversation_id),
                entity_type="conversation",
                event_type="conversation.message_posted",
                payload={
                    "conversation_id": str(message.conversation_id),
                    "message_id": str(message.message_id),
                    "author_identity": message.author_identity,
                    "evidence_references": list(message.evidence_references),
                },
            )
        return message

    def record_decision(self, decision: MissionDecision) -> MissionDecision:
        return self._record_mission_fact(
            decision,
            identity=str(decision.decision_id),
            mission_id=str(decision.mission_id),
            conversation_id=str(decision.conversation_id),
            event_type="mission.decision_recorded",
            event_payload={
                "decision_id": str(decision.decision_id),
                "actor_id": decision.actor_id,
                "scope": list(decision.scope),
            },
        )

    def create_escalation(self, escalation: MissionEscalation) -> MissionEscalation:
        if escalation.state is not EscalationState.OPEN:
            raise MishkanError(ErrorCode.MISSION, "new escalation must begin open")
        payload = self._json(escalation)
        with Session(self._engine) as session, session.begin():
            existing = session.get(MissionEscalationRow, str(escalation.escalation_id))
            if existing is not None:
                return self._idempotent(existing.payload, payload, escalation)
            self._require_mission_channel(
                session, str(escalation.mission_id), str(escalation.conversation_id)
            )
            session.add(
                MissionEscalationRow(
                    id=str(escalation.escalation_id),
                    mission_id=str(escalation.mission_id),
                    conversation_id=str(escalation.conversation_id),
                    state=escalation.state.value,
                    payload=payload,
                    created_at=escalation.created_at.isoformat(),
                    updated_at=escalation.updated_at.isoformat(),
                )
            )
            self._event(
                session,
                aggregate_id=str(escalation.mission_id),
                entity_type="mission",
                event_type="mission.escalation_opened",
                payload={
                    "escalation_id": str(escalation.escalation_id),
                    "blocked_scope": list(escalation.blocked_scope),
                    "independent_work_continuing": list(escalation.independent_work_continuing),
                },
            )
        return escalation

    def apply_intervention(
        self,
        intervention: MissionIntervention,
        *,
        expected_revision: int,
    ) -> MissionIntervention:
        payload = self._json(intervention)
        with Session(self._engine) as session, session.begin():
            existing = session.get(MissionInterventionRow, str(intervention.intervention_id))
            if existing is not None:
                return self._idempotent(existing.payload, payload, intervention)
            mission_row = self._require_mission(session, str(intervention.mission_id))
            mission = MissionRecord.model_validate_json(mission_row.payload)
            if mission.revision != expected_revision:
                raise MishkanError(
                    ErrorCode.REVISION_MISMATCH,
                    "mission intervention expected revision is stale",
                    details={"expected": mission.revision, "received": expected_revision},
                )
            self._require_mission_channel(
                session, str(intervention.mission_id), str(intervention.conversation_id)
            )
            if intervention.escalation_id is not None:
                escalation_row = session.get(MissionEscalationRow, str(intervention.escalation_id))
                if escalation_row is None or escalation_row.mission_id != str(
                    intervention.mission_id
                ):
                    raise MishkanError(
                        ErrorCode.MISSION,
                        "intervention escalation is absent or belongs to another mission",
                    )
                escalation = MissionEscalation.model_validate_json(escalation_row.payload)
                if escalation.state is not EscalationState.OPEN:
                    raise MishkanError(ErrorCode.MISSION, "escalation is already settled")
                answered = escalation.model_copy(
                    update={
                        "state": EscalationState.ANSWERED,
                        "answer_intervention_id": intervention.intervention_id,
                        "updated_at": utc_now(),
                    }
                )
                escalation_row.state = answered.state.value
                escalation_row.payload = self._json(answered)
                escalation_row.updated_at = answered.updated_at.isoformat()
            next_state = self._transition_state(mission, intervention)
            updated = mission.model_copy(
                update={
                    "state": next_state or mission.state,
                    "revision": mission.revision + 1,
                    "updated_at": utc_now(),
                }
            )
            mission_row.state = updated.state.value
            mission_row.revision = updated.revision
            mission_row.payload = self._json(updated)
            mission_row.updated_at = updated.updated_at.isoformat()
            session.add(
                MissionInterventionRow(
                    id=str(intervention.intervention_id),
                    mission_id=str(intervention.mission_id),
                    conversation_id=str(intervention.conversation_id),
                    kind=intervention.kind.value,
                    payload=payload,
                    created_at=intervention.created_at.isoformat(),
                )
            )
            self._event(
                session,
                aggregate_id=str(intervention.mission_id),
                entity_type="mission",
                event_type="mission.intervention_applied",
                payload={
                    "intervention_id": str(intervention.intervention_id),
                    "actor_id": intervention.actor_id,
                    "kind": intervention.kind.value,
                    "target_kind": intervention.target_kind.value,
                    "scope": list(intervention.scope),
                    "resulting_mission_state": (
                        next_state.value if next_state is not None else mission.state.value
                    ),
                },
            )
        return intervention

    def channel(self, conversation_id: str) -> ConversationChannel:
        with Session(self._engine) as session:
            return ConversationChannel.model_validate_json(
                self._require_channel(session, conversation_id).payload
            )

    def channels(
        self, *, mission_id: str | None = None, limit: int = 100
    ) -> tuple[ConversationChannel, ...]:
        with Session(self._engine) as session:
            query = select(ConversationChannelRow)
            if mission_id is not None:
                query = query.where(ConversationChannelRow.mission_id == mission_id)
            rows = session.scalars(query.order_by(ConversationChannelRow.created_at).limit(limit))
            return tuple(ConversationChannel.model_validate_json(row.payload) for row in rows)

    def messages(
        self, conversation_id: str, *, limit: int = 100
    ) -> tuple[ConversationMessage, ...]:
        with Session(self._engine) as session:
            self._require_channel(session, conversation_id)
            rows = session.scalars(
                select(ConversationMessageRow)
                .where(ConversationMessageRow.conversation_id == conversation_id)
                .order_by(ConversationMessageRow.created_at)
                .limit(limit)
            )
            return tuple(ConversationMessage.model_validate_json(row.payload) for row in rows)

    def escalations(
        self,
        mission_id: str,
        *,
        state: EscalationState | None = None,
        limit: int = 100,
    ) -> tuple[MissionEscalation, ...]:
        with Session(self._engine) as session:
            self._require_mission(session, mission_id)
            query = select(MissionEscalationRow).where(
                MissionEscalationRow.mission_id == mission_id
            )
            if state is not None:
                query = query.where(MissionEscalationRow.state == state.value)
            rows = session.scalars(query.order_by(MissionEscalationRow.created_at).limit(limit))
            return tuple(MissionEscalation.model_validate_json(row.payload) for row in rows)

    def decisions(self, mission_id: str, *, limit: int = 100) -> tuple[MissionDecision, ...]:
        with Session(self._engine) as session:
            self._require_mission(session, mission_id)
            rows = session.scalars(
                select(MissionDecisionRow)
                .where(MissionDecisionRow.mission_id == mission_id)
                .order_by(MissionDecisionRow.created_at)
                .limit(limit)
            )
            return tuple(MissionDecision.model_validate_json(row.payload) for row in rows)

    def interventions(
        self, mission_id: str, *, limit: int = 100
    ) -> tuple[MissionIntervention, ...]:
        with Session(self._engine) as session:
            self._require_mission(session, mission_id)
            rows = session.scalars(
                select(MissionInterventionRow)
                .where(MissionInterventionRow.mission_id == mission_id)
                .order_by(MissionInterventionRow.created_at)
                .limit(limit)
            )
            return tuple(MissionIntervention.model_validate_json(row.payload) for row in rows)

    def _record_mission_fact(
        self,
        record: MissionDecision,
        *,
        identity: str,
        mission_id: str,
        conversation_id: str,
        event_type: str,
        event_payload: dict[str, object],
    ) -> MissionDecision:
        payload = self._json(record)
        with Session(self._engine) as session, session.begin():
            existing = session.get(MissionDecisionRow, identity)
            if existing is not None:
                return self._idempotent(existing.payload, payload, record)
            self._require_mission_channel(session, mission_id, conversation_id)
            session.add(
                MissionDecisionRow(
                    id=identity,
                    mission_id=mission_id,
                    conversation_id=conversation_id,
                    payload=payload,
                    created_at=record.created_at.isoformat(),
                )
            )
            self._event(
                session,
                aggregate_id=mission_id,
                entity_type="mission",
                event_type=event_type,
                payload=event_payload,
            )
        return record

    @staticmethod
    def _transition_state(
        mission: MissionRecord, intervention: MissionIntervention
    ) -> MissionState | None:
        if intervention.target_kind is not InterventionTargetKind.MISSION:
            return None
        requested = intervention.resulting_mission_state
        if requested is None:
            return None
        if mission.state in {MissionState.COMPLETED, MissionState.FAILED, MissionState.CANCELLED}:
            raise MishkanError(ErrorCode.MISSION, "terminal mission cannot be intervened upon")
        if intervention.kind is InterventionKind.RESUME and mission.state not in {
            MissionState.PAUSED,
            MissionState.BLOCKED,
        }:
            raise MishkanError(ErrorCode.MISSION, "only paused or blocked missions can resume")
        return requested

    @staticmethod
    def _require_channel(session: Session, conversation_id: str) -> ConversationChannelRow:
        row = session.get(ConversationChannelRow, conversation_id)
        if row is None:
            raise MishkanError(ErrorCode.MISSION, "conversation does not exist")
        return row

    @staticmethod
    def _require_mission(session: Session, mission_id: str) -> MissionRow:
        row = session.get(MissionRow, mission_id)
        if row is None:
            raise MishkanError(ErrorCode.MISSION, "mission does not exist")
        return row

    @classmethod
    def _require_mission_channel(
        cls, session: Session, mission_id: str, conversation_id: str
    ) -> ConversationChannelRow:
        cls._require_mission(session, mission_id)
        row = cls._require_channel(session, conversation_id)
        channel = ConversationChannel.model_validate_json(row.payload)
        if (
            channel.channel_class is not ChannelClass.EXECUTIVE
            and str(channel.mission_id) != mission_id
        ):
            raise MishkanError(
                ErrorCode.MISSION,
                "conversation does not govern the referenced mission",
            )
        return row

    @staticmethod
    def _json(record: BaseModel) -> str:
        return record.model_dump_json()

    @staticmethod
    def _idempotent(existing_payload: str, requested_payload: str, record: RecordT) -> RecordT:
        if existing_payload != requested_payload:
            raise MishkanError(
                ErrorCode.DUPLICATE_RESULT,
                "immutable record identity was reused with different content",
            )
        return record

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
                source="mishkan.conversations",
                payload=json.dumps(payload, sort_keys=True, separators=(",", ":")),
                occurred_at=utc_now().isoformat(),
                command_id=None,
                correlation_id=None,
                causation_id=None,
                sensitivity="internal",
                published_at=None,
            )
        )
