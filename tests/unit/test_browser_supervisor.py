from __future__ import annotations

import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from pydantic import AnyHttpUrl, ValidationError
from support.capabilities import context_for, inspector, policy_for

from mishkan.artifacts.service import DurableArtifactService
from mishkan.browser import (
    BrowserActionKind,
    BrowserActionRequest,
    BrowserActionState,
    BrowserDiagnosticRequest,
    BrowserObservationRequest,
    BrowserSessionRequest,
    BrowserSessionState,
    BrowserSupervisor,
    BrowserTarget,
    PlaywrightChromiumDriver,
    build_browser_tool_adapters,
)
from mishkan.browser.driver import (
    BrowserOperationCancelled,
    BrowserUncertainEffect,
    DriverActionOutcome,
    DriverArtifact,
    DriverDiagnostics,
    DriverObservation,
    DriverSession,
)
from mishkan.browser.playwright import _LiveSession
from mishkan.browser.tools import BrowserActToolAdapter
from mishkan.config.models import (
    BrowserConfig,
    BrowserProfileConfig,
    BrowserProfileKind,
    MishkanConfig,
)
from mishkan.config.presets import preset_text
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.time import utc_now
from mishkan.persistence import SchemaManager
from mishkan.policy import Decision, PolicyAuthority
from mishkan.tools.gateway import CapabilityGateway, MappingCredentialResolver, MemoryEvidenceSink
from mishkan.tools.gateway_models import CallStatus, DeclaredTargets
from mishkan.tools.inspection import ContentInspector, InspectionProfileLoader


class FakeDriver:
    adapter_id = "playwright.chromium"

    def __init__(self) -> None:
        self.uncertain_open = False
        self.fail_open = False
        self.uncertain = False
        self.actions = 0
        self.closed = 0
        self.fail_close = False
        self.last_value: object = None

    def open(
        self,
        profile: BrowserProfileConfig,
        *,
        workspace: str,
        initial_url: str | None,
    ) -> DriverSession:
        del profile, workspace, initial_url
        if self.uncertain_open:
            raise BrowserUncertainEffect("fixture lost certainty while opening")
        if self.fail_open:
            raise RuntimeError("fixture open failure")
        return DriverSession("handle-1", ("page-1",), "fixture-1")

    def observe(
        self,
        handle: str,
        page_id: str,
        *,
        screenshot: bool,
    ) -> DriverObservation:
        del handle, page_id
        target = BrowserTarget(
            reference="button:save",
            role="button",
            name="Save",
            element_revision="sha256:" + hashlib.sha256(b"save").hexdigest(),
            candidate_effects=("form.submit",),
        )
        field = BrowserTarget(
            reference="textbox:password",
            role="textbox",
            name="Password",
            element_revision="sha256:" + hashlib.sha256(b"password").hexdigest(),
            candidate_effects=("form.field.update",),
        )
        download = BrowserTarget(
            reference="link:download",
            role="link",
            name="Download",
            element_revision="sha256:" + hashlib.sha256(b"download").hexdigest(),
            candidate_effects=("file.download",),
        )
        return DriverObservation(
            url="https://example.com/form",
            title="Form",
            tree=b"- button Save [ref=button:save]",
            targets=(target, field, download),
            screenshot=b"png" if screenshot else None,
        )

    def act(
        self,
        handle: str,
        request: BrowserActionRequest,
        target: BrowserTarget | None,
        *,
        cancellation_requested,
    ) -> DriverActionOutcome:
        del handle, target
        if cancellation_requested():
            raise BrowserOperationCancelled("cancelled before fixture dispatch")
        self.last_value = request.value
        self.actions += 1
        if self.uncertain:
            raise BrowserUncertainEffect("connection lost after submit")
        if request.resolved_effect == "file.download":
            return DriverActionOutcome(
                ("page-1",),
                (
                    DriverArtifact(
                        channel="browser.download",
                        media_type="application/octet-stream",
                        content=b"download proof",
                    ),
                ),
            )
        return DriverActionOutcome(("page-1",))

    def diagnostics(
        self,
        handle: str,
        page_id: str,
        channels: tuple[str, ...],
        cursor: int,
        limit: int,
    ) -> DriverDiagnostics:
        del handle, page_id, channels, limit
        return DriverDiagnostics(({"kind": "console", "text": "ready"},), cursor + 1, False)

    def close(self, handle: str) -> None:
        del handle
        self.closed += 1
        if self.fail_close:
            raise RuntimeError("fixture close uncertainty")


def _config() -> BrowserConfig:
    config = MishkanConfig.model_validate(yaml.safe_load(preset_text("local")))
    assert config.browser is not None
    return config.browser


def _supervisor(
    tmp_path: Path,
    driver: FakeDriver,
    config: BrowserConfig | None = None,
) -> BrowserSupervisor:
    database = tmp_path / ".mishkan" / "mishkan.db"
    if not database.exists():
        SchemaManager(database).initialize()
    artifacts = DurableArtifactService(
        database,
        tmp_path / ".mishkan" / "artifacts",
        max_artifact_bytes=2_000_000,
        max_chunk_bytes=64_000,
    )
    inspector = ContentInspector(
        InspectionProfileLoader().load(
            "package://mishkan.resources.inspection/default-security.yaml",
            tmp_path,
        )
    )
    return BrowserSupervisor(
        database,
        tmp_path,
        config or _config(),
        artifacts,
        {driver.adapter_id: driver},
        inspector,
    )


def _persistent_config() -> BrowserConfig:
    config = _config()
    source = config.profiles[config.default_profile]
    profile = source.model_copy(
        update={
            "kind": BrowserProfileKind.PROJECT_PERSISTENT,
            "user_data_dir": config.staging_root / "profiles" / "project",
        }
    )
    return config.model_copy(
        update={
            "default_profile": "persistent-project",
            "profiles": {"persistent-project": profile},
        }
    )


class PersistentStateDriver(FakeDriver):
    def __init__(self) -> None:
        super().__init__()
        self.restored_state = False

    def open(
        self,
        profile: BrowserProfileConfig,
        *,
        workspace: str,
        initial_url: str | None,
    ) -> DriverSession:
        del initial_url
        assert profile.user_data_dir is not None
        root = Path(workspace) / profile.user_data_dir
        marker = root / "Default" / "Cookies.fixture"
        self.restored_state = (
            marker.read_bytes() == b"authenticated-state" if marker.exists() else False
        )
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(b"authenticated-state")
        return DriverSession(f"handle-{self.actions}", ("page-1",), "fixture-1")


def _open(supervisor: BrowserSupervisor):
    return supervisor.open(
        BrowserSessionRequest(
            profile_id="isolated-chromium",
            owner_identity="role:Engineer",
            run_id="run-1",
            task_attempt_id="task:1",
            workspace=".",
            initial_url=AnyHttpUrl("https://example.com/form"),
        )
    )


def test_observation_bound_action_is_durable_idempotent_and_invalidates_revision(
    tmp_path: Path,
) -> None:
    driver = FakeDriver()
    supervisor = _supervisor(tmp_path, driver)
    browser = _open(supervisor)
    assert supervisor.list(owner_identity="role:Engineer") == (browser,)
    with pytest.raises(MishkanError) as wrong_owner:
        supervisor.get(browser.id, owner_identity="role:Other")
    assert wrong_owner.value.envelope.code is ErrorCode.AUTHORITY_NOT_GRANTED
    with pytest.raises(MishkanError):
        supervisor.diagnostics(
            BrowserDiagnosticRequest(
                session_id=browser.id,
                page_id="not-owned",
                channels=("console",),
            ),
            owner_identity="role:Engineer",
        )
    observation = supervisor.observe(
        BrowserObservationRequest(
            session_id=browser.id,
            page_id="page-1",
            expected_session_revision=browser.revision,
            include_screenshot=True,
        ),
        owner_identity="role:Engineer",
    )
    request = BrowserActionRequest(
        session_id=browser.id,
        page_id="page-1",
        observation_id=observation.id,
        target_reference="button:save",
        kind=BrowserActionKind.CLICK,
        resolved_effect="form.submit",
        expected_session_revision=browser.revision,
    )

    first = supervisor.act(request, owner_identity="role:Engineer")
    replay = supervisor.act(request, owner_identity="role:Engineer")

    assert first.state is BrowserActionState.COMPLETED
    assert replay == first
    assert driver.actions == 1
    assert first.session_revision == browser.revision + 1
    with pytest.raises(MishkanError) as conflicting_key:
        supervisor.act(
            request.model_copy(update={"value": "different"}),
            owner_identity="role:Engineer",
        )
    assert conflicting_key.value.envelope.code is ErrorCode.DUPLICATE_RESULT
    with pytest.raises(MishkanError) as stale:
        supervisor.act(
            request.model_copy(update={"idempotency_key": UUID(int=1)}),
            owner_identity="role:Engineer",
        )
    assert stale.value.envelope.code is ErrorCode.REVISION_MISMATCH


def test_open_failure_and_uncertainty_remain_durable_queryable_session_states(
    tmp_path: Path,
) -> None:
    uncertain_driver = FakeDriver()
    uncertain_driver.uncertain_open = True
    uncertain_supervisor = _supervisor(tmp_path / "uncertain", uncertain_driver)

    uncertain = _open(uncertain_supervisor)

    assert uncertain.state is BrowserSessionState.UNCERTAIN
    assert uncertain.uncertain_effect == "browser.session.open"
    assert (
        uncertain_supervisor.get(
            uncertain.id,
            owner_identity="role:Engineer",
        )
        == uncertain
    )

    failed_driver = FakeDriver()
    failed_driver.fail_open = True
    failed_supervisor = _supervisor(tmp_path / "failed", failed_driver)

    failed = _open(failed_supervisor)

    assert failed.state is BrowserSessionState.FAILED
    assert failed_supervisor.get(failed.id, owner_identity="role:Engineer") == failed


def test_persistent_profile_is_settled_as_artifact_and_restored_on_next_open(
    tmp_path: Path,
) -> None:
    driver = PersistentStateDriver()
    config = _persistent_config()
    supervisor = _supervisor(tmp_path, driver, config)

    opened = supervisor.open(
        BrowserSessionRequest(
            profile_id=config.default_profile,
            owner_identity="role:Engineer",
            run_id="run-1",
            task_attempt_id="task:1",
            workspace=".",
        )
    )
    assert opened.state is BrowserSessionState.ACTIVE
    assert driver.restored_state is False
    user_data_dir = config.profiles[config.default_profile].user_data_dir
    assert user_data_dir is not None
    profile_path = tmp_path / user_data_dir
    assert profile_path.is_dir()

    closed = supervisor.close(opened.id, owner_identity="role:Engineer")

    assert closed.state is BrowserSessionState.CLOSED
    assert closed.profile_state_artifact_reference is not None
    assert not profile_path.exists()

    reopened = supervisor.open(
        BrowserSessionRequest(
            profile_id=config.default_profile,
            owner_identity="role:Engineer",
            run_id="run-2",
            task_attempt_id="task:2",
            workspace=".",
        )
    )
    assert reopened.state is BrowserSessionState.ACTIVE
    assert driver.restored_state is True


def test_persistent_profile_refuses_unsettled_raw_state_after_restart(tmp_path: Path) -> None:
    config = _persistent_config()
    first = _supervisor(tmp_path, PersistentStateDriver(), config)
    opened = first.open(
        BrowserSessionRequest(
            profile_id=config.default_profile,
            owner_identity="role:Engineer",
            run_id="run-1",
            task_attempt_id="task:1",
            workspace=".",
        )
    )
    assert opened.state is BrowserSessionState.ACTIVE

    restarted = _supervisor(tmp_path, PersistentStateDriver(), config)
    with pytest.raises(MishkanError) as unsettled:
        restarted.open(
            BrowserSessionRequest(
                profile_id=config.default_profile,
                owner_identity="role:Engineer",
                run_id="run-2",
                task_attempt_id="task:2",
                workspace=".",
            )
        )

    assert unsettled.value.envelope.code is ErrorCode.RUN_INTERRUPTED
    assert unsettled.value.envelope.details["reconciliation_required"] is True


def test_persistent_profile_refuses_a_symlinked_managed_directory_chain(tmp_path: Path) -> None:
    config = _persistent_config()
    managed = tmp_path / config.staging_root
    managed.mkdir(parents=True)
    outside = tmp_path / "outside-profile"
    outside.mkdir()
    (managed / "profiles").symlink_to(outside, target_is_directory=True)
    supervisor = _supervisor(tmp_path, PersistentStateDriver(), config)

    with pytest.raises(MishkanError) as escaped:
        supervisor.open(
            BrowserSessionRequest(
                profile_id=config.default_profile,
                owner_identity="role:Engineer",
                run_id="run-1",
                task_attempt_id="task:1",
                workspace=".",
            )
        )

    assert escaped.value.envelope.code is ErrorCode.AUTHORITY_NOT_GRANTED
    assert not any(outside.iterdir())


def test_concurrent_session_actions_recheck_revision_before_the_second_effect(
    tmp_path: Path,
) -> None:
    class BlockingDriver(FakeDriver):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def act(self, *args, **kwargs):
            self.entered.set()
            assert self.release.wait(timeout=5)
            return super().act(*args, **kwargs)

    driver = BlockingDriver()
    supervisor = _supervisor(tmp_path, driver)
    browser = _open(supervisor)
    observation = supervisor.observe(
        BrowserObservationRequest(
            session_id=browser.id,
            page_id="page-1",
            expected_session_revision=browser.revision,
        ),
        owner_identity="role:Engineer",
    )

    def request(identity: int) -> BrowserActionRequest:
        return BrowserActionRequest(
            session_id=browser.id,
            page_id="page-1",
            observation_id=observation.id,
            target_reference="button:save",
            kind=BrowserActionKind.CLICK,
            resolved_effect="form.submit",
            expected_session_revision=browser.revision,
            idempotency_key=UUID(int=identity),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            supervisor.act,
            request(10),
            owner_identity="role:Engineer",
        )
        assert driver.entered.wait(timeout=5)
        second = executor.submit(
            supervisor.act,
            request(11),
            owner_identity="role:Engineer",
        )
        driver.release.set()

        assert first.result(timeout=5).state is BrowserActionState.COMPLETED
        with pytest.raises(MishkanError) as stale:
            second.result(timeout=5)

    assert stale.value.envelope.code is ErrorCode.REVISION_MISMATCH
    assert driver.actions == 1


def test_observed_link_destination_requires_exact_action_network_authority(
    tmp_path: Path,
) -> None:
    class LinkDriver(FakeDriver):
        def observe(
            self,
            handle: str,
            page_id: str,
            *,
            screenshot: bool,
        ) -> DriverObservation:
            del handle, page_id, screenshot
            target = BrowserTarget(
                reference="link:next",
                role="link",
                name="Next",
                element_revision="sha256:" + hashlib.sha256(b"next").hexdigest(),
                candidate_effects=("ui.interaction",),
                destination_origin="https://example.com",
            )
            return DriverObservation(
                url="https://example.com/form",
                title="Form",
                tree=b"- link Next [ref=link:next]",
                targets=(target,),
            )

    driver = LinkDriver()
    supervisor = _supervisor(tmp_path, driver)
    browser = _open(supervisor)
    observation = supervisor.observe(
        BrowserObservationRequest(
            session_id=browser.id,
            page_id="page-1",
            expected_session_revision=browser.revision,
        ),
        owner_identity="role:Engineer",
    )
    request = BrowserActionRequest(
        session_id=browser.id,
        page_id="page-1",
        observation_id=observation.id,
        target_reference="link:next",
        kind=BrowserActionKind.CLICK,
        resolved_effect="ui.interaction",
        expected_session_revision=browser.revision,
    )

    with pytest.raises(MishkanError) as unauthorized:
        supervisor.act(request, owner_identity="role:Engineer")
    assert unauthorized.value.envelope.code is ErrorCode.AUTHORITY_NOT_GRANTED
    assert driver.actions == 0

    accepted = supervisor.act(
        request.model_copy(update={"authorized_origins": ("https://example.com",)}),
        owner_identity="role:Engineer",
    )
    assert accepted.state is BrowserActionState.COMPLETED
    assert driver.actions == 1


def test_download_result_is_committed_as_an_immutable_artifact(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path, FakeDriver())
    browser = _open(supervisor)
    observation = supervisor.observe(
        BrowserObservationRequest(
            session_id=browser.id,
            page_id="page-1",
            expected_session_revision=browser.revision,
        ),
        owner_identity="role:Engineer",
    )

    result = supervisor.act(
        BrowserActionRequest(
            session_id=browser.id,
            page_id="page-1",
            observation_id=observation.id,
            target_reference="link:download",
            kind=BrowserActionKind.CLICK,
            resolved_effect="file.download",
            expected_session_revision=browser.revision,
        ),
        owner_identity="role:Engineer",
    )

    assert result.state is BrowserActionState.COMPLETED
    assert len(result.artifact_references) == 1
    reference = result.artifact_references[0]
    database = tmp_path / ".mishkan" / "mishkan.db"
    artifacts = DurableArtifactService(
        database,
        tmp_path / ".mishkan" / "artifacts",
        max_artifact_bytes=2_000_000,
        max_chunk_bytes=64_000,
    )
    manifest = artifacts.manifest(reference)
    assert manifest.provenance.channel == "browser.download"
    assert artifacts.read_bytes(reference) == b"download proof"


def test_action_cancellation_is_durable_only_when_observed_before_dispatch(
    tmp_path: Path,
) -> None:
    driver = FakeDriver()
    supervisor = _supervisor(tmp_path, driver)
    browser = _open(supervisor)
    observation = supervisor.observe(
        BrowserObservationRequest(
            session_id=browser.id,
            page_id="page-1",
            expected_session_revision=browser.revision,
        ),
        owner_identity="role:Engineer",
    )
    request = BrowserActionRequest(
        session_id=browser.id,
        page_id="page-1",
        observation_id=observation.id,
        target_reference="button:save",
        kind=BrowserActionKind.CLICK,
        resolved_effect="form.submit",
        expected_session_revision=browser.revision,
    )

    result = supervisor.act(
        request,
        owner_identity="role:Engineer",
        cancellation_requested=lambda: True,
    )

    assert result.state is BrowserActionState.CANCELLED
    assert result.observation_invalidated is False
    assert result.error_code == ErrorCode.BROWSER
    assert driver.actions == 0
    assert supervisor.act(request, owner_identity="role:Engineer") == result
    assert supervisor.get(browser.id, owner_identity="role:Engineer") == browser


def test_uncertain_action_blocks_session_reuse_and_restart_marks_live_sessions_lost(
    tmp_path: Path,
) -> None:
    driver = FakeDriver()
    supervisor = _supervisor(tmp_path, driver)
    browser = _open(supervisor)
    observation = supervisor.observe(
        BrowserObservationRequest(
            session_id=browser.id,
            page_id="page-1",
            expected_session_revision=browser.revision,
        ),
        owner_identity="role:Engineer",
    )
    driver.uncertain = True
    result = supervisor.act(
        BrowserActionRequest(
            session_id=browser.id,
            page_id="page-1",
            observation_id=observation.id,
            target_reference="button:save",
            kind=BrowserActionKind.CLICK,
            resolved_effect="form.submit",
            expected_session_revision=browser.revision,
        ),
        owner_identity="role:Engineer",
    )

    assert result.state is BrowserActionState.UNCERTAIN
    stored = supervisor.get(browser.id, owner_identity="role:Engineer")
    assert stored.state is BrowserSessionState.UNCERTAIN

    second_driver = FakeDriver()
    second = _supervisor(tmp_path / "second", second_driver)
    live = _open(second)
    assert second.reconcile_all() == 1
    assert second.get(live.id, owner_identity="role:Engineer").state is BrowserSessionState.LOST


def test_credential_is_resolved_only_for_dispatch_and_never_reaches_action_journal(
    tmp_path: Path,
) -> None:
    driver = FakeDriver()
    secret = "credential-value-123"
    supervisor = _supervisor(tmp_path, driver)
    browser = _open(supervisor)
    observation = supervisor.observe(
        BrowserObservationRequest(
            session_id=browser.id,
            page_id="page-1",
            expected_session_revision=browser.revision,
        ),
        owner_identity="role:Engineer",
    )
    result = supervisor.act(
        BrowserActionRequest(
            session_id=browser.id,
            page_id="page-1",
            observation_id=observation.id,
            target_reference="textbox:password",
            kind=BrowserActionKind.FILL,
            credential_reference="project.login",
            credential_origin="https://example.com",
            resolved_effect="form.field.update",
            expected_session_revision=browser.revision,
        ),
        owner_identity="role:Engineer",
        credential_values={"project.login": secret},
    )

    assert result.state is BrowserActionState.COMPLETED
    assert driver.last_value == secret
    database = tmp_path / ".mishkan" / "mishkan.db"
    with sqlite3.connect(database) as connection:
        payload = connection.execute("SELECT payload FROM browser_actions").fetchone()[0]
    assert secret not in payload
    assert "project.login" in payload


def test_coordinate_fallback_is_bound_to_the_source_observation_screenshot(
    tmp_path: Path,
) -> None:
    driver = FakeDriver()
    supervisor = _supervisor(tmp_path, driver)
    browser = _open(supervisor)
    observation = supervisor.observe(
        BrowserObservationRequest(
            session_id=browser.id,
            page_id="page-1",
            expected_session_revision=browser.revision,
            include_screenshot=True,
        ),
        owner_identity="role:Engineer",
    )
    assert observation.screenshot_artifact_reference is not None

    completed = supervisor.act(
        BrowserActionRequest(
            session_id=browser.id,
            page_id="page-1",
            observation_id=observation.id,
            kind=BrowserActionKind.COORDINATE_CLICK,
            coordinates=(10, 20),
            visual_evidence_artifact_reference=observation.screenshot_artifact_reference,
            resolved_effect="ui.interaction",
            expected_session_revision=browser.revision,
        ),
        owner_identity="role:Engineer",
    )

    assert completed.state is BrowserActionState.COMPLETED
    assert driver.actions == 1
    artifacts = DurableArtifactService(
        tmp_path / ".mishkan" / "mishkan.db",
        tmp_path / ".mishkan" / "artifacts",
        max_artifact_bytes=2_000_000,
        max_chunk_bytes=64_000,
    )
    collection = artifacts.plan_gc(watermark=utc_now() + timedelta(seconds=1))
    assert observation.tree_artifact_reference not in collection.candidates
    assert observation.screenshot_artifact_reference not in collection.candidates


def test_browser_credential_is_refused_on_a_different_observed_origin(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path, FakeDriver())
    browser = _open(supervisor)
    observation = supervisor.observe(
        BrowserObservationRequest(
            session_id=browser.id,
            page_id="page-1",
            expected_session_revision=browser.revision,
        ),
        owner_identity="role:Engineer",
    )

    with pytest.raises(MishkanError) as refused:
        supervisor.act(
            BrowserActionRequest(
                session_id=browser.id,
                page_id="page-1",
                observation_id=observation.id,
                target_reference="textbox:password",
                kind=BrowserActionKind.FILL,
                credential_reference="project.login",
                credential_origin="https://other.example",
                resolved_effect="form.field.update",
                expected_session_revision=browser.revision,
            ),
            owner_identity="role:Engineer",
            credential_values={"project.login": "credential-value-123"},
        )

    assert refused.value.envelope.code is ErrorCode.AUTHORITY_NOT_GRANTED


def test_browser_open_uses_the_same_governed_gateway_as_crewai(tmp_path: Path) -> None:
    driver = FakeDriver()
    supervisor = _supervisor(tmp_path, driver)
    origin = "https://example.com"
    policy = policy_for(
        "browser.open",
        Decision.ALLOW,
        effect_class="network",
        paths=("*",),
        arguments=("browser.session.open",),
        network_destinations=(origin,),
        allow_network=True,
    )
    context = context_for(tmp_path, "browser.open", policy, ("*",), network=True)
    adapters = build_browser_tool_adapters(supervisor)

    def gateway(credentials: dict[str, str] | None = None) -> CapabilityGateway:
        return CapabilityGateway(
            tmp_path,
            PolicyAuthority(),
            MappingCredentialResolver(credentials or {}),
            inspector(tmp_path),
            adapters,
            MemoryEvidenceSink(),
        )

    request = BrowserSessionRequest(
        profile_id="isolated-chromium",
        owner_identity="role:Engineer",
        run_id="run-1",
        task_attempt_id="task:1",
        workspace=".",
        initial_url=AnyHttpUrl("https://example.com/form"),
    )

    result = gateway().invoke(
        context,
        {
            "request": request.model_dump(mode="json"),
            "paths": ["."],
            "network_destinations": [origin],
            "credential_refs": [],
            "declared_effects": ["browser.session.open"],
        },
        DeclaredTargets(paths=(".",), network_destinations=(origin,)),
    )

    assert result.status is CallStatus.COMPLETED
    assert result.output is not None
    assert result.output["state"] == BrowserSessionState.ACTIVE.value
    session_id = UUID(result.output["id"])
    resource = f"browser:{session_id}"

    observe_policy = policy_for(
        "browser.observe",
        Decision.ALLOW,
        effect_class="read",
        arguments=("browser.observe",),
        external_resources=(resource,),
    )
    observe_context = context_for(
        tmp_path,
        "browser.observe",
        observe_policy,
        (resource,),
    )
    observation = gateway().invoke(
        observe_context,
        {
            "request": BrowserObservationRequest(
                session_id=session_id,
                page_id="page-1",
                expected_session_revision=1,
            ).model_dump(mode="json"),
            "session_resource": resource,
            "credential_refs": [],
            "declared_effects": ["browser.observe"],
        },
        DeclaredTargets(external_resources=(resource,)),
    )
    assert observation.status is CallStatus.COMPLETED
    assert observation.output is not None

    secret = "resolved-browser-secret"
    act_policy = policy_for(
        "browser.act",
        Decision.ALLOW,
        effect_class="network",
        arguments=("form.field.update",),
        credentials=("project.login",),
        external_resources=(resource,),
        allow_network=True,
    )
    act_context = context_for(
        tmp_path,
        "browser.act",
        act_policy,
        (resource,),
        network=True,
    )
    acted = gateway({"project.login": secret}).invoke(
        act_context,
        {
            "request": BrowserActionRequest(
                session_id=session_id,
                page_id="page-1",
                observation_id=UUID(observation.output["id"]),
                target_reference="textbox:password",
                kind=BrowserActionKind.FILL,
                credential_reference="project.login",
                credential_origin="https://example.com",
                resolved_effect="form.field.update",
                expected_session_revision=1,
            ).model_dump(mode="json"),
            "paths": [],
            "network_destinations": [],
            "session_resource": resource,
            "credential_refs": ["project.login"],
            "declared_effects": ["form.field.update"],
        },
        DeclaredTargets(external_resources=(resource,)),
    )
    assert acted.status is CallStatus.COMPLETED
    assert driver.last_value == secret

    diagnostics_policy = policy_for(
        "browser.diagnostics",
        Decision.ALLOW,
        effect_class="read",
        arguments=("browser.diagnostics",),
        external_resources=(resource,),
    )
    diagnostics_context = context_for(
        tmp_path,
        "browser.diagnostics",
        diagnostics_policy,
        (resource,),
    )
    diagnostics = gateway().invoke(
        diagnostics_context,
        {
            "request": BrowserDiagnosticRequest(
                session_id=session_id,
                page_id="page-1",
                channels=("console",),
            ).model_dump(mode="json"),
            "session_resource": resource,
            "credential_refs": [],
            "declared_effects": ["browser.diagnostics"],
        },
        DeclaredTargets(external_resources=(resource,)),
    )
    assert diagnostics.status is CallStatus.COMPLETED

    close_policy = policy_for(
        "browser.close",
        Decision.ALLOW,
        effect_class="network",
        arguments=("browser.session.close",),
        external_resources=(resource,),
        allow_network=True,
    )
    close_context = context_for(
        tmp_path,
        "browser.close",
        close_policy,
        (resource,),
        network=True,
    )
    closed = gateway().invoke(
        close_context,
        {
            "session_id": str(session_id),
            "session_resource": resource,
            "credential_refs": [],
            "declared_effects": ["browser.session.close"],
        },
        DeclaredTargets(external_resources=(resource,)),
    )
    assert closed.status is CallStatus.COMPLETED
    assert closed.output is not None
    assert closed.output["state"] == BrowserSessionState.CLOSED.value

    reopened = gateway().invoke(
        context,
        {
            "request": request.model_dump(mode="json"),
            "paths": ["."],
            "network_destinations": [origin],
            "credential_refs": [],
            "declared_effects": ["browser.session.open"],
        },
        DeclaredTargets(paths=(".",), network_destinations=(origin,)),
    )
    assert reopened.output is not None
    reopened_id = UUID(reopened.output["id"])
    reopened_resource = f"browser:{reopened_id}"
    reopened_observation = gateway().invoke(
        context_for(
            tmp_path,
            "browser.observe",
            policy_for(
                "browser.observe",
                Decision.ALLOW,
                effect_class="read",
                arguments=("browser.observe",),
                external_resources=(reopened_resource,),
            ),
            (reopened_resource,),
        ),
        {
            "request": BrowserObservationRequest(
                session_id=reopened_id,
                page_id="page-1",
                expected_session_revision=1,
            ).model_dump(mode="json"),
            "session_resource": reopened_resource,
            "credential_refs": [],
            "declared_effects": ["browser.observe"],
        },
        DeclaredTargets(external_resources=(reopened_resource,)),
    )
    assert reopened_observation.output is not None
    driver.uncertain = True
    uncertain = gateway().invoke(
        context_for(
            tmp_path,
            "browser.act",
            policy_for(
                "browser.act",
                Decision.ALLOW,
                effect_class="network",
                arguments=("form.submit",),
                external_resources=(reopened_resource,),
                allow_network=True,
            ),
            (reopened_resource,),
            network=True,
        ),
        {
            "request": BrowserActionRequest(
                session_id=reopened_id,
                page_id="page-1",
                observation_id=UUID(reopened_observation.output["id"]),
                target_reference="button:save",
                kind=BrowserActionKind.CLICK,
                resolved_effect="form.submit",
                expected_session_revision=1,
            ).model_dump(mode="json"),
            "paths": [],
            "network_destinations": [],
            "session_resource": reopened_resource,
            "credential_refs": [],
            "declared_effects": ["form.submit"],
        },
        DeclaredTargets(external_resources=(reopened_resource,)),
    )
    assert uncertain.status is CallStatus.UNCERTAIN
    assert uncertain.error_code == ErrorCode.BROWSER
    assert uncertain.output is not None
    assert uncertain.output["state"] == BrowserActionState.UNCERTAIN.value


def test_close_is_journaled_and_preserves_uncertainty_after_adapter_loss(tmp_path: Path) -> None:
    driver = FakeDriver()
    supervisor = _supervisor(tmp_path, driver)
    browser = _open(supervisor)
    driver.fail_close = True

    closed = supervisor.close(browser.id, owner_identity="role:Engineer")

    assert closed.state is BrowserSessionState.UNCERTAIN
    assert closed.uncertain_effect == "browser.session.close"
    assert driver.closed == 1
    with pytest.raises(MishkanError):
        supervisor.close(browser.id, owner_identity="role:Engineer")


def test_playwright_value_effect_and_origin_guards_are_explicit() -> None:
    candidate = PlaywrightChromiumDriver._candidate_effects
    assert candidate("link", {"href": "/next"}) == ("navigation",)
    assert candidate("link", {"href": "/proof", "download": True}) == ("file.download",)
    assert candidate("textbox", {"tag": "input", "type": "file"}) == ("file.upload",)
    assert candidate("button", {"tag": "button", "type": "submit"}) == ("form.submit",)
    assert candidate("textbox", {"tag": "input", "type": "text"}) == ("form.field.update",)
    assert candidate("button", {"tag": "button", "type": "button"}) == ("ui.interaction",)
    assert PlaywrightChromiumDriver._string_value("value", "fill") == "value"
    assert PlaywrightChromiumDriver._string_sequence(["a", "b"], "select") == ["a", "b"]
    assert PlaywrightChromiumDriver._safe_url("https://u:p@example.com/a?q=secret#x") == (
        "https://example.com/a"
    )
    assert PlaywrightChromiumDriver._safe_url("://") == "[INVALID_URL]"
    PlaywrightChromiumDriver._require_origin(("https://*.example.com",), "https://a.example.com/x")
    with pytest.raises(MishkanError):
        PlaywrightChromiumDriver._string_value(1, "fill")
    with pytest.raises(MishkanError):
        PlaywrightChromiumDriver._string_sequence([1], "select")
    with pytest.raises(MishkanError):
        PlaywrightChromiumDriver._require_origin(("https://example.com",), "file:///tmp/x")
    with pytest.raises(MishkanError):
        PlaywrightChromiumDriver._require_origin(
            ("https://example.com",),
            "https://other.example/x",
        )


def test_playwright_refuses_websockets_that_cannot_use_the_verified_transport(
    tmp_path: Path,
) -> None:
    configured = MishkanConfig.model_validate(yaml.safe_load(preset_text("local")))
    assert configured.browser is not None
    assert configured.web is not None

    class Context:
        def __init__(self) -> None:
            self.websockets: dict[str, object] = {}

        def route(self, pattern: str, handler: object) -> None:
            del pattern, handler

        def route_web_socket(self, pattern: str, handler: object) -> None:
            self.websockets[pattern] = handler

    class WebSocket:
        url = "wss://example.com/socket?credential=secret"

        def __init__(self) -> None:
            self.closed: tuple[int | None, str | None] | None = None

        def close(self, *, code: int | None = None, reason: str | None = None) -> None:
            self.closed = (code, reason)

    context = Context()
    driver = object.__new__(PlaywrightChromiumDriver)
    driver._network_profiles = configured.web.network_profiles  # type: ignore[attr-defined]
    driver._max_diagnostic_entries = 100  # type: ignore[attr-defined]
    driver._max_pending_downloads = 10  # type: ignore[attr-defined]
    live = _LiveSession(
        configured.browser.profiles[configured.browser.default_profile],
        tmp_path,
        None,
        context,  # type: ignore[arg-type]
    )

    driver._install_network_mediation(live)
    route = WebSocket()
    handler = context.websockets["wss://**/*"]
    assert callable(handler)
    handler(route)

    assert route.closed == (1008, "WebSocket transport is not mediated by this profile")
    assert live.diagnostics == [
        {
            "cursor": 0,
            "channel": "network",
            "kind": "blocked",
            "url": "wss://example.com/socket",
            "reason": "unmediated_websocket_transport",
        }
    ]


def test_browser_action_credentials_are_structurally_constrained() -> None:
    with pytest.raises(ValidationError):
        BrowserActionRequest(
            session_id=UUID(int=1),
            page_id="page",
            observation_id=UUID(int=2),
            kind=BrowserActionKind.CLICK,
            target_reference="button",
            credential_reference="login",
            resolved_effect="ui.interaction",
            expected_session_revision=1,
        )
    with pytest.raises(ValidationError):
        BrowserActionRequest(
            session_id=UUID(int=1),
            page_id="page",
            observation_id=UUID(int=2),
            kind=BrowserActionKind.FILL,
            target_reference="textbox",
            value="literal",
            credential_reference="login",
            resolved_effect="form.field.update",
            expected_session_revision=1,
        )


def test_browser_action_states_remain_visible_at_the_gateway_boundary() -> None:
    assert BrowserActToolAdapter._call_status(BrowserActionState.COMPLETED) is CallStatus.COMPLETED
    assert BrowserActToolAdapter._call_status(BrowserActionState.REFUSED) is CallStatus.REFUSED
    assert BrowserActToolAdapter._call_status(BrowserActionState.FAILED) is CallStatus.FAILED
    assert BrowserActToolAdapter._call_status(BrowserActionState.CANCELLED) is CallStatus.CANCELLED
    assert BrowserActToolAdapter._call_status(BrowserActionState.UNCERTAIN) is CallStatus.UNCERTAIN
    assert BrowserActToolAdapter._session_status(BrowserSessionState.ACTIVE) is CallStatus.COMPLETED
    assert BrowserActToolAdapter._session_status(BrowserSessionState.CLOSED) is CallStatus.COMPLETED
    assert BrowserActToolAdapter._session_status(BrowserSessionState.FAILED) is CallStatus.FAILED
    assert BrowserActToolAdapter._session_status(BrowserSessionState.LOST) is CallStatus.FAILED
    assert (
        BrowserActToolAdapter._session_status(BrowserSessionState.UNCERTAIN) is CallStatus.UNCERTAIN
    )


def test_browser_literal_secret_and_observed_effect_drift_are_refused(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path, FakeDriver())
    browser = _open(supervisor)
    observation = supervisor.observe(
        BrowserObservationRequest(
            session_id=browser.id,
            page_id="page-1",
            expected_session_revision=browser.revision,
        ),
        owner_identity="role:Engineer",
    )
    with pytest.raises(MishkanError) as effect:
        supervisor.act(
            BrowserActionRequest(
                session_id=browser.id,
                page_id="page-1",
                observation_id=observation.id,
                target_reference="button:save",
                kind=BrowserActionKind.CLICK,
                resolved_effect="ui.interaction",
                expected_session_revision=browser.revision,
            ),
            owner_identity="role:Engineer",
        )
    assert effect.value.envelope.code is ErrorCode.AUTHORITY_NOT_GRANTED
    with pytest.raises(MishkanError) as secret:
        supervisor.act(
            BrowserActionRequest(
                session_id=browser.id,
                page_id="page-1",
                observation_id=observation.id,
                target_reference="textbox:password",
                kind=BrowserActionKind.FILL,
                value="password=secret-value-123",
                resolved_effect="form.field.update",
                expected_session_revision=browser.revision,
            ),
            owner_identity="role:Engineer",
        )
    assert secret.value.envelope.code is ErrorCode.SECRET_CONTENT
