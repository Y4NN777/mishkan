"""CrewAI-native representation of a fully governed exact tool binding."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, cast

from crewai.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr, create_model

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.policy import ApprovalEvidence
from mishkan.tools.gateway import CapabilityGateway, declared_targets_for
from mishkan.tools.gateway_models import CallStatus, DeclaredTargets, InvocationContext
from mishkan.tools.models import ToolContract, argument_fingerprint

TargetBuilder = Callable[[dict[str, Any]], DeclaredTargets]


def arguments_model(contract: ToolContract) -> type[BaseModel]:
    properties = contract.input_schema.get("properties", {})
    required = set(contract.input_schema.get("required", []))
    if not isinstance(properties, dict):
        raise MishkanError(ErrorCode.TOOL_CONTRACT, "tool input schema properties are invalid")
    fields: dict[str, Any] = {}
    for name, raw_schema in properties.items():
        if not isinstance(name, str) or not isinstance(raw_schema, dict):
            raise MishkanError(ErrorCode.TOOL_CONTRACT, "tool input schema field is invalid")
        field_type = _python_type(raw_schema)
        default: Any = ... if name in required else None
        fields[name] = (field_type, Field(default=default))
    return cast(
        type[BaseModel],
        create_model(
            f"{contract.crewai_name.title().replace('_', '')}Input",
            __base__=BaseModel,
            **fields,
        ),
    )


def _python_type(schema: dict[str, Any]) -> Any:
    value = schema.get("type")
    if isinstance(value, list) and len(value) == 2 and "null" in value:
        concrete = next(item for item in value if item != "null")
        return _python_type({**schema, "type": concrete}) | None
    if value == "string":
        return str
    if value == "integer":
        return int
    if value == "number":
        return float
    if value == "boolean":
        return bool
    if value == "array":
        _python_type(schema.get("items", {}))
        return list[Any]
    if value == "object":
        return dict[str, Any]
    raise MishkanError(
        ErrorCode.TOOL_CONTRACT,
        "CrewAI binding does not support a tool input schema type",
        details={"type": value},
    )


class GatewayCrewAITool(BaseTool):
    _gateway: CapabilityGateway = PrivateAttr()
    _context: InvocationContext = PrivateAttr()
    _targets: TargetBuilder = PrivateAttr()
    _approval: ApprovalEvidence | tuple[ApprovalEvidence, ...] | None = PrivateAttr()
    _enforce_exact_calls: bool = PrivateAttr()
    _remaining_call_fingerprints: set[str] = PrivateAttr()
    _completed_call_fingerprints: set[str] = PrivateAttr()

    def __init__(
        self,
        contract: ToolContract,
        gateway: CapabilityGateway,
        context: InvocationContext,
        target_builder: TargetBuilder | None = None,
        approval: ApprovalEvidence | tuple[ApprovalEvidence, ...] | None = None,
    ) -> None:
        super().__init__(
            name=contract.crewai_name,
            description=contract.summary,
            args_schema=arguments_model(contract),
        )
        self._gateway = gateway
        self._context = context
        self._targets = target_builder or (
            lambda arguments: declared_targets_for(contract, arguments)
        )
        self._approval = approval
        self._enforce_exact_calls = bool(context.binding.allowed_call_fingerprints)
        self._remaining_call_fingerprints = set(context.binding.allowed_call_fingerprints)
        self._completed_call_fingerprints = set()

    @property
    def pending_call_fingerprints(self) -> frozenset[str]:
        return frozenset(self._remaining_call_fingerprints)

    @property
    def completed_call_fingerprints(self) -> frozenset[str]:
        return frozenset(self._completed_call_fingerprints)

    def _run(self, **kwargs: Any) -> str:
        fingerprint = argument_fingerprint(kwargs)
        if self._enforce_exact_calls:
            if fingerprint not in self._remaining_call_fingerprints:
                raise MishkanError(
                    ErrorCode.AUTHORITY_NOT_GRANTED,
                    "CrewAI attempted an unplanned or already attempted tool call",
                    details={"argument_fingerprint": fingerprint},
                )
            # Reserve before dispatch: a failed or uncertain non-idempotent call is never
            # repeated automatically through the same accepted task binding.
            self._remaining_call_fingerprints.remove(fingerprint)
        result = self._gateway.invoke(
            self._context,
            kwargs,
            self._targets(kwargs),
            self._approval,
        )
        if result.status is not CallStatus.COMPLETED or result.output is None:
            code = ErrorCode(result.error_code) if result.error_code else ErrorCode.TOOL_EFFECT
            raise MishkanError(code, result.reason)
        self._completed_call_fingerprints.add(fingerprint)
        return json.dumps(result.output, sort_keys=True)
