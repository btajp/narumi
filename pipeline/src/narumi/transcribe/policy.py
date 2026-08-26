"""``external_send_policy`` enforcement for media engines (transcription / diarization).

This is the media-side twin of ``narumi.llm``'s provider policy check: the meeting policy is
matched against the engine profile and a violation raises ``PolicyViolationError`` — engines
are never swapped silently.
"""

from __future__ import annotations

from typing import Protocol

from narumi.errors import PolicyViolationError
from narumi.models import ExternalSendPolicy
from narumi.transcribe.base import (
    COST_CLASS_API,
    COST_CLASS_SUBSCRIPTION,
    DATA_DESTINATION_LOCAL,
)


class SendProfile(Protocol):
    """The subset of an engine profile the policy check needs."""

    sends_audio_externally: bool
    data_destination: str
    cost_class: str


def is_local(profile: SendProfile) -> bool:
    return not profile.sends_audio_externally and profile.data_destination == DATA_DESTINATION_LOCAL


def check_send_policy(
    policy: ExternalSendPolicy | str, profile: SendProfile, *, subject: str
) -> None:
    """Raise ``PolicyViolationError`` when ``profile`` may not be used under ``policy``.

    ``local_only`` admits only local profiles; ``subscription_ok`` additionally admits
    ``cost_class == "subscription"``; ``api_ok`` admits everything. A non-local profile with an
    unknown cost class is treated as metered (fail closed).
    """
    policy = ExternalSendPolicy(policy)
    if is_local(profile):
        return
    if policy == ExternalSendPolicy.API_OK:
        return
    if (
        policy == ExternalSendPolicy.SUBSCRIPTION_OK
        and profile.cost_class == COST_CLASS_SUBSCRIPTION
    ):
        return
    required = (
        ExternalSendPolicy.SUBSCRIPTION_OK
        if profile.cost_class == COST_CLASS_SUBSCRIPTION
        else ExternalSendPolicy.API_OK
    )
    raise PolicyViolationError(
        f"{subject} sends audio to {profile.data_destination!r} "
        f"(cost class {profile.cost_class!r}) but external_send_policy is {policy.value!r}; "
        f"choose a local engine or set the policy to {required.value!r}",
        details={
            "subject": subject,
            "policy": policy.value,
            "data_destination": profile.data_destination,
            "cost_class": profile.cost_class or COST_CLASS_API,
            "required_policy": required.value,
        },
    )
