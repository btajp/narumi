"""Effect fakes shared by provider settings, authentication and preparation tests."""

from __future__ import annotations

import copy
import uuid
from concurrent.futures import Future

from narumi.contracts.loader import load_contracts
from narumi.errors import CancelledError
from narumi.providers.runtime import RuntimeInspector

INSTANCE_ONE = "00000000-0000-4000-8000-000000000001"
INSTANCE_TWO = "00000000-0000-4000-8000-000000000002"


class MemorySecretStore:
    def __init__(self):
        self.values = {}
        self.calls = []
        self.fail_set = False
        self.fail_delete = False

    def get(self, account):
        self.calls.append(("get", account))
        return self.values.get(account)

    def set(self, account, value):
        self.calls.append(("set", account))
        if self.fail_set and not account.endswith("request-hmac"):
            raise RuntimeError("unexpected upstream secret: fixture-key")
        self.values[account] = value

    def delete(self, account):
        self.calls.append(("delete", account))
        if self.fail_delete:
            raise RuntimeError("unexpected upstream secret: fixture-key")
        self.values.pop(account, None)


class FakeMetadata:
    def __init__(self, models=None):
        self.calls = []
        self.error = None
        self.models = (
            models
            if models is not None
            else load_contracts()["list_provider_models"].output_examples[0]["models"]
        )

    def fetch(self, provider_id, endpoint, api_key):
        self.calls.append((provider_id, endpoint, api_key))
        if self.error is not None:
            raise self.error
        return copy.deepcopy(self.models)


class ManualExecutor:
    def __init__(self):
        self.pending = []

    def submit(self, function, *args):
        future = Future()
        self.pending.append((future, function, args))
        return future

    def run_next(self):
        future, function, args = self.pending.pop(0)
        result = function(*args)
        future.set_result(result)
        return result


class FakeProgress:
    def __init__(self, job_id, *, cancelled=False):
        self.job_id = job_id
        self.cancelled = cancelled
        self.stages = []

    def __call__(self, stage, fraction):
        if self.cancelled:
            raise CancelledError("fixture cancellation")
        self.stages.append((stage, fraction))


class JobQueue:
    def __init__(self):
        self.calls = []

    def __call__(self, function):
        job_id = "job-" + uuid.uuid4().hex
        self.calls.append((job_id, function))
        return job_id

    def run(self, index=0, *, cancelled=False):
        job_id, function = self.calls[index]
        return function(FakeProgress(job_id, cancelled=cancelled))


class FakeRuntimeInspector(RuntimeInspector):
    def __init__(self):
        self.calls = []
        self.error = None
        self.version = "1.0.0"

    def resource(self, provider_id):
        return {
            "resource_id": {
                "anthropic-api": "anthropic-client",
                "ollama": "local-ollama",
                "claude-agent-sdk": "claude-sdk",
            }[provider_id],
            "display_name": "Installed runtime fixture",
            "kind": "runtime",
            "version": self.version,
            "source": "installed",
            "download_host": None,
            "sha256": "a" * 64,
            "license": "Fixture license",
        }

    def prepare(self, root, provider_id, resource, progress):
        self.calls.append((provider_id, resource))
        progress("fixture_runtime_inspection", 0.5)
        if self.error is not None:
            raise self.error


def create_connection(
    service, *, provider_id="anthropic-api", key="fixture-key", request_id="create"
):
    args = {
        "provider_id": provider_id,
        "display_name": "Fixture connection",
        "auth_method": "none" if provider_id == "ollama" else "api_key",
        "request_id": "provider-" + request_id,
    }
    if provider_id != "ollama" and key is not None:
        args["api_key"] = key
    return service.set_connection(args)["connection"]
