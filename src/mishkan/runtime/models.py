"""Normative durable run/task lifecycle values."""

from enum import StrEnum


class RunState(StrEnum):
    PLANNING = "planning"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskState(StrEnum):
    PENDING = "pending"
    ELIGIBLE = "eligible"
    EXECUTING = "executing"
    VALIDATING = "validating"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"
