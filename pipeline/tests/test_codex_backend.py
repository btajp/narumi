"""Connection lifecycle with synthetic credentials and an entirely fake App Server."""

from __future__ import annotations

import copy
import queue
import stat
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from narumi.errors import (
    AuthenticationRequiredError,
    BusyError,
    CancelledError,
    EngineUnavailableError,
    InvalidArgumentError,
)
from narumi.providers.codex import _generation, _policy, _rpc, _runtime, _session
from narumi.providers.codex.backend import CodexBackend

CONNECTION = "conn-0123456789abcdef"
OTHER_CONNECTION = "conn-fedcba9876543210"
SECRET = "fixture-codex-backend-private-42368"
ORIGINAL = b'{"fixture_token":"original synthetic credential"}'
REFRESHED = b'{"fixture_token":"refreshed synthetic credential"}'
DEVICE_URL = "https://auth.openai.com/codex/device"
USER_CODE = "ABCD-EFGH"
LOGIN_RESPONSE = {
    "type": "chatgptDeviceCode",
    "loginId": "fixture-login",
    "verificationUrl": DEVICE_URL,
    "userCode": USER_CODE,
}
MODEL = {
    "model": "fixture-model",
    "displayName": "Fixture model",
    "hidden": False,
    "inputModalities": ["text"],
    "defaultReasoningEffort": "low",
    "supportedReasoningEfforts": [{"reasoningEffort": "low"}],
}


class FakeRPC:
    def __init__(self, command, *, env, cwd, should_cancel, options):
        self.command, self.env, self.cwd = command, dict(env), Path(cwd)
        self.options, self.should_cancel = options, should_cancel
        self.state, self.home, self.run = (
            Path(env["CODEX_HOME"]),
            Path(env["HOME"]),
            Path(cwd).parent,
        )
        self.initial_state = {path.name: path.read_bytes() for path in self.state.iterdir()}
        self.initial_home_files = [
            path.relative_to(self.home) for path in self.home.rglob("*") if path.is_file()
        ]
        self.calls, self.notifications, self.waited = [], [], []
        self.closed, self.cancel_count, self.config_count = False, 0, 0
        self.cancelled, self.waiting, self.release = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        self.login_id = "fixture-login"
        self.events = [
            {
                "method": "account/login/completed",
                "params": {"loginId": "other-login", "success": True},
            },
            {
                "method": "account/login/completed",
                "params": {
                    "loginId": self.login_id,
                    "success": options.get("login_success", True),
                    "error": SECRET,
                },
            },
            {"method": "account/updated", "params": {"authMode": "chatgpt"}},
        ]

    def check_cancelled(self):
        if self.cancelled.is_set() or self.should_cancel():
            raise CancelledError("Fixture Codex operation cancelled")

    def refresh_credentials(self):
        path = self.state / "auth.json"
        if "credential_symlink" in self.options:
            path.unlink(missing_ok=True)
            path.symlink_to(self.options["credential_symlink"])
        elif self.options.get("credentials", REFRESHED) is not None:
            path.write_bytes(self.options.get("credentials", REFRESHED))
            path.chmod(0o600)

    def call(self, method, params, **kwargs):
        self.check_cancelled()
        self.calls.append((method, copy.deepcopy(params)))
        if method == self.options.get("block_method"):
            self.wait_until_released()
        if method == self.options.get("fail_method"):
            raise self.options.get("error", _rpc.unavailable("fixture_rpc_failed"))
        if method == "initialize":
            return {"codexHome": self.options.get("initialized_home", str(self.state))}
        if method == "config/read":
            self.config_count += 1
            config = copy.deepcopy(_policy.FIXED_CONFIG)
            if (self.run / "models.json").exists():
                config["model_catalog_json"] = str(self.run / "models.json")
            body = {
                "config": config,
                "layers": [
                    {"name": {"type": "sessionFlags"}, "config": copy.deepcopy(config)},
                    {
                        "name": {"type": "user", "file": str(self.state / "config.toml")},
                        "config": {},
                    },
                    {"name": {"type": "system"}, "config": {}},
                ],
            }
            if mutate := self.options.get("configuration"):
                mutate(body, self.config_count)
            return body
        if method == "configRequirements/read":
            return copy.deepcopy(self.options.get("requirements", {"requirements": None}))
        if method == "account/login/start":
            return copy.deepcopy(self.options.get("login_response", LOGIN_RESPONSE))
        if method == "account/read":
            self.refresh_credentials()
            return copy.deepcopy(
                self.options.get(
                    "account",
                    {
                        "requiresOpenaiAuth": True,
                        "account": {"type": "chatgpt"},
                    },
                )
            )
        if method == "model/list":
            return {"data": [copy.deepcopy(MODEL)], "nextCursor": None}
        pytest.fail(f"unexpected fake RPC method: {method}")

    def notify(self, method, params=None):
        self.notifications.append((method, params))

    def wait_until_released(self):
        self.waiting.set()
        deadline = time.monotonic() + 3
        while not self.release.wait(0.01):
            self.check_cancelled()
            assert time.monotonic() < deadline, "fixture authentication was not released"

    def wait_for(self, predicate, *, timeout):
        self.waiting.set()
        if self.options.get("block"):
            self.wait_until_released()
        while self.events:
            self.check_cancelled()
            message = self.events.pop(0)
            if predicate(message):
                self.waited.append(message["method"])
                return message
        raise _rpc.unavailable("fixture_notification_missing")

    def cancel(self):
        self.cancel_count += 1
        self.cancelled.set()

    def close(self):
        self.closed = True
        if self.options.get("close_error"):
            raise RuntimeError(SECRET)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / "data-root"
    backend = CodexBackend(root)
    fixture = SimpleNamespace(
        root=root,
        backend=backend,
        rpcs=[],
        options=[],
        created=queue.Queue(),
        preflights=[],
        prepared=[],
    )

    def no_process(*args, **kwargs):
        pytest.fail("real Codex or subprocess execution is forbidden")

    def new_rpc(command, **kwargs):
        assert Path(kwargs["env"]["CODEX_HOME"]).is_relative_to(root)
        assert Path(kwargs["env"]["HOME"]).is_relative_to(root)
        assert Path(kwargs["cwd"]).is_relative_to(root)
        options = fixture.options.pop(0) if fixture.options else {}
        rpc = FakeRPC(command, options=options, **kwargs)
        fixture.rpcs.append(rpc)
        fixture.created.put(rpc)
        return rpc

    def prepared():
        fixture.prepared.append(True)
        return root / "fixed-test-codex"

    monkeypatch.setattr(_rpc.subprocess, "Popen", no_process)
    monkeypatch.setattr(_runtime, "installed_candidates", lambda: [])
    monkeypatch.setattr(_runtime, "verify_version", no_process)
    monkeypatch.setattr(_session, "StdioRPC", new_rpc)
    monkeypatch.setattr(_policy, "host_preflight", lambda: fixture.preflights.append(True))
    # A stale browser-login path must fail the test before it can touch a port.
    monkeypatch.setattr(_policy, "check_callback_ports", no_process, raising=False)
    monkeypatch.setattr(backend.runtime, "require_prepared", prepared)
    yield fixture
    backend.close()


def save_credentials(fixture, connection=CONNECTION, contents=ORIGINAL):
    fixture.root.mkdir(mode=0o700, exist_ok=True)
    state = _session.connection_directory(fixture.root, connection) / "state"
    state.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = state / "auth.json"
    path.write_bytes(contents)
    path.chmod(0o600)
    return path


def force_terminated_credential_write(fixture, connection=CONNECTION, marker="a"):
    """Materialize the only file that SIGKILL can leave before atomic replace."""
    state = _session.connection_directory(fixture.root, connection) / "state"
    state.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = state / f".auth.{marker * 32}.tmp"
    temporary.write_bytes(REFRESHED)
    temporary.chmod(0o600)
    return temporary


def authenticate(fixture, connection=CONNECTION, *, callback=None, operation_id=None):
    authorizations = []
    receive = callback or (lambda url, code: authorizations.append((url, code)))
    fixture.backend.authenticate(
        connection,
        on_authorization_code=receive,
        cancelled=lambda: False,
        operation_id=operation_id,
    )
    return authorizations


def assert_clean(fixture):
    assert all(rpc.closed and not rpc.run.exists() for rpc in fixture.rpcs)
    assert fixture.backend._operations == {}


def assert_private(error, reason):
    assert error.details["reason"] == reason
    assert SECRET not in str(error) + repr(error.details)
    assert SECRET not in "".join(traceback.format_exception(error))


def start_auth(fixture, executor, *, connection=CONNECTION, operation_id="fixture-auth"):
    fixture.options.append({"block": True})
    future = executor.submit(authenticate, fixture, connection, operation_id=operation_id)
    rpc = fixture.created.get(timeout=2)
    assert rpc.waiting.wait(2)
    return future, rpc


def test_construction_and_resource_listing_are_passive(setup):
    assert setup.backend.resource()["sha256"] is None
    assert not setup.root.exists()
    assert setup.rpcs == setup.preflights == setup.prepared == []


def test_authentication_transfers_only_credentials_and_commits_after_readiness(
    setup, monkeypatch, tmp_path
):
    saved = save_credentials(setup)
    for name in ("config.toml", "installation_id", "state.sqlite", "environments.toml"):
        path = saved.parent / name
        path.write_text(SECRET)
        path.chmod(0o000)
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    (ambient / "auth.json").write_bytes(b"unrelated synthetic ambient credential")
    monkeypatch.setenv("HOME", str(ambient))
    monkeypatch.setenv("CODEX_HOME", str(ambient))
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    authorizations = []

    def receive_code(url, code):
        assert saved.read_bytes() == ORIGINAL
        authorizations.append((url, code))

    authenticate(setup, callback=receive_code)
    rpc = setup.rpcs[0]
    assert authorizations == [(DEVICE_URL, USER_CODE)]
    assert rpc.initial_state == {"auth.json": ORIGINAL} and rpc.initial_home_files == []
    assert rpc.state != saved.parent and rpc.home != ambient
    assert SECRET not in repr(rpc.env) and "OPENAI_API_KEY" not in rpc.env
    assert rpc.waited == ["account/login/completed", "account/updated"]
    assert rpc.config_count == 2
    assert ("account/login/start", {"type": "chatgptDeviceCode"}) in rpc.calls
    assert ("account/read", {"refreshToken": True}) in rpc.calls
    assert rpc.notifications == [("initialized", None)]
    assert saved.read_bytes() == REFRESHED and stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert_clean(setup)


def test_each_operation_has_fresh_home_and_connection_scoped_credentials(setup):
    save_credentials(setup)
    authenticate(setup)
    authenticate(setup, OTHER_CONNECTION)
    models = setup.backend.list_models(CONNECTION)
    assert [model["model_id"] for model in models] == ["fixture-model"]
    assert [rpc.initial_state.get("auth.json") for rpc in setup.rpcs] == [ORIGINAL, None, REFRESHED]
    assert len({rpc.state for rpc in setup.rpcs}) == len({rpc.home for rpc in setup.rpcs}) == 3
    assert_clean(setup)


def test_failed_login_does_not_replace_credentials_or_accept_readiness(setup):
    saved = save_credentials(setup)
    setup.options.append({"login_success": False})
    with pytest.raises(AuthenticationRequiredError) as failure:
        authenticate(setup)
    assert_private(failure.value, "codex_login_failed")
    assert saved.read_bytes() == ORIGINAL
    assert setup.rpcs[0].waited == ["account/login/completed"]
    assert all(method != "account/read" for method, _ in setup.rpcs[0].calls)
    assert_clean(setup)


@pytest.mark.parametrize(
    "changes",
    [
        {"type": "apiKey"},
        {"type": "chatgpt", "authUrl": "https://auth.openai.com/oauth/authorize"},
        {"loginId": ""},
        {"verificationUrl": "https://untrusted.invalid/"},
        {"verificationUrl": DEVICE_URL + "?user_code=" + USER_CODE},
        {"userCode": None},
        {"userCode": ""},
        {"userCode": "A" * 33},
        {"userCode": "ABCD ＥＦＧＨ"},
        {"userCode": "ABCD EFGH"},
        {"userCode": "ABCD/EFGH"},
        {"userCode": "ABCD\nEFGH"},
    ],
)
def test_untrusted_login_response_never_reaches_ui_or_persistent_state(setup, changes):
    setup.options.append({"login_response": {**LOGIN_RESPONSE, **changes}})
    authorizations = []
    with pytest.raises(EngineUnavailableError):
        authenticate(setup, callback=lambda url, code: authorizations.append((url, code)))
    assert authorizations == []
    assert not (_session.connection_directory(setup.root, CONNECTION) / "state/auth.json").exists()
    assert_clean(setup)


def test_device_login_start_error_never_falls_back_to_browser_or_exposes_upstream_error(setup):
    upstream = EngineUnavailableError(f"HTTP 404: {SECRET}", details={"reason": "codex_rpc_failed"})
    setup.options.append({"fail_method": "account/login/start", "error": upstream})
    authorizations = []
    with pytest.raises(AuthenticationRequiredError) as failure:
        authenticate(setup, callback=lambda url, code: authorizations.append((url, code)))
    assert_private(failure.value, "device_code_login_unavailable")
    assert authorizations == []
    assert [
        params for method, params in setup.rpcs[0].calls if method == "account/login/start"
    ] == [{"type": "chatgptDeviceCode"}]
    assert len(setup.rpcs) == 1
    assert_clean(setup)


@pytest.mark.parametrize("reason", ["codex_process_eof", "codex_rpc_timeout", "cancelled"])
def test_device_start_transport_failure_keeps_original_classification(setup, reason):
    original = (
        CancelledError("Fixture cancellation")
        if reason == "cancelled"
        else _rpc.unavailable(reason)
    )
    setup.options.append({"fail_method": "account/login/start", "error": original})
    with pytest.raises(type(original)) as failure:
        authenticate(setup)
    assert failure.value is original
    assert [
        params for method, params in setup.rpcs[0].calls if method == "account/login/start"
    ] == [{"type": "chatgptDeviceCode"}]
    assert_clean(setup)


@pytest.mark.parametrize("operation", ["auth", "models"])
def test_success_without_fresh_credentials_is_not_reported_as_complete(setup, operation):
    setup.options.append({"credentials": None})
    with pytest.raises(EngineUnavailableError) as failure:
        authenticate(setup) if operation == "auth" else setup.backend.list_models(CONNECTION)
    assert_private(failure.value, "codex_credentials_missing")
    assert_clean(setup)


def test_post_replace_directory_sync_failure_reports_unknown_credential_install(setup, monkeypatch):
    original_replace = _session.os.replace
    original_fsync = _session.os.fsync
    installed = False

    def track_replace(source, target, **kwargs):
        nonlocal installed
        result = original_replace(source, target, **kwargs)
        if target == "auth.json":
            installed = True
        return result

    def fail_installed_directory_sync(descriptor):
        if installed and stat.S_ISDIR(_session.os.fstat(descriptor).st_mode):
            raise OSError("fixture post-replace directory sync failure")
        return original_fsync(descriptor)

    monkeypatch.setattr(_session.os, "replace", track_replace)
    monkeypatch.setattr(_session.os, "fsync", fail_installed_directory_sync)
    with pytest.raises(EngineUnavailableError) as failure:
        authenticate(setup, operation_id="ambiguous-credential-install")

    assert_private(failure.value, "codex_credential_install_outcome_unknown")
    saved = _session.connection_directory(setup.root, CONNECTION) / "state/auth.json"
    assert saved.read_bytes() == REFRESHED
    assert_clean(setup)


@pytest.mark.parametrize(
    "account",
    [
        {"requiresOpenaiAuth": True, "account": None},
        {"requiresOpenaiAuth": True, "account": {"type": "apiKey"}},
        {"requiresOpenaiAuth": True, "account": {"type": "chatgptAuthTokens"}},
        {"requiresOpenaiAuth": False, "account": {"type": "chatgpt"}},
        {"requiresOpenaiAuth": 1, "account": {"type": "chatgpt"}},
    ],
)
def test_model_discovery_requires_explicit_chatgpt_account(setup, account):
    saved = save_credentials(setup)
    setup.options.append({"account": account})
    with pytest.raises(AuthenticationRequiredError) as failure:
        setup.backend.list_models(CONNECTION)
    assert_private(failure.value, "codex_chatgpt_authentication_required")
    assert saved.read_bytes() == ORIGINAL
    assert all(method != "model/list" for method, _ in setup.rpcs[0].calls)
    assert_clean(setup)


@pytest.mark.parametrize(
    "kind,reason",
    [
        ("merged", "codex_configuration_mismatch"),
        ("unexpected", "codex_unexpected_configuration"),
        ("project", "codex_inherited_configuration_present"),
        ("user", "codex_user_configuration_present"),
        ("empty_layers", "codex_configuration_unverified"),
    ],
)
def test_inherited_configuration_is_rejected_before_authentication(setup, kind, reason):
    def mutate(body, count):
        if kind == "merged":
            body["config"]["model_provider"] = SECRET
        elif kind == "unexpected":
            body["config"]["unrecognized"] = SECRET
        elif kind == "project":
            body["layers"].append({"name": {"type": "project"}, "config": {}})
        elif kind == "user":
            body["layers"][1]["config"] = {"model": SECRET}
        else:
            body["layers"] = []

    setup.options.append({"configuration": mutate})
    with pytest.raises(EngineUnavailableError) as failure:
        authenticate(setup)
    assert_private(failure.value, reason)
    assert all(method != "account/login/start" for method, _ in setup.rpcs[0].calls)
    assert_clean(setup)


@pytest.mark.parametrize(
    "requirements", [{}, {"requirements": {}}, {"requirements": {"model": SECRET}}]
)
def test_inherited_requirements_are_rejected_before_authentication(setup, requirements):
    setup.options.append({"requirements": requirements})
    with pytest.raises(EngineUnavailableError) as failure:
        authenticate(setup)
    assert_private(failure.value, "codex_managed_requirements_present")
    assert all(method != "account/login/start" for method, _ in setup.rpcs[0].calls)
    assert_clean(setup)


def test_post_login_configuration_is_rechecked_before_credentials_are_saved(setup):
    saved = save_credentials(setup)

    def mutate(body, count):
        if count == 2:
            body["layers"][1]["config"] = {"model": SECRET}

    setup.options.append({"configuration": mutate})
    with pytest.raises(EngineUnavailableError) as failure:
        authenticate(setup)
    assert_private(failure.value, "codex_user_configuration_present")
    assert saved.read_bytes() == ORIGINAL
    assert setup.rpcs[0].waited == ["account/login/completed", "account/updated"]
    assert_clean(setup)


@pytest.mark.parametrize("where", ["saved", "fresh"])
def test_credential_symlink_is_never_followed_or_written_through(setup, tmp_path, where):
    saved = save_credentials(setup)
    outside = tmp_path / "outside-auth"
    outside.write_bytes(b"unrelated synthetic credential")
    if where == "saved":
        saved.unlink()
        saved.symlink_to(outside)
    else:
        setup.options.append({"credential_symlink": outside})
    with pytest.raises(EngineUnavailableError) as failure:
        setup.backend.list_models(CONNECTION)
    assert_private(failure.value, "codex_operation_failed")
    assert outside.read_bytes() == b"unrelated synthetic credential"
    if where == "saved":
        assert setup.rpcs == []
    else:
        assert saved.read_bytes() == ORIGINAL
    assert list((saved.parent.parent / "runs").iterdir()) == []
    assert_clean(setup)


def test_failed_model_operation_does_not_persist_a_refreshed_credential(setup):
    saved = save_credentials(setup)
    setup.options.append({"fail_method": "model/list", "error": RuntimeError(SECRET)})
    with pytest.raises(EngineUnavailableError) as failure:
        setup.backend.list_models(CONNECTION)
    assert_private(failure.value, "codex_operation_failed")
    assert saved.read_bytes() == ORIGINAL
    assert_clean(setup)


def test_wrong_initialized_codex_home_is_rejected_and_cleaned_up(setup):
    setup.options.append({"initialized_home": SECRET})
    with pytest.raises(EngineUnavailableError) as failure:
        authenticate(setup)
    assert_private(failure.value, "codex_home_mismatch")
    assert [method for method, _ in setup.rpcs[0].calls] == ["initialize"]
    assert_clean(setup)


def test_stale_auth_cancel_cannot_stop_a_new_operation(setup):
    saved = save_credentials(setup)
    with ThreadPoolExecutor(max_workers=1) as executor:
        try:
            first, old = start_auth(setup, executor, operation_id="old-auth")
            (old.state / "auth.json").write_bytes(REFRESHED)
            assert setup.backend.cancel_auth(CONNECTION, operation_id="not-this-auth") is False
            assert not old.cancelled.is_set()
            assert setup.backend.cancel_auth(CONNECTION, operation_id="old-auth") is True
            with pytest.raises(CancelledError):
                first.result(timeout=2)
            assert not saved.exists() and old.closed
            second, new = start_auth(setup, executor, operation_id="new-auth")
            assert setup.backend.cancel_auth(CONNECTION, operation_id="old-auth") is False
            assert not new.cancelled.is_set()
            new.release.set()
            second.result(timeout=2)
        finally:
            setup.backend.close()
    assert saved.read_bytes() == REFRESHED
    assert_clean(setup)


def test_cancel_waits_past_late_credential_copy_and_verifies_cleanup(setup, monkeypatch):
    copied = threading.Event()
    original_copy = _session._copy_credentials
    state = _session.connection_directory(setup.root, CONNECTION) / "state"

    def copy_then_wait_for_cancel(source, destination, root, **kwargs):
        result = original_copy(source, destination, root, **kwargs)
        if destination == state and result:
            copied.set()
            operation = setup.backend._operations[CONNECTION]
            assert operation.cancelled.wait(2)
        return result

    monkeypatch.setattr(_session, "_copy_credentials", copy_then_wait_for_cancel)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            authenticate,
            setup,
            operation_id="late-copy-authentication",
        )
        assert copied.wait(2)
        assert (state / "auth.json").exists()
        assert (
            setup.backend.cancel_auth(CONNECTION, operation_id="late-copy-authentication") is True
        )
        future.result(timeout=2)
    assert not (state / "auth.json").exists()
    runs = state.parent / "runs"
    assert not runs.exists() or list(runs.iterdir()) == []
    assert_clean(setup)


def test_next_session_removes_crash_run_before_copying_credentials(setup):
    save_credentials(setup)
    runs = _session.connection_directory(setup.root, CONNECTION) / "runs"
    orphan = runs / ("d" * 32)
    orphan.mkdir(parents=True, mode=0o700)
    (orphan / "auth.json").write_bytes(REFRESHED)
    setup.backend.list_models(CONNECTION)
    assert not orphan.exists()
    assert_clean(setup)


def test_next_session_recovers_a_force_terminated_credential_replace(setup):
    saved = save_credentials(setup)
    interrupted = force_terminated_credential_write(setup)
    retained = saved.parent / ".auth.not-a-narumi-temporary.tmp"
    retained.write_bytes(b"unrelated synthetic state")
    other = force_terminated_credential_write(setup, OTHER_CONNECTION, marker="b")

    setup.backend.list_models(CONNECTION)

    assert not interrupted.exists()
    assert retained.read_bytes() == b"unrelated synthetic state"
    assert other.read_bytes() == REFRESHED
    assert setup.rpcs[0].initial_state == {"auth.json": ORIGINAL}
    assert_clean(setup)


@pytest.mark.parametrize("action", ["logout", "cancel_auth"])
def test_explicit_cleanup_removes_force_terminated_credential_replace(setup, action):
    saved = save_credentials(setup)
    interrupted = force_terminated_credential_write(setup)

    getattr(setup.backend, action)(CONNECTION)

    assert not saved.exists() and not interrupted.exists()
    assert setup.rpcs == setup.preflights == setup.prepared == []
    assert_clean(setup)


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink", "world_writable"])
def test_unsafe_credential_temporary_is_never_followed_or_deleted(setup, tmp_path, link_kind):
    saved = save_credentials(setup)
    candidate = saved.parent / f".auth.{'c' * 32}.tmp"
    target = tmp_path / "unrelated-private-file"
    target.write_bytes(b"unchanged synthetic contents")
    if link_kind == "symlink":
        candidate.symlink_to(target)
    elif link_kind == "hardlink":
        candidate.hardlink_to(target)
    else:
        candidate.write_bytes(REFRESHED)
        candidate.chmod(0o666)

    with pytest.raises(EngineUnavailableError) as failure:
        setup.backend.logout(CONNECTION)

    assert_private(failure.value, "codex_credential_cleanup_rejected")
    assert saved.read_bytes() == ORIGINAL
    assert candidate.exists()
    assert target.read_bytes() == b"unchanged synthetic contents"
    assert setup.rpcs == setup.preflights == setup.prepared == []
    assert_clean(setup)


def test_credential_temporary_identity_change_is_fail_closed(setup, monkeypatch):
    saved = save_credentials(setup)
    interrupted = force_terminated_credential_write(setup)
    monkeypatch.setattr(_session.os.path, "samestat", lambda _opened, _current: False)

    with pytest.raises(EngineUnavailableError) as failure:
        setup.backend.logout(CONNECTION)

    assert_private(failure.value, "codex_credential_cleanup_rejected")
    assert saved.read_bytes() == ORIGINAL and interrupted.read_bytes() == REFRESHED
    assert setup.rpcs == setup.preflights == setup.prepared == []
    assert_clean(setup)


def test_credential_temporary_unlink_failure_is_fail_closed(setup, monkeypatch):
    saved = save_credentials(setup)
    interrupted = force_terminated_credential_write(setup)
    original_unlink = _session.os.unlink

    def reject_temporary(name, *, dir_fd=None):
        if name == interrupted.name:
            raise OSError("fixture unlink failure")
        return original_unlink(name, dir_fd=dir_fd)

    monkeypatch.setattr(_session.os, "unlink", reject_temporary)
    with pytest.raises(EngineUnavailableError) as failure:
        setup.backend.logout(CONNECTION)

    assert_private(failure.value, "codex_credential_cleanup_unverified")
    assert saved.read_bytes() == ORIGINAL and interrupted.read_bytes() == REFRESHED
    assert setup.rpcs == setup.preflights == setup.prepared == []
    assert_clean(setup)


def test_close_cancels_all_owned_operations_and_prevents_new_children(setup):
    saved = [save_credentials(setup, connection) for connection in (CONNECTION, OTHER_CONNECTION)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        try:
            first, one = start_auth(setup, executor)
            setup.options.append({"block_method": "model/list"})
            second = executor.submit(setup.backend.list_models, OTHER_CONNECTION)
            two = setup.created.get(timeout=2)
            assert two.waiting.wait(2)
            setup.backend.cancel_auth(OTHER_CONNECTION)
            assert not two.cancelled.is_set()
            with pytest.raises(BusyError):
                setup.backend.logout(OTHER_CONNECTION)
            with pytest.raises(BusyError):
                setup.backend.list_models(CONNECTION)
            setup.backend.close()
            for future in (first, second):
                with pytest.raises(CancelledError):
                    future.result(timeout=2)
            assert one.cancel_count > 0 and two.cancel_count > 0
        finally:
            setup.backend.close()
    with pytest.raises(EngineUnavailableError) as failure:
        setup.backend.list_models(CONNECTION)
    assert_private(failure.value, "codex_backend_closed")
    assert len(setup.rpcs) == 2 and all(path.read_bytes() == ORIGINAL for path in saved)
    assert_clean(setup)


def test_device_authentication_can_run_concurrently_for_different_connections(setup):
    with ThreadPoolExecutor(max_workers=2) as executor:
        try:
            first, one = start_auth(setup, executor)
            second, two = start_auth(setup, executor, connection=OTHER_CONNECTION)
            with pytest.raises(BusyError):
                authenticate(setup, CONNECTION)
            one.release.set()
            two.release.set()
            assert first.result(timeout=2) == second.result(timeout=2) == [(DEVICE_URL, USER_CODE)]
        finally:
            setup.backend.close()
    assert one.state != two.state and len(setup.rpcs) == 2
    assert_clean(setup)


def test_logout_is_offline_and_deletes_only_selected_connection_credentials(setup):
    selected = save_credentials(setup)
    other = save_credentials(setup, OTHER_CONNECTION)
    retained = selected.parent / "config.toml"
    retained.write_text("synthetic retained state")
    setup.backend.logout(CONNECTION)
    setup.backend.logout(CONNECTION)
    assert not selected.exists() and other.read_bytes() == ORIGINAL
    assert retained.read_text() == "synthetic retained state"
    assert setup.rpcs == setup.preflights == setup.prepared == []


def test_logout_removes_orphan_runs_without_following_links_or_touching_other_connections(
    setup, tmp_path
):
    selected = save_credentials(setup)
    other = save_credentials(setup, OTHER_CONNECTION)
    orphan = selected.parent.parent / "runs" / ("a" * 32)
    other_run = other.parent.parent / "runs" / ("b" * 32)
    outside = tmp_path / "unrelated-state"
    for path in (orphan, other_run, outside):
        path.mkdir(parents=True, mode=0o700)
        (path / "auth.json").write_bytes(ORIGINAL)
    link = orphan.parent / ("c" * 32)
    link.symlink_to(outside, target_is_directory=True)
    setup.backend.logout(CONNECTION)
    assert not selected.exists() and not orphan.exists()
    assert other.read_bytes() == (other_run / "auth.json").read_bytes() == ORIGINAL
    assert (outside / "auth.json").read_bytes() == ORIGINAL
    assert setup.rpcs == setup.preflights == setup.prepared == []


def test_logout_waits_for_active_auth_cancellation_before_removing_credential(setup):
    saved = save_credentials(setup)
    with ThreadPoolExecutor(max_workers=1) as executor:
        try:
            future, rpc = start_auth(setup, executor)
            setup.backend.logout(CONNECTION)
            with pytest.raises(CancelledError):
                future.result(timeout=2)
        finally:
            setup.backend.close()
    assert not saved.exists() and rpc.cancel_count > 0 and len(setup.rpcs) == 1
    assert_clean(setup)


def test_rpc_close_failure_still_discards_private_home_and_credentials(setup):
    saved = save_credentials(setup)
    setup.options.append({"close_error": True})
    with pytest.raises(EngineUnavailableError) as failure:
        authenticate(setup)
    assert_private(failure.value, "codex_operation_failed")
    assert saved.read_bytes() == ORIGINAL
    assert_clean(setup)


def test_complete_uses_separate_metadata_and_generation_sessions(setup, monkeypatch):
    save_credentials(setup)
    generated = []

    def generate(session, model, parameters, prompt, *, system):
        assert setup.rpcs[0].closed and not setup.rpcs[0].run.exists()
        generated.append((model["model_id"], parameters, prompt, system))
        return "Synthetic minutes"

    monkeypatch.setattr(_generation, "generate", generate)
    assert (
        setup.backend.complete(
            CONNECTION,
            "fixture-model",
            {"reasoning_effort": "low"},
            "Synthetic transcript",
            system="Fixture instructions",
        )
        == "Synthetic minutes"
    )
    assert generated == [
        (
            "fixture-model",
            {"reasoning_effort": "low"},
            "Synthetic transcript",
            "Fixture instructions",
        )
    ]
    assert len(setup.rpcs) == 2 and setup.rpcs[0].state != setup.rpcs[1].state
    assert all(method != "model/list" for method, _ in setup.rpcs[1].calls)
    assert_clean(setup)


def test_generation_success_followed_by_credential_save_failure_is_unknown(setup, monkeypatch):
    save_credentials(setup)

    def generate(session, *args, **kwargs):
        session.generation_attempted = True
        (session.codex_home / "auth.json").unlink()
        return "Synthetic minutes whose completion cannot be committed"

    monkeypatch.setattr(_generation, "generate", generate)
    with pytest.raises(EngineUnavailableError) as failure:
        setup.backend.complete(CONNECTION, "fixture-model", {}, "Synthetic transcript")
    assert_private(failure.value, "codex_generation_outcome_unknown")
    assert_clean(setup)


@pytest.mark.parametrize("primary", ["cancelled", "unknown"])
def test_generation_primary_failure_survives_rpc_cleanup_failure(setup, monkeypatch, primary):
    save_credentials(setup)
    setup.options.extend([{}, {"close_error": True}])
    original = (
        CancelledError("Fixture pre-submission cancellation", details={"outcome_unknown": False})
        if primary == "cancelled"
        else _rpc.unavailable("codex_generation_outcome_unknown")
    )

    def generate(session, *args, **kwargs):
        session.generation_attempted = primary == "unknown"
        raise original

    monkeypatch.setattr(_generation, "generate", generate)
    with pytest.raises(type(original)) as failure:
        setup.backend.complete(CONNECTION, "fixture-model", {}, "Synthetic transcript")
    assert failure.value is original
    if primary == "cancelled":
        assert failure.value.details == {"outcome_unknown": False}
    assert_clean(setup)


@pytest.mark.parametrize("identifier", ["../other", "conn-../../other", "conn-not-hex", ""])
def test_invalid_connection_identifier_fails_before_io(setup, identifier):
    with pytest.raises(InvalidArgumentError):
        setup.backend.list_models(identifier)
    assert not setup.root.exists() and setup.rpcs == []
