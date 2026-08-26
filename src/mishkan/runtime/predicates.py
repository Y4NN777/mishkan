"""Restricted declarative predicate DSL for bounded workflow loops."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mishkan.domain.errors import ErrorCode, MishkanError


@dataclass(frozen=True, slots=True)
class PredicateLimits:
    max_depth: int = 12
    max_nodes: int = 128


class PredicateEvaluator:
    def __init__(self, limits: PredicateLimits | None = None) -> None:
        self._limits = limits or PredicateLimits()

    def evaluate(self, predicate: dict[str, Any], results: dict[str, Any]) -> bool:
        nodes = [0]
        return self._evaluate(predicate, results, depth=0, nodes=nodes)

    def validate_loop(self, *, maximum_iterations: int) -> None:
        if maximum_iterations < 1 or maximum_iterations > 10_000:
            raise MishkanError(
                ErrorCode.PLAN,
                "workflow loop requires a bounded positive maximum iteration count",
            )

    def _evaluate(
        self,
        predicate: dict[str, Any],
        results: dict[str, Any],
        *,
        depth: int,
        nodes: list[int],
    ) -> bool:
        nodes[0] += 1
        if depth > self._limits.max_depth or nodes[0] > self._limits.max_nodes:
            raise MishkanError(ErrorCode.PLAN, "workflow predicate exceeds configured bounds")
        if len(predicate) != 1:
            raise MishkanError(ErrorCode.PLAN, "workflow predicate requires exactly one operator")
        operator, operand = next(iter(predicate.items()))
        if operator in {"all", "any"}:
            if not isinstance(operand, list) or not operand:
                raise MishkanError(ErrorCode.PLAN, f"{operator} predicate requires operands")
            values = [
                self._evaluate(item, results, depth=depth + 1, nodes=nodes)
                for item in operand
                if isinstance(item, dict)
            ]
            if len(values) != len(operand):
                raise MishkanError(ErrorCode.PLAN, "logical predicate operand is invalid")
            return all(values) if operator == "all" else any(values)
        if operator == "not":
            if not isinstance(operand, dict):
                raise MishkanError(ErrorCode.PLAN, "not predicate requires one predicate")
            return not self._evaluate(operand, results, depth=depth + 1, nodes=nodes)
        if operator == "exists":
            if not isinstance(operand, str):
                raise MishkanError(ErrorCode.PLAN, "exists predicate requires a result path")
            found, _value = self._resolve(results, operand)
            return found
        if operator not in {"eq", "ne", "lt", "le", "gt", "ge", "in", "contains"}:
            raise MishkanError(ErrorCode.PLAN, "workflow predicate operator is unsupported")
        if not isinstance(operand, list) or len(operand) != 2 or not isinstance(operand[0], str):
            raise MishkanError(ErrorCode.PLAN, "comparison predicate requires path and literal")
        found, actual = self._resolve(results, operand[0])
        if not found:
            return False
        expected = operand[1]
        try:
            if operator == "eq":
                return bool(actual == expected)
            if operator == "ne":
                return bool(actual != expected)
            if operator == "lt":
                return bool(actual < expected)
            if operator == "le":
                return bool(actual <= expected)
            if operator == "gt":
                return bool(actual > expected)
            if operator == "ge":
                return bool(actual >= expected)
            if operator == "in":
                return bool(actual in expected)
            return bool(expected in actual)
        except TypeError as exc:
            raise MishkanError(
                ErrorCode.PLAN, "workflow predicate operands are incompatible"
            ) from exc

    @staticmethod
    def _resolve(results: dict[str, Any], path: str) -> tuple[bool, Any]:
        if not path or path.startswith(".") or path.endswith("."):
            raise MishkanError(ErrorCode.PLAN, "workflow result path is invalid")
        current: Any = results
        for part in path.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
                current = current[int(part)]
            else:
                return False, None
        return True, current
