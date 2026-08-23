"""Identity-bearing record primitives."""

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mishkan.domain.time import require_aware, utc_now


def new_id() -> UUID:
    """Create a globally unique record identifier."""

    return uuid4()


class DomainRecord(BaseModel):
    """Minimum contract for persisted domain records."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=new_id)
    schema_version: str = "1.0"
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def created_at_is_unambiguous(cls, value: datetime) -> datetime:
        return require_aware(value)
