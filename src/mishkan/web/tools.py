"""CapabilityGateway adapters for the typed Web surface."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from mishkan.config.models import CredentialReference, WebConfig
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.tools.adapters import AdapterCall, CapabilityAdapter
from mishkan.tools.gateway_models import AdapterResult
from mishkan.web.models import (
    CrawlRequest,
    ExtractionRequest,
    FetchRequest,
    HttpRequest,
    MapRequest,
    SearchRequest,
    WebOperationContext,
)
from mishkan.web.network import NetworkGuard
from mishkan.web.service import WebService

RequestModel = TypeVar("RequestModel", bound=BaseModel)


class CallCredentialResolver:
    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)

    def resolve(self, references: tuple[CredentialReference, ...]) -> tuple[str | None, ...]:
        if not references:
            return (None,)
        missing = [item.locator for item in references if item.locator not in self._values]
        if missing:
            raise MishkanError(
                ErrorCode.TOOL_UNAVAILABLE,
                "authorized Web credential values are unavailable",
                details={"references": missing},
            )
        return tuple(self._values[item.locator] for item in references)


class _WebToolAdapter:
    adapter_id: str

    def __init__(self, config: WebConfig, service: WebService) -> None:
        self._config = config
        self._service = service

    @staticmethod
    def _request(call: AdapterCall, model: type[RequestModel]) -> RequestModel:
        try:
            return model.model_validate(call.arguments["request"])
        except (KeyError, ValidationError) as exc:
            raise MishkanError(
                ErrorCode.TOOL_SCHEMA,
                "Web tool request does not match its public schema",
            ) from exc

    @staticmethod
    def _context(call: AdapterCall) -> WebOperationContext:
        return WebOperationContext(
            producer_identity=call.acting_identity,
            run_id=call.run_id,
            task_attempt_id=call.task_attempt_id,
            call_id=call.execution_id,
            capability=call.capability,
            sensitivity=str(call.arguments.get("sensitivity", "internal")),
            retention=str(call.arguments.get("retention", "run")),
        )

    @staticmethod
    def _declared_credentials(call: AdapterCall) -> tuple[str, ...]:
        raw = call.arguments.get("credential_refs", [])
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise MishkanError(ErrorCode.TOOL_SCHEMA, "Web credential_refs must be a string list")
        return tuple(raw)

    def _verify_credentials(
        self,
        call: AdapterCall,
        expected: tuple[CredentialReference, ...],
    ) -> CallCredentialResolver:
        locators = tuple(dict.fromkeys(item.locator for item in expected))
        if self._declared_credentials(call) != locators or tuple(call.credentials) != locators:
            raise MishkanError(
                ErrorCode.TOOL_SCHEMA,
                "Web credential declarations differ from configured references",
            )
        return CallCredentialResolver(call.credentials)

    @staticmethod
    def _verify_targets(call: AdapterCall, expected: tuple[str, ...], *, scope: str) -> None:
        actual = (
            call.targets.network_destinations
            if scope == "network"
            else call.targets.external_resources
        )
        if actual != tuple(dict.fromkeys(expected)):
            raise MishkanError(
                ErrorCode.TOOL_SCHEMA,
                "Web resolved targets differ from configured request targets",
                details={"scope": scope, "expected": expected, "received": actual},
            )

    @staticmethod
    def _result(
        call: AdapterCall,
        value: BaseModel,
        references: tuple[str, ...] = (),
    ) -> AdapterResult:
        return AdapterResult(
            output=value.model_dump(mode="json"),
            actual_targets=call.targets,
            external_references=tuple(dict.fromkeys(references)),
            evidence={"adapter": "mishkan.web", "operation": call.capability},
        )


class WebSearchToolAdapter(_WebToolAdapter):
    adapter_id = "native.web.search"

    def invoke(self, call: AdapterCall) -> AdapterResult:
        request = self._request(call, SearchRequest)
        selected = request.source_ids or self._config.default_search_sources
        try:
            sources = tuple(self._config.sources[item] for item in selected)
        except KeyError as exc:
            raise MishkanError(ErrorCode.WEB, "search source is not configured") from exc
        origins = tuple(
            NetworkGuard(self._config.network_profiles[source.network_profile])
            .validate_url(str(source.endpoint))
            .origin
            for source in sources
        )
        self._verify_targets(call, origins, scope="network")
        references = tuple(ref for source in sources for ref in source.credential_refs)
        resolver = self._verify_credentials(call, references)
        return self._result(
            call,
            self._service.search(request, credential_resolver=resolver),
        )


class _WebRequestToolAdapter(_WebToolAdapter):
    request_model: type[HttpRequest]

    def invoke(self, call: AdapterCall) -> AdapterResult:
        request = self._request(call, self.request_model)
        profile = self._config.network_profiles.get(request.network_profile)
        if profile is None:
            raise MishkanError(ErrorCode.WEB, "Web request network profile is not configured")
        guard = NetworkGuard(profile)
        origins = (
            guard.validate_url(str(request.url)).origin,
            *(guard.validate_url(item).origin for item in request.allowed_redirect_origins),
        )
        self._verify_targets(call, origins, scope="network")
        external = (request.body_artifact_reference,) if request.body_artifact_reference else ()
        self._verify_targets(call, external, scope="external_resource")
        resolver = self._verify_credentials(call, request.credential_refs)
        context = self._context(call)
        result = (
            self._service.fetch(request, context, credential_resolver=resolver)
            if isinstance(request, FetchRequest)
            else self._service.request(request, context, credential_resolver=resolver)
        )
        return self._result(call, result, (result.artifact_reference,))


class WebFetchToolAdapter(_WebRequestToolAdapter):
    adapter_id = "native.web.fetch"
    request_model = FetchRequest


class WebHttpRequestToolAdapter(_WebRequestToolAdapter):
    adapter_id = "native.web.request"
    request_model = HttpRequest


class WebExtractToolAdapter(_WebToolAdapter):
    adapter_id = "native.web.extract"

    def invoke(self, call: AdapterCall) -> AdapterResult:
        request = self._request(call, ExtractionRequest)
        self._verify_targets(call, (request.artifact_reference,), scope="external_resource")
        self._verify_credentials(call, ())
        result = self._service.extract(request, self._context(call))
        return self._result(
            call,
            result,
            (request.artifact_reference, result.output_artifact_reference),
        )


class _WebCrawlToolAdapter(_WebToolAdapter):
    request_model: type[CrawlRequest]

    def invoke(self, call: AdapterCall) -> AdapterResult:
        request = self._request(call, self.request_model)
        crawler = self._config.crawlers.get(request.crawler_id)
        if crawler is None:
            raise MishkanError(ErrorCode.WEB, "Web crawler is not configured")
        guard = NetworkGuard(self._config.network_profiles[crawler.network_profile])
        origins = tuple(
            dict.fromkeys(
                (
                    guard.validate_url(str(request.root_url)).origin,
                    *(guard.validate_url(item).origin for item in request.allowed_origins),
                )
            )
        )
        self._verify_targets(call, origins, scope="network")
        self._verify_credentials(call, ())
        result = (
            self._service.map(request, self._context(call))
            if isinstance(request, MapRequest)
            else self._service.crawl(request, self._context(call))
        )
        references = tuple(
            reference
            for page in result.pages
            for reference in (page.artifact_reference, page.extracted_artifact_reference)
            if reference is not None
        )
        return self._result(call, result, references)


class WebMapToolAdapter(_WebCrawlToolAdapter):
    adapter_id = "native.web.map"
    request_model = MapRequest


class WebCrawlToolAdapter(_WebCrawlToolAdapter):
    adapter_id = "native.web.crawl"
    request_model = CrawlRequest


def build_web_tool_adapters(
    config: WebConfig,
    service: WebService,
) -> dict[str, CapabilityAdapter]:
    adapters = (
        WebSearchToolAdapter(config, service),
        WebFetchToolAdapter(config, service),
        WebHttpRequestToolAdapter(config, service),
        WebExtractToolAdapter(config, service),
        WebMapToolAdapter(config, service),
        WebCrawlToolAdapter(config, service),
    )
    return {adapter.adapter_id: adapter for adapter in adapters}
