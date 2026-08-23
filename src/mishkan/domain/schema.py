"""Persisted schema compatibility without implicit migration."""

from collections.abc import Mapping

from mishkan.domain.errors import ErrorCode, MishkanError


class SchemaRegistry:
    """Closed registry of versions understood by the running release."""

    _supported: Mapping[str, frozenset[str]] = {
        "mishkan.config": frozenset({"1.0"}),
        "mishkan.error": frozenset({"1.0"}),
        "mishkan.record": frozenset({"1.0"}),
    }

    @classmethod
    def supported_versions(cls, contract: str) -> frozenset[str]:
        return cls._supported.get(contract, frozenset())

    @classmethod
    def require_supported(cls, contract: str, version: object) -> str:
        normalized = str(version) if version is not None else ""
        supported = cls.supported_versions(contract)
        if normalized not in supported:
            raise MishkanError(
                ErrorCode.VERSION,
                f"unsupported schema version for {contract}",
                details={
                    "contract": contract,
                    "received": normalized or None,
                    "supported": sorted(supported),
                    "automatic_migration": False,
                },
            )
        return normalized
