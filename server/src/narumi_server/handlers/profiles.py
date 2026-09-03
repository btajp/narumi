"""Profile tools: ``list_profiles`` / ``get_profile`` / ``set_profile`` / ``delete_profile``.

Storage lives in ``narumi.profiles.ProfileStore`` (``<NARUMI_HOME>/profiles.json``); this module
adds the server-side checks: the stored config must pass the engine / provider registries and
its ``external_send_policy`` (絶対原則 4 — validated at save time, nothing is silently
downgraded), and ``export_destinations`` must be registered exporters.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from narumi.errors import ConfigurationConflictError, InvalidArgumentError
from narumi.export import list_exporters
from narumi.models import MeetingConfig
from narumi.profiles import UNSET, Profile

from narumi_server.handlers.common import (
    check_cache_epoch_monotonic,
    config_from_mapping,
    validated_config,
)

if TYPE_CHECKING:
    from narumi_server.context import ServerContext

PROFILE_KEYS: tuple[str, ...] = ("config", "scope", "engagement", "export_destinations")


def _payload(profile: Profile) -> dict[str, Any]:
    return profile.model_dump(mode="json")


def list_profiles(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "profiles": [_payload(profile) for profile in ctx.profiles.list()],
        "default": ctx.profiles.default_name,
    }


def get_profile(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    return {"profile": _payload(ctx.profiles.get(args["name"]))}


def set_profile(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    name = args["name"]
    expected_supplied = "expected_config" in args
    expected = MeetingConfig.model_validate(args["expected_config"]) if expected_supplied else None
    destinations = args.get("export_destinations")
    if destinations is not None:
        registered = sorted(str(item["name"]) for item in list_exporters())
        unknown = sorted(set(destinations) - set(registered))
        if unknown:
            raise InvalidArgumentError(
                f"unknown export destination(s): {', '.join(unknown)};"
                f" registered: {', '.join(registered)}",
                details={"unknown": unknown, "registered": registered},
            )
    make_default = bool(args.get("make_default", False))
    # Even legacy calls without expected_config use an internal compare-and-set. If a
    # concurrent writer wins, rebuild from that snapshot before checking the epoch again.
    # This preserves merge-on-latest behavior without allowing a stale epoch to be published.
    for attempt in range(3):
        if expected_supplied:
            base = expected
        else:
            current = ctx.profiles.peek(name)
            base = current.config if current is not None else MeetingConfig()
        assert base is not None
        config = config_from_mapping(base, args.get("config"))
        check_cache_epoch_monotonic(base, config)
        with validated_config(ctx, config):
            try:
                profile = ctx.profiles.set(
                    name,
                    config=config,
                    expected_config=base,
                    scope=args.get("scope", UNSET),
                    engagement=args.get("engagement", UNSET),
                    export_destinations=destinations,
                    make_default=make_default,
                )
            except ConfigurationConflictError as exc:
                save_outcome_unknown = exc.details.get("reason") == "profile_save_outcome_unknown"
                if expected_supplied or save_outcome_unknown or attempt == 2:
                    raise
                continue
        break
    ctx.catalog.audit(
        ctx.actor,
        "set_profile",
        {
            "name": name,
            "updated": sorted(key for key in PROFILE_KEYS if key in args),
            "make_default": make_default,
        },
    )
    return {"profile": _payload(profile)}


def delete_profile(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    ctx.profiles.delete(args["name"])
    ctx.catalog.audit(ctx.actor, "delete_profile", {"name": args["name"]})
    return {"name": args["name"], "deleted": True}
