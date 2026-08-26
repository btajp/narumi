"""``external_send_policy`` enforcement (絶対原則 4: never silently fall back)."""

from __future__ import annotations

from narumi.errors import PolicyViolationError
from narumi.llm.base import CapabilityProfile
from narumi.models import ExternalSendPolicy

LOCAL_DESTINATION = "local"


def is_allowed(profile: CapabilityProfile, policy: ExternalSendPolicy) -> bool:
    if profile.data_destination == LOCAL_DESTINATION:
        return True
    if policy == ExternalSendPolicy.LOCAL_ONLY:
        return False
    if policy == ExternalSendPolicy.SUBSCRIPTION_OK:
        return profile.cost_class == "subscription"
    return policy == ExternalSendPolicy.API_OK


def check_policy(
    profile: CapabilityProfile,
    policy: ExternalSendPolicy,
    *,
    provider: str | None = None,
) -> None:
    """Raise :class:`PolicyViolationError` when ``profile`` may not be used under ``policy``.

    - ``local_only``: only ``data_destination == "local"``
    - ``subscription_ok``: local, or ``cost_class == "subscription"``
    - ``api_ok``: anything
    """
    policy = ExternalSendPolicy(policy)
    if is_allowed(profile, policy):
        return
    who = f"provider {provider!r}" if provider else "provider"
    raise PolicyViolationError(
        f"{who} sends data to {profile.data_destination!r} ({profile.cost_class}), "
        f"which external_send_policy={policy.value!r} does not allow",
        details={
            "provider": provider,
            "data_destination": profile.data_destination,
            "cost_class": profile.cost_class,
            "policy": policy.value,
        },
    )
