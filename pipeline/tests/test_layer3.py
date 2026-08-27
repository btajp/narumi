from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from narumi.bundle import Bundle
from narumi.diarize.layer3 import (
    BOUNDARY_WINDOW_SEC,
    LAYER3_KEY,
    LAYER3_NAMES_PATH,
    LAYER3_OUTPUT,
    LAYER3_PROMPT_VERSION,
    LAYER3_SYSTEM_PROMPT,
    BoundarySample,
    boundary_samples,
    drop_layer3,
    load_layer3_names,
    run_layer3,
    validate_layer3_answer,
)
from narumi.diarize.stage import LAYER2_KEY
from narumi.errors import ErrorCode, NarumiError, NotFoundError, PolicyViolationError
from narumi.llm import CapabilityProfile, FakeCall, FakeProvider, registry
from narumi.llm.vision import parse_json_answer
from narumi.models import Diarization, EngineInfo, ExternalSendPolicy, MeetingConfig, Turn
from narumi.slides import SLIDES_KEY, run_slides

from .media_fixtures import make_bundle_with_tracks
from .test_slides import add_screen_track

VISION_LOCAL = CapabilityProfile(
    vision=True, context_window=8000, cost_class="local", data_destination="local", tool_use=False
)
VISION_API = CapabilityProfile(
    vision=True,
    context_window=200_000,
    cost_class="api",
    data_destination="anthropic",
    tool_use=True,
)

Canned = dict[int, tuple[str | None, float, str]]


@dataclass
class FakeVisionProvider(FakeProvider):
    """FakeProvider that answers vision calls with canned per-image JSON."""

    name: str = "fake-vision"
    profile: CapabilityProfile = VISION_LOCAL
    canned: Canned = field(default_factory=dict)
    raw_answer: str | None = None

    def complete(self, prompt, *, system=None, images=None, max_tokens=None):  # type: ignore[override]
        if not images:
            return super().complete(prompt, system=system, images=images, max_tokens=max_tokens)
        self.calls.append(FakeCall(prompt, system, tuple(images), max_tokens))
        if self.raw_answer is not None:
            return self.raw_answer
        # canned indices are global across the batches of one run
        consumed = sum(len(call.images) for call in self.calls[:-1])
        items = []
        for i in range(1, len(images) + 1):
            name, confidence, evidence = self.canned.get(consumed + i, (None, 0.0, ""))
            items.append({"image": i, "name": name, "confidence": confidence, "evidence": evidence})
        return json.dumps(items, ensure_ascii=False)


def register_provider(monkeypatch, provider) -> None:
    monkeypatch.setitem(registry.PROVIDER_PROFILES, provider.name, provider.profile)
    monkeypatch.setitem(registry._FACTORIES, provider.name, lambda: provider)


def write_layer2(bundle: Bundle, turns: list[Turn]) -> None:
    diarization = Diarization(layer=2, engine=EngineInfo(name="fake", version="1"), turns=turns)

    def produce(out: Path) -> None:
        bundle.write_json("diarization/layer2-fake.json", diarization)

    bundle.run_stage(
        LAYER2_KEY,
        inputs={},
        params={"engine": "fake"},
        producer=("fake", "1"),
        output="diarization/layer2-fake.json",
        fn=produce,
    )


def turn2(start: float, end: float, speaker: str) -> Turn:
    return Turn(start=start, end=end, speaker=speaker, layer=2)


def vision_bundle(
    tmp_path: Path,
    *,
    provider_name: str = "fake-vision",
    policy: ExternalSendPolicy = ExternalSendPolicy.LOCAL_ONLY,
    turns: list[Turn] | None = None,
) -> Bundle:
    config = MeetingConfig(
        transcription_engine="fake",
        diarization_engine="fake",
        llm_provider=provider_name,
        external_send_policy=policy,
    )
    bundle = make_bundle_with_tracks(tmp_path, seconds=12.0, config=config)
    add_screen_track(bundle)
    assert run_slides(bundle) is not None
    write_layer2(
        bundle,
        turns
        if turns is not None
        else [turn2(0.0, 10.0, "SPEAKER_00"), turn2(10.0, 12.0, "SPEAKER_01")],
    )
    return bundle


# ----------------------------------------------------------------------------- happy path
def test_run_layer3_writes_turns_and_names(tmp_path: Path, monkeypatch):
    provider = FakeVisionProvider(
        canned={1: ("田中", 0.9, "名前枠がハイライト"), 2: ("佐藤", 0.8, "マイクアイコン")}
    )
    register_provider(monkeypatch, provider)
    bundle = vision_bundle(tmp_path)

    result = run_layer3(bundle)
    assert result is not None and not result.skipped
    assert result.key == LAYER3_KEY

    record = bundle.artifact(LAYER3_KEY)
    assert record is not None
    assert record.path == LAYER3_OUTPUT
    assert record.inputs == {
        LAYER2_KEY: bundle.artifact_hash(LAYER2_KEY),
        SLIDES_KEY: bundle.artifact_hash(SLIDES_KEY),
    }
    assert record.params == {
        "provider": "fake-vision",
        "prompt_version": LAYER3_PROMPT_VERSION,
        "window": BOUNDARY_WINDOW_SEC,
        "batch_size": 4,
    }

    diarization = Diarization.model_validate(bundle.read_json(LAYER3_OUTPUT))
    assert diarization.layer == 3
    assert diarization.engine.name == "screen-vision"
    assert diarization.engine.params["prompt_version"] == LAYER3_PROMPT_VERSION
    assert [(t.start, t.end, t.speaker, t.confidence, t.layer) for t in diarization.turns] == [
        (0.0, 10.0, "SPEAKER_00", 0.9, 3),
        (10.0, 12.0, "SPEAKER_01", 0.8, 3),
    ]

    names = load_layer3_names(bundle)
    assert set(names) == {"SPEAKER_00", "SPEAKER_01"}
    assert names["SPEAKER_00"].name == "田中"
    assert names["SPEAKER_00"].confidence == 0.9
    assert "00:00:00" in names["SPEAKER_00"].evidence
    assert names["SPEAKER_01"].name == "佐藤"

    # one batch of two boundary frames went to the provider with the fixed prompt
    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert len(call.images) == 2
    assert call.system == LAYER3_SYSTEM_PROMPT
    assert "出力は JSON 配列のみ" in call.prompt
    assert "画像 1" in call.prompt and "画像 2" in call.prompt


def test_run_layer3_idempotent_then_forced(tmp_path: Path, monkeypatch):
    provider = FakeVisionProvider(canned={1: ("田中", 0.9, "x"), 2: ("佐藤", 0.8, "y")})
    register_provider(monkeypatch, provider)
    bundle = vision_bundle(tmp_path)
    first = run_layer3(bundle)
    assert first is not None and not first.skipped
    calls = len(provider.calls)
    again = run_layer3(Bundle.open(bundle.path))
    assert again is not None and again.skipped
    assert len(provider.calls) == calls
    forced = run_layer3(bundle, force=True)
    assert forced is not None and not forced.skipped
    assert len(provider.calls) > calls


def test_run_layer3_aggregates_votes(tmp_path: Path, monkeypatch):
    provider = FakeVisionProvider(
        canned={1: ("田中", 0.9, "a"), 2: ("田仲", 0.4, "b"), 3: ("佐藤", 0.8, "c")}
    )
    register_provider(monkeypatch, provider)
    turns = [
        turn2(0.0, 5.0, "SPEAKER_00"),
        turn2(5.0, 10.0, "SPEAKER_00"),
        turn2(10.0, 12.0, "SPEAKER_01"),
    ]
    bundle = vision_bundle(tmp_path, turns=turns)
    assert run_layer3(bundle) is not None
    names = load_layer3_names(bundle)
    assert names["SPEAKER_00"].name == "田中"  # highest total confidence wins
    assert names["SPEAKER_00"].confidence == 0.9
    diarization = Diarization.model_validate(bundle.read_json(LAYER3_OUTPUT))
    assert len(diarization.turns) == 3


def test_run_layer3_unidentified_boundaries(tmp_path: Path, monkeypatch):
    provider = FakeVisionProvider(canned={1: (None, 0.0, ""), 2: ("佐藤", 0.8, "y")})
    register_provider(monkeypatch, provider)
    bundle = vision_bundle(tmp_path)
    assert run_layer3(bundle) is not None
    diarization = Diarization.model_validate(bundle.read_json(LAYER3_OUTPUT))
    assert [t.speaker for t in diarization.turns] == ["SPEAKER_01"]
    assert set(load_layer3_names(bundle)) == {"SPEAKER_01"}


def test_run_layer3_no_boundary_near_a_frame(tmp_path: Path, monkeypatch):
    provider = FakeVisionProvider()
    register_provider(monkeypatch, provider)
    # frames exist at ~0/5/6/11 s; a turn starting at 30 s has none within ±2 s
    bundle = vision_bundle(tmp_path, turns=[turn2(30.0, 35.0, "SPEAKER_00")])
    result = run_layer3(bundle)
    assert result is not None and not result.skipped
    assert provider.calls == []
    diarization = Diarization.model_validate(bundle.read_json(LAYER3_OUTPUT))
    assert diarization.turns == []
    assert load_layer3_names(bundle) == {}


# ----------------------------------------------------------------------------- skip paths
def test_run_layer3_skips_without_vision(tmp_path: Path):
    bundle = vision_bundle(tmp_path, provider_name="fake")  # fake has no vision
    assert run_layer3(bundle) is None
    assert bundle.artifact(LAYER3_KEY) is None

    bundle.manifest.config = MeetingConfig(llm_provider="none")
    bundle.save()
    assert run_layer3(bundle) is None


def test_run_layer3_skips_without_upstream_artifacts(tmp_path: Path, monkeypatch):
    provider = FakeVisionProvider()
    register_provider(monkeypatch, provider)
    config = MeetingConfig(llm_provider="fake-vision")
    no_layer2 = make_bundle_with_tracks(tmp_path / "a", seconds=12.0, config=config)
    add_screen_track(no_layer2)
    assert run_slides(no_layer2) is not None
    assert run_layer3(no_layer2) is None  # layer-2 artifact missing

    no_slides = make_bundle_with_tracks(tmp_path / "b", seconds=12.0, config=config)
    write_layer2(no_slides, [turn2(0.0, 10.0, "SPEAKER_00")])
    assert run_layer3(no_slides) is None  # slides artifact missing
    assert provider.calls == []


def test_run_layer3_drops_stale_artifact_when_disabled(tmp_path: Path, monkeypatch):
    provider = FakeVisionProvider(canned={1: ("田中", 0.9, "x"), 2: ("佐藤", 0.8, "y")})
    register_provider(monkeypatch, provider)
    bundle = vision_bundle(tmp_path)
    assert run_layer3(bundle) is not None
    assert bundle.abspath(LAYER3_NAMES_PATH).exists()

    bundle.manifest.config = bundle.manifest.config.model_copy(update={"llm_provider": "none"})
    bundle.save()
    assert run_layer3(bundle) is None
    assert bundle.artifact(LAYER3_KEY) is None
    assert not bundle.abspath(LAYER3_OUTPUT).exists()
    assert not bundle.abspath(LAYER3_NAMES_PATH).exists()
    assert drop_layer3(bundle) is False  # nothing left to drop


def test_run_layer3_missing_frames_dir(tmp_path: Path, monkeypatch):
    provider = FakeVisionProvider()
    register_provider(monkeypatch, provider)
    bundle = vision_bundle(tmp_path)
    for frame in bundle.abspath("preprocess/frames").iterdir():
        frame.unlink()
    with pytest.raises(NotFoundError):
        run_layer3(bundle)


# ----------------------------------------------------------------------------- policy
def test_run_layer3_policy_violation_is_loud(tmp_path: Path, monkeypatch):
    provider = FakeVisionProvider(name="fake-vision-api", profile=VISION_API)
    register_provider(monkeypatch, provider)
    for policy in (ExternalSendPolicy.LOCAL_ONLY, ExternalSendPolicy.SUBSCRIPTION_OK):
        bundle = vision_bundle(
            tmp_path / policy.value, provider_name="fake-vision-api", policy=policy
        )
        with pytest.raises(PolicyViolationError):
            run_layer3(bundle)
        assert bundle.artifact(LAYER3_KEY) is None
        assert provider.calls == []

    allowed = vision_bundle(
        tmp_path / "ok", provider_name="fake-vision-api", policy=ExternalSendPolicy.API_OK
    )
    provider.canned = {1: ("田中", 0.9, "x"), 2: ("佐藤", 0.8, "y")}
    assert run_layer3(allowed) is not None


# ----------------------------------------------------------------------------- strict parsing
def test_run_layer3_rejects_non_json_answer(tmp_path: Path, monkeypatch):
    provider = FakeVisionProvider(raw_answer="ここに JSON はありません")
    register_provider(monkeypatch, provider)
    bundle = vision_bundle(tmp_path)
    with pytest.raises(NarumiError) as excinfo:
        run_layer3(bundle)
    assert excinfo.value.code == ErrorCode.INTERNAL
    assert bundle.artifact(LAYER3_KEY) is None


def test_run_layer3_accepts_fenced_json(tmp_path: Path, monkeypatch):
    answer = (
        '```json\n[{"image": 1, "name": "田中", "confidence": 0.9, "evidence": "x"},'
        ' {"image": 2, "name": null, "confidence": 0.0, "evidence": ""}]\n```'
    )
    provider = FakeVisionProvider(raw_answer=answer)
    register_provider(monkeypatch, provider)
    bundle = vision_bundle(tmp_path)
    assert run_layer3(bundle) is not None
    assert load_layer3_names(bundle)["SPEAKER_00"].name == "田中"


@pytest.mark.parametrize(
    "item",
    [
        {"image": 99, "name": "x", "confidence": 0.5, "evidence": ""},  # out of range
        {"image": True, "name": "x", "confidence": 0.5, "evidence": ""},  # bool is not an int
        {"image": 1, "name": "", "confidence": 0.5, "evidence": ""},  # empty name
        {"image": 1, "name": "x", "confidence": 1.5, "evidence": ""},  # confidence > 1
        {"image": 1, "name": "x", "confidence": 0.5},  # missing key
        {"image": 1, "name": "x", "confidence": 0.5, "evidence": "", "extra": 1},
    ],
)
def test_validate_layer3_answer_rejects(item):
    with pytest.raises(NarumiError) as excinfo:
        validate_layer3_answer([item], 2)
    assert excinfo.value.code == ErrorCode.INTERNAL


def test_validate_layer3_answer_shape():
    with pytest.raises(NarumiError):
        validate_layer3_answer({"image": 1}, 1)  # not an array
    with pytest.raises(NarumiError):
        validate_layer3_answer(["x"], 1)  # element is not an object
    duplicated = [
        {"image": 1, "name": "x", "confidence": 0.5, "evidence": ""},
        {"image": 1, "name": "y", "confidence": 0.5, "evidence": ""},
    ]
    with pytest.raises(NarumiError):
        validate_layer3_answer(duplicated, 2)
    ok = validate_layer3_answer([{"image": 2, "name": None, "confidence": 0, "evidence": ""}], 2)
    assert ok[0]["image"] == 2


def test_parse_json_answer():
    assert parse_json_answer(" [1, 2] ") == [1, 2]
    assert parse_json_answer('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(NarumiError):
        parse_json_answer("ただの文章")


# ----------------------------------------------------------------------------- sampling
def test_boundary_samples_picks_nearest_within_window(tmp_path: Path):
    frames = []
    for ms in (0, 5000, 11000):
        frame = tmp_path / f"frame_{len(frames):04d}_{ms:08d}.png"
        frame.touch()
        frames.append(frame)
    turns = [
        turn2(0.0, 10.0, "SPEAKER_00"),
        turn2(10.0, 12.0, "SPEAKER_01"),  # nearest is 11.0 (within 2 s)
        turn2(30.0, 31.0, "SPEAKER_02"),  # nothing within 2 s
    ]
    samples = boundary_samples(turns, frames)
    assert samples == [
        BoundarySample(turn_index=0, label="SPEAKER_00", time=0.0, frame=frames[0]),
        BoundarySample(turn_index=1, label="SPEAKER_01", time=11.0, frame=frames[2]),
    ]
    assert boundary_samples(turns, []) == []
