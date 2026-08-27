"""The server's Gaia configuration is bound per operation, never to a shared registry."""

from types import SimpleNamespace

import pytest
from narumi import pipeline


def test_default_steps_are_unchanged() -> None:
    assert pipeline._process_steps(None) is pipeline.PROCESS_STEPS


def test_bound_brief_factories_do_not_change_the_shared_steps(monkeypatch) -> None:
    clients = [object(), object()]
    calls = []
    monkeypatch.setattr(
        pipeline,
        "run_brief",
        lambda bundle, client, **kwargs: calls.append((bundle, client, kwargs)),
    )
    original = pipeline.PROCESS_STEPS
    bound = [pipeline._process_steps(lambda client=client: client) for client in clients]
    bundle = object()
    for steps in bound:
        dict(steps)[pipeline.STAGE_BRIEF](bundle, True)
        for name, step in steps:
            if name != pipeline.STAGE_BRIEF:
                assert step is dict(original)[name]
    assert pipeline.PROCESS_STEPS is original
    assert calls == [(bundle, client, {"force": True}) for client in clients]


@pytest.mark.parametrize("operation", ["process_meeting", "refresh_meeting"])
def test_process_and_refresh_bind_the_supplied_factory(monkeypatch, operation) -> None:
    client = object()
    seen = []
    monkeypatch.setattr(pipeline, "run_brief", lambda bundle, actual, **_: seen.append(actual))
    monkeypatch.setattr(pipeline, "_record_regeneration", lambda *args, **kwargs: None)

    def run_steps(bundle, steps, **kwargs):
        dict(steps)[pipeline.STAGE_BRIEF](bundle, False)
        return pipeline.ProcessResult(meeting_id="test", minutes_version=None)

    monkeypatch.setattr(pipeline, "_run_steps", run_steps)
    getattr(pipeline, operation)(object(), gaia_client_factory=lambda: client)
    assert seen == [client]


def test_gaia_export_binds_a_fresh_exporter_without_mutating_registry(monkeypatch) -> None:
    client = object()
    calls = []

    class FakeGaiaExporter:
        def __init__(self, *, client_factory=None):
            self.client_factory = client_factory

        def export(self, bundle, *, minutes_version, options):
            calls.append((self.client_factory(), minutes_version, options))
            return SimpleNamespace(
                destination="gaia-library",
                ref="gaia://proposal/1",
                minutes_version=1,
                at="2026-08-27T00:00:00Z",
                details={},
            )

    registered = FakeGaiaExporter()
    monkeypatch.setattr(pipeline, "GaiaExporter", FakeGaiaExporter)
    monkeypatch.setattr(pipeline, "get_exporter", lambda _: registered)
    bundle = SimpleNamespace(
        manifest=SimpleNamespace(minutes_versions=[SimpleNamespace(version=1)], exports=[]),
        save=lambda: None,
    )
    result = pipeline.export_meeting(
        bundle, "gaia-library", gaia_client_factory=lambda: client, request_id="runtime-test"
    )
    assert result.ref == "gaia://proposal/1"
    assert calls == [(client, 1, {})]
    assert registered.client_factory is None
    assert bundle.manifest.exports[0].request_id == "runtime-test"
