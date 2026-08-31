"""Native audio timing and saved chunk semantics, without any provider calls."""

from __future__ import annotations

import copy
import json
import logging
import traceback
from dataclasses import asdict

import pytest
from narumi.errors import EngineUnavailableError, InvalidArgumentError
from narumi.providers.audio_response import (
    MAX_AUDIO_RESPONSE_NODES,
    TRANSCRIPTION_OUTCOME_UNKNOWN,
    AudioSegment,
    AudioTranscriptionResult,
    AudioWord,
    parse_audio_response,
    parse_saved_result,
)

MODELS = ("whisper-1", "gpt-4o-transcribe-diarize")
KEY = "fixture-private-audio-parser-key-not-real"


def native_reply(model_id="whisper-1"):
    segment = {"start": 0, "end": 2, "text": " Fixture transcript "}
    if model_id == "whisper-1":
        segment.update(
            id=0,
            seek=0,
            tokens=[1, 2],
            temperature=0,
            avg_logprob=-0.2,
            compression_ratio=1.1,
            no_speech_prob=0,
        )
        return {
            "duration": 3,
            "language": "english",
            "text": " Fixture transcript ",
            "segments": [segment],
            "words": [{"start": 0, "end": 1, "word": " Fixture "}],
        }
    segment.update(id="seg_0", speaker="speaker_A", type="transcript.text.segment")
    return {
        "duration": 3,
        "task": "transcribe",
        "text": " Fixture transcript ",
        "segments": [segment],
    }


def parse(body, model_id="whisper-1", **options):
    return parse_audio_response(model_id, body, chunk_duration=3, api_key=KEY, **options)


def mutate(value, path, replacement):
    parent = value
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = replacement


def assert_unknown(error):
    assert error.details == {"reason": TRANSCRIPTION_OUTCOME_UNKNOWN, "outcome_unknown": True}
    assert KEY not in str(error.to_payload())
    assert KEY not in "".join(traceback.format_exception(error))


@pytest.mark.parametrize("model_id", MODELS)
def test_native_result_preserves_relative_values_without_response_model_or_status(model_id):
    body = native_reply(model_id)
    result = parse(body, model_id)
    diarized = model_id != "whisper-1"
    assert result == AudioTranscriptionResult(
        text=" Fixture transcript ",
        duration=3.0,
        segments=(
            AudioSegment(
                "seg_0" if diarized else 0,
                0.0,
                2.0,
                " Fixture transcript ",
                "speaker_A" if diarized else None,
            ),
        ),
        words=None if diarized else (AudioWord(0.0, 1.0, " Fixture "),),
        language=None if diarized else "english",
    )
    assert body == native_reply(model_id)
    assert set(asdict(result)) == {"text", "duration", "segments", "words", "language", "usage"}


@pytest.mark.parametrize("model_id", MODELS)
def test_extension_keys_are_inspected_but_do_not_become_stored_diagnostics(model_id):
    body = native_reply(model_id)
    body.update(model="unverified-native-extra", status="unverified", extra={"safe": [1, 2]})
    body["segments"][0]["new_provider_field"] = {"safe": "not provenance"}
    assert parse(body, model_id) == parse(native_reply(model_id), model_id)


@pytest.mark.parametrize("model_id", MODELS)
def test_segment_overlap_is_allowed_without_clamping_or_speaker_identity_inference(model_id):
    body = native_reply(model_id)
    other = copy.deepcopy(body["segments"][0])
    other.update(id=1 if model_id == "whisper-1" else "seg_1", start=1, end=3)
    if model_id != "whisper-1":
        other["speaker"] = "arbitrary opaque speaker label"
    body["segments"].append(other)
    result = parse(body, model_id)
    assert [(segment.start, segment.end) for segment in result.segments] == [(0, 2), (1, 3)]
    if model_id != "whisper-1":
        assert result.segments[1].speaker == "arbitrary opaque speaker label"


@pytest.mark.parametrize("model_id", MODELS)
@pytest.mark.parametrize("duration", [0, 3])
def test_silence_is_valid_without_inventing_a_segment_or_usage(model_id, duration):
    body = native_reply(model_id)
    body.update(text="", segments=[], duration=duration, usage=None)
    body.pop("words", None)
    result = parse(body, model_id)
    assert result.segments == () and result.words is None and result.usage is None
    assert result.text == "" and result.duration == duration


@pytest.mark.parametrize("missing", [None, "absent"])
def test_optional_whisper_arrays_may_be_absent_only_when_no_segment_text_is_needed(missing):
    body = native_reply()
    body.update(text="", segments=None, words=None)
    if missing == "absent":
        del body["segments"], body["words"]
    assert parse(body).segments == ()
    body["text"] = "Transcript without timestamps"
    with pytest.raises(EngineUnavailableError) as caught:
        parse(body)
    assert_unknown(caught.value)


def test_words_alone_do_not_invent_native_segments():
    body = native_reply()
    del body["segments"]
    with pytest.raises(EngineUnavailableError) as caught:
        parse(body)
    assert_unknown(caught.value)


@pytest.mark.parametrize("model_id", MODELS)
@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("duration",), -1),
        (("duration",), 3.0000000001),
        (("duration",), True),
        (("duration",), "3"),
        (("duration",), float("nan")),
        (("duration",), float("inf")),
        (("duration",), 10**1000),
        (("text",), None),
        (("text",), ""),
        (("segments",), []),
        (("segments",), None),
        (("segments", 0, "start"), -0.0000000001),
        (("segments", 0, "start"), 2.1),
        (("segments", 0, "end"), 0),
        (("segments", 0, "end"), 3.0000000001),
        (("segments", 0, "end"), False),
        (("segments", 0, "end"), None),
        (("segments", 0, "text"), ""),
    ],
)
def test_native_invalid_timing_and_text_are_unknown_not_partial_success(
    model_id, path, replacement
):
    body = native_reply(model_id)
    mutate(body, path, replacement)
    with pytest.raises(EngineUnavailableError) as caught:
        parse(body, model_id)
    assert_unknown(caught.value)


@pytest.mark.parametrize("model_id", MODELS)
def test_segments_must_also_fit_native_duration_and_have_unique_ids(model_id):
    body = native_reply(model_id)
    body["duration"] = 1.9999999999
    with pytest.raises(EngineUnavailableError):
        parse(body, model_id)
    body = native_reply(model_id)
    body["segments"].append(copy.deepcopy(body["segments"][0]))
    with pytest.raises(EngineUnavailableError):
        parse(body, model_id)


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("id", "0"),
        ("seek", -1),
        ("tokens", [True]),
        ("tokens", None),
        ("temperature", "0"),
        ("avg_logprob", float("inf")),
        ("compression_ratio", None),
        ("no_speech_prob", False),
    ],
)
def test_whisper_native_segment_diagnostics_have_required_types(field, bad):
    body = native_reply()
    body["segments"][0][field] = bad
    with pytest.raises(EngineUnavailableError):
        parse(body)
    del body["segments"][0][field]
    with pytest.raises(EngineUnavailableError):
        parse(body)


@pytest.mark.parametrize(
    ("path", "bad"),
    [
        (("task",), "translate"),
        (("segments", 0, "id"), 0),
        (("segments", 0, "id"), ""),
        (("segments", 0, "speaker"), None),
        (("segments", 0, "speaker"), "A\nB"),
        (("segments", 0, "type"), "transcript.text.delta"),
    ],
)
def test_diarization_requires_final_native_segments_with_opaque_labels(path, bad):
    body = native_reply(MODELS[1])
    mutate(body, path, bad)
    with pytest.raises(EngineUnavailableError) as caught:
        parse(body, MODELS[1])
    assert_unknown(caught.value)


@pytest.mark.parametrize("field", ["language", "text", "duration"])
def test_whisper_required_top_fields_cannot_be_missing(field):
    body = native_reply()
    del body[field]
    with pytest.raises(EngineUnavailableError):
        parse(body)


@pytest.mark.parametrize(
    ("field", "value"),
    [("start", -1), ("start", 1.1), ("end", 3.0000000001), ("end", True), ("word", " ")],
)
def test_words_require_valid_native_spans(field, value):
    body = native_reply()
    body["words"][0][field] = value
    with pytest.raises(EngineUnavailableError):
        parse(body)


def test_zero_duration_blank_segment_is_not_fabricated_speech():
    body = native_reply()
    body.update(text="", words=[])
    body["segments"][0].update(start=0, end=0, text="")
    result = parse(body)
    assert result.segments[0].start == result.segments[0].end == 0


@pytest.mark.parametrize("model_id", MODELS)
def test_duration_usage_is_native_and_not_derived_from_audio_length(model_id):
    body = native_reply(model_id)
    body["usage"] = {"type": "duration", "seconds": 4, "future_field": 9}
    assert parse(body, model_id).usage == {"type": "duration", "seconds": 4.0}


def test_token_usage_preserves_present_counts_without_zero_filling_optional_details():
    body = native_reply(MODELS[1])
    body["usage"] = {"type": "tokens", "input_tokens": 20, "output_tokens": 7, "total_tokens": 29}
    assert parse(body, MODELS[1]).usage == body["usage"]
    body["usage"]["input_token_details"] = {"audio_tokens": 18}
    assert parse(body, MODELS[1]).usage["input_token_details"] == {"audio_tokens": 18}


@pytest.mark.parametrize(
    "usage",
    [
        {},
        {"type": "duration", "seconds": -1},
        {"type": "duration", "seconds": True},
        {"type": "tokens", "input_tokens": 2, "output_tokens": 3},
        {"type": "tokens", "input_tokens": 2, "output_tokens": 3, "total_tokens": False},
    ],
)
def test_invalid_native_usage_does_not_become_invented_or_partial_usage(usage):
    body = native_reply(MODELS[1])
    body["usage"] = usage
    with pytest.raises(EngineUnavailableError):
        parse(body, MODELS[1])


@pytest.mark.parametrize("field", ["error", "refusal", "incomplete_details"])
def test_explicit_error_fields_are_not_accepted_with_otherwise_valid_transcripts(field, caplog):
    body = native_reply()
    body[field] = {"message": KEY}
    with pytest.raises(EngineUnavailableError) as caught:
        parse(body)
    assert_unknown(caught.value)
    logging.getLogger(__name__).error("Synthetic audio error", exc_info=caught.value)
    assert KEY not in caplog.text


@pytest.mark.parametrize("where", ["text", "unused", "context", "key", "segment", "word", "usage"])
def test_raw_key_reflection_anywhere_is_rejected_even_in_ignored_fields(where):
    body = native_reply()
    if where in {"text", "unused", "context"}:
        body[where] = "reflected " + KEY
    elif where == "key":
        body["extension " + KEY] = None
    elif where == "segment":
        body["segments"][0]["extra"] = [KEY]
    elif where == "word":
        body["words"][0]["word"] = KEY
    else:
        body["usage"] = {"type": "duration", "seconds": 1, "unused": KEY}
    with pytest.raises(EngineUnavailableError) as caught:
        parse(body)
    assert_unknown(caught.value)


def test_large_but_bounded_native_word_array_has_audio_specific_structure_budget():
    body = native_reply()
    body["words"] *= 4_000
    assert len(parse(body).words) == 4_000
    body["words"] *= 6
    with pytest.raises(EngineUnavailableError):
        parse(body)


def test_payload_limits_include_unused_fields_and_text_length():
    body = native_reply()
    body["unused"] = [None] * MAX_AUDIO_RESPONSE_NODES
    with pytest.raises(EngineUnavailableError):
        parse(body)
    body = native_reply()
    body["text"] = "x" * 1_048_577
    with pytest.raises(EngineUnavailableError):
        parse(body)
    body = native_reply()
    nested = []
    body["unused"] = nested
    for _ in range(33):
        child = []
        nested.append(child)
        nested = child
    with pytest.raises(EngineUnavailableError):
        parse(body)


@pytest.mark.parametrize("model_id", MODELS)
@pytest.mark.parametrize("serialized", [False, True])
def test_saved_results_roundtrip_dataclass_tuples_and_json_lists(model_id, serialized):
    expected = parse(native_reply(model_id), model_id)
    payload = asdict(expected)
    if serialized:
        payload = json.loads(json.dumps(payload))
    assert parse_saved_result(payload, model_id=model_id, chunk_duration=3) == expected


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("duration",), 4),
        (("text",), ""),
        (("segments", 0, "native_id"), "0"),
        (("segments", 0, "speaker"), "invented"),
        (("segments", 0, "end"), 3.0000000001),
        (("words", 0, "text"), ""),
        (("words", 0, "end"), -1),
        (("language",), None),
        (("usage",), {"type": "duration", "seconds": 1, "extra": "unverified"}),
    ],
)
def test_saved_corruption_is_fixed_failure_not_native_unknown_or_cache_miss(path, value):
    payload = json.loads(json.dumps(asdict(parse(native_reply()))))
    mutate(payload, path, value)
    with pytest.raises(EngineUnavailableError) as caught:
        parse_saved_result(payload, model_id="whisper-1", chunk_duration=3)
    assert caught.value.details == {"reason": "transcription_saved_result_invalid"}


@pytest.mark.parametrize("extra", ["top", "segment", "word"])
def test_saved_normalized_shape_does_not_accept_extra_fields(extra):
    payload = asdict(parse(native_reply()))
    target = payload if extra == "top" else payload[extra + "s"][0]
    target["raw_response"] = "unverified native details"
    with pytest.raises(EngineUnavailableError):
        parse_saved_result(payload, model_id="whisper-1", chunk_duration=3)


@pytest.mark.parametrize("duration", [False, 0, -1, 600.0000001, float("nan"), 10**1000])
def test_parser_context_validation_is_known_and_bounded(duration):
    with pytest.raises(InvalidArgumentError):
        parse_audio_response("whisper-1", native_reply(), chunk_duration=duration)
    with pytest.raises(InvalidArgumentError):
        parse_saved_result({}, model_id="whisper-1", chunk_duration=duration)
