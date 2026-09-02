"""Build one request-scoped, content-bound Claude execution snapshot."""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib.metadata
import os
import re
import stat
import zipfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from narumi.providers._claude_sources import (
    ADAPTER_SOURCE_PATHS,
    RESOURCE_SHA256_FIELD,
)
from narumi.providers._claude_sources import (
    claude_resource_sha256 as _resource_sha256,
)
from narumi.providers.claude.runtime import (
    CLI_VERSION,
    MAX_CLI_BYTES,
    PACKAGE_NAME,
    REQUIRED_CLI_CAPABILITIES,
    SDK_VERSION,
    runtime_fingerprint,
)

MAX_ADAPTER_SOURCE_BYTES = 512 * 1024
MAX_SDK_SOURCE_BYTES = 2 * 1024 * 1024
MAX_SDK_SOURCE_TOTAL_BYTES = 16 * 1024 * 1024
MAX_EXECUTION_ARCHIVE_BYTES = 32 * 1024 * 1024
_ARCHIVE_ENV = "NARUMI_CLAUDE_SNAPSHOT_ARCHIVE"
_CLI_ENV = "NARUMI_CLAUDE_SNAPSHOT_CLI"
_DEPENDENCIES_ENV = "NARUMI_CLAUDE_DEPENDENCY_ROOT"
_PACKAGE_ROOT = Path(__file__).resolve().parents[2]
_IMMUTABLE_FLAG = getattr(stat, "UF_IMMUTABLE", 0)
_VERSION_PATTERN = re.compile(rb'^__cli_version__ = "([0-9]+\.[0-9]+\.[0-9]+)"\s*$', re.MULTILINE)

_CHILD_ADAPTER_PATHS = (
    "providers/claude/protocol.py",
    "providers/claude/runtime.py",
    "providers/claude/worker.py",
)
_EMPTY_PACKAGE_MARKERS = (
    "narumi/__init__.py",
    "narumi/providers/__init__.py",
    "narumi/providers/claude/__init__.py",
)


@dataclass
class ExecutionSnapshot:
    """Open archive FD plus a named CLI copy valid for one worker invocation."""

    archive_descriptor: int
    cli_path: Path
    dependency_root: Path
    runtime_evidence: dict[str, str]

    @property
    def archive_path(self) -> str:
        return f"/dev/fd/{self.archive_descriptor}"

    @property
    def inherited_descriptors(self) -> tuple[int, ...]:
        return (self.archive_descriptor,)

    def environment(self) -> dict[str, str]:
        return {
            _ARCHIVE_ENV: self.archive_path,
            _CLI_ENV: str(self.cli_path),
            _DEPENDENCIES_ENV: str(self.dependency_root),
        }

    def verify_after_execution(self) -> None:
        digest, _ = _read_regular_file(
            self.cli_path,
            MAX_CLI_BYTES,
            allow_empty=False,
            required_markers=_cli_markers(),
        )
        if digest != self.runtime_evidence["cli_sha256"]:
            raise RuntimeError("Claude CLI snapshot changed during execution")

    def close(self) -> None:
        os.close(self.archive_descriptor)


@dataclass
class ExecutionImage:
    """Server-lifetime immutable SDK/source image, keyed by resource SHA."""

    archive_descriptor: int
    archive_sha256: str
    cli_descriptor: int
    cli_path: Path
    dependency_root: Path
    runtime_evidence: dict[str, str]

    def materialize(self, workspace: Path) -> ExecutionSnapshot:
        if not workspace.is_dir():
            raise RuntimeError("Claude run workspace is unavailable")
        snapshot_directory = workspace / "snapshot"
        _create_private_directory(snapshot_directory)
        try:
            self.verify_cli()
            archive_descriptor = _copy_archive(
                self.archive_descriptor,
                snapshot_directory / "runtime.zip",
                self.archive_sha256,
            )
            return ExecutionSnapshot(
                archive_descriptor,
                self.cli_path,
                self.dependency_root,
                dict(self.runtime_evidence),
            )
        except BaseException:
            raise

    def verify_cli(self) -> None:
        before = os.fstat(self.cli_descriptor)
        digest, size = _descriptor_digest(
            self.cli_descriptor,
            MAX_CLI_BYTES,
            required_markers=_cli_markers(),
        )
        path_info = self.cli_path.stat(follow_symlinks=False)
        if (
            digest != self.runtime_evidence["cli_sha256"]
            or size != before.st_size
            or _file_state(before) != _file_state(os.fstat(self.cli_descriptor))
            or _file_state(before) != _file_state(path_info)
        ):
            raise RuntimeError("cached Claude CLI changed")

    def close(self) -> None:
        archive_descriptor, cli_descriptor = self.archive_descriptor, self.cli_descriptor
        self.archive_descriptor = -1
        self.cli_descriptor = -1
        errors = []
        for descriptor in (archive_descriptor, cli_descriptor):
            try:
                os.close(descriptor)
            except OSError as error:
                errors.append(error)
        _unlink_cli(self.cli_path)
        if errors:
            raise errors[0]


def adapter_source_digest(
    *,
    package_root: Path = _PACKAGE_ROOT,
    relative_paths: tuple[str, ...] = ADAPTER_SOURCE_PATHS,
) -> bytes:
    """Hash bounded adapter bytes read without following any path component."""
    sources = _capture_sources(package_root, relative_paths)
    return _source_digest(sources)


def claude_resource_sha256(evidence: dict[str, str], source_digest: bytes) -> str:
    """Bind the public SDK evidence and exact Narumi adapter source set."""
    if not isinstance(source_digest, bytes) or len(source_digest) != 32:
        raise ValueError("invalid Claude adapter source digest")
    sdk_evidence = {key: value for key, value in evidence.items() if key != RESOURCE_SHA256_FIELD}
    return _resource_sha256(runtime_fingerprint(sdk_evidence), source_digest)


def create_execution_image(
    workspace: Path,
    sdk_evidence: dict[str, str],
    *,
    expected_resource_sha256: str | None,
) -> ExecutionImage:
    """Copy only verified bytes into a private snapshot and bind its resource hash."""
    snapshot_directory = workspace / "image"
    _create_private_directory(snapshot_directory)

    adapter_sources = _capture_sources(_PACKAGE_ROOT, ADAPTER_SOURCE_PATHS)
    source_digest = _source_digest(adapter_sources)
    calculated_resource = claude_resource_sha256(sdk_evidence, source_digest)
    if expected_resource_sha256 is not None and expected_resource_sha256 != calculated_resource:
        raise RuntimeError("Claude adapter snapshot does not match the selected runtime")
    runtime_evidence = dict(sdk_evidence)
    runtime_evidence[RESOURCE_SHA256_FIELD] = calculated_resource

    distribution = importlib.metadata.distribution(PACKAGE_NAME)
    if distribution.version != SDK_VERSION:
        raise RuntimeError("unsupported Claude Agent SDK version")
    record_path = _distribution_record_path(distribution)
    _, record_bytes = _read_regular_file(
        record_path, MAX_SDK_SOURCE_BYTES, allow_empty=False, capture=True
    )
    assert record_bytes is not None
    record_hashes = _record_hashes(record_bytes.decode("utf-8"))
    sdk_root = Path(distribution.locate_file("."))
    runtime_paths = tuple(
        sorted(path for path in record_hashes if path.startswith("claude_agent_sdk/"))
    )
    if not runtime_paths:
        raise RuntimeError("Claude SDK distribution contains no verifiable runtime files")

    executable_name = "claude.exe" if os.name == "nt" else "claude"
    cli_relative = f"claude_agent_sdk/_bundled/{executable_name}"
    version_relative = "claude_agent_sdk/_cli_version.py"
    if cli_relative not in record_hashes or version_relative not in record_hashes:
        raise RuntimeError("Claude SDK RECORD is incomplete")
    cli_path = snapshot_directory / executable_name
    sdk_sources: dict[str, bytes] = {}
    sdk_source_digest = hashlib.sha256()
    total_source_bytes = 0
    archive_descriptor: int | None = None
    cli_descriptor: int | None = None
    try:
        for relative in runtime_paths:
            source_path = sdk_root / PurePosixPath(relative)
            if relative == cli_relative:
                digest = _copy_regular_file(
                    source_path,
                    cli_path,
                    MAX_CLI_BYTES,
                    required_markers=_cli_markers(),
                )
                os.chmod(cli_path, 0o500, follow_symlinks=False)
            else:
                digest, content = _read_regular_file(
                    source_path,
                    MAX_SDK_SOURCE_BYTES,
                    allow_empty=True,
                    capture=True,
                )
                assert content is not None
                total_source_bytes += len(content)
                if total_source_bytes > MAX_SDK_SOURCE_TOTAL_BYTES:
                    raise RuntimeError("Claude SDK source snapshot is too large")
                sdk_sources[relative] = content
            if digest != record_hashes[relative]:
                raise RuntimeError("Claude SDK snapshot does not match RECORD")
            sdk_source_digest.update(relative.encode("ascii") + b"\0" + bytes.fromhex(digest))
        if sdk_source_digest.hexdigest() != sdk_evidence.get("sdk_source_sha256"):
            raise RuntimeError("Claude SDK source snapshot does not match runtime evidence")
        if record_hashes[cli_relative] != sdk_evidence.get("cli_sha256"):
            raise RuntimeError("Claude CLI snapshot does not match runtime evidence")
        version_bytes = sdk_sources[version_relative]
        version_match = _VERSION_PATTERN.search(version_bytes)
        if version_match is None or version_match.group(1).decode("ascii") != CLI_VERSION:
            raise RuntimeError("unsupported bundled Claude Code version")

        archive_descriptor = _create_archive(
            snapshot_directory,
            adapter_sources,
            sdk_sources,
        )
        archive_sha256, archive_size = _descriptor_digest(
            archive_descriptor,
            MAX_EXECUTION_ARCHIVE_BYTES,
            required_markers=(),
        )
        if archive_size <= 0:
            raise RuntimeError("Claude execution archive is empty")
        cli_descriptor = _open_verified_cli(cli_path, sdk_evidence["cli_sha256"])
        image = ExecutionImage(
            archive_descriptor,
            archive_sha256,
            cli_descriptor,
            cli_path,
            sdk_root,
            runtime_evidence,
        )
        return image
    except BaseException:
        for descriptor in (archive_descriptor, cli_descriptor):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        _unlink_cli(cli_path)
        raise


def _create_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700)
    info = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise RuntimeError("untrusted Claude execution snapshot directory")


def _unlink_cli(path: Path) -> None:
    try:
        if _IMMUTABLE_FLAG:
            os.chflags(path, 0, follow_symlinks=False)
        path.unlink()
    except FileNotFoundError:
        pass


def _capture_sources(root: Path, relative_paths: tuple[str, ...]) -> dict[str, bytes]:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
        file_flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        file_flags |= os.O_NONBLOCK
    captured: dict[str, bytes] = {}
    with ExitStack() as descriptors:
        anchor = os.open(root.anchor, directory_flags)
        descriptors.callback(os.close, anchor)
        directories: list[tuple[int, str, int, tuple[int, int, int]]] = []

        def open_child(parent: int, name: str) -> int:
            descriptor = os.open(name, directory_flags, dir_fd=parent)
            descriptors.callback(os.close, descriptor)
            identity = _directory_identity(os.fstat(descriptor))
            directories.append((parent, name, descriptor, identity))
            return descriptor

        package = anchor
        for part in root.parts[1:]:
            package = open_child(package, part)
        opened = {(): package}
        files: list[tuple[int, str, tuple[int, ...]]] = []
        for relative in relative_paths:
            _validate_relative_path(relative)
            parts = relative.split("/")
            directory = package
            for index, part in enumerate(parts[:-1]):
                prefix = tuple(parts[: index + 1])
                if prefix not in opened:
                    opened[prefix] = open_child(directory, part)
                directory = opened[prefix]
            descriptor = os.open(parts[-1], file_flags, dir_fd=directory)
            try:
                before = _trusted_regular(
                    os.fstat(descriptor), MAX_ADAPTER_SOURCE_BYTES, allow_empty=True
                )
                files.append((directory, parts[-1], _file_state(before)))
                _, content = _read_descriptor(
                    descriptor,
                    MAX_ADAPTER_SOURCE_BYTES,
                    before=before,
                    required_markers=(),
                    capture=True,
                )
                assert content is not None
                captured[relative] = content
            finally:
                os.close(descriptor)
        for directory, name, state in files:
            if state != _file_state(os.stat(name, dir_fd=directory, follow_symlinks=False)):
                raise RuntimeError("Claude adapter source changed during snapshot")
        for parent, name, descriptor, identity in directories:
            if identity != _directory_identity(os.fstat(descriptor)) or identity != (
                _directory_identity(os.stat(name, dir_fd=parent, follow_symlinks=False))
            ):
                raise RuntimeError("Claude adapter source directory changed during snapshot")
    return captured


def _source_digest(sources: dict[str, bytes]) -> bytes:
    digest = hashlib.sha256()
    for relative in sources:
        digest.update(relative.encode("ascii") + b"\0" + hashlib.sha256(sources[relative]).digest())
    return digest.digest()


def _create_archive(
    directory: Path,
    adapter_sources: dict[str, bytes],
    sdk_sources: dict[str, bytes],
) -> int:
    path = directory / "runtime.zip"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o400)
    try:
        with os.fdopen(os.dup(descriptor), "wb") as stream:
            with zipfile.ZipFile(stream, mode="w", compression=zipfile.ZIP_STORED) as archive:
                for marker in _EMPTY_PACKAGE_MARKERS:
                    archive.writestr(marker, b"")
                for relative in _CHILD_ADAPTER_PATHS:
                    archive.writestr(f"narumi/{relative}", adapter_sources[relative])
                for relative, content in sorted(sdk_sources.items()):
                    archive.writestr(relative, content)
            stream.flush()
            os.fsync(stream.fileno())
        read_flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            read_flags |= os.O_NOFOLLOW
        read_descriptor = os.open(path, read_flags)
        write_info = os.fstat(descriptor)
        read_info = os.fstat(read_descriptor)
        if (
            not stat.S_ISREG(read_info.st_mode)
            or write_info.st_dev != read_info.st_dev
            or write_info.st_ino != read_info.st_ino
            or read_info.st_uid != os.geteuid()
            or stat.S_IMODE(read_info.st_mode) != 0o400
        ):
            os.close(read_descriptor)
            raise RuntimeError("untrusted Claude execution archive")
        path.unlink()
        return read_descriptor
    finally:
        os.close(descriptor)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _open_verified_cli(path: Path, expected_digest: str) -> int:
    if _IMMUTABLE_FLAG:
        os.chflags(path, _IMMUTABLE_FLAG, follow_symlinks=False)
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        _trusted_regular(before, MAX_CLI_BYTES, allow_empty=False)
        digest, size = _descriptor_digest(
            descriptor,
            MAX_CLI_BYTES,
            required_markers=_cli_markers(),
        )
        if (
            digest != expected_digest
            or size != before.st_size
            or (_IMMUTABLE_FLAG and not before.st_flags & _IMMUTABLE_FLAG)
            or _file_state(before) != _file_state(os.fstat(descriptor))
            or _file_state(before) != _file_state(path.stat(follow_symlinks=False))
        ):
            raise RuntimeError("Claude CLI snapshot changed during creation")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _copy_archive(source_descriptor: int, path: Path, expected_digest: str) -> int:
    source_before = _trusted_regular(
        os.fstat(source_descriptor), MAX_EXECUTION_ARCHIVE_BYTES, allow_empty=False
    )
    write_flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC
    read_flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        write_flags |= os.O_NOFOLLOW
        read_flags |= os.O_NOFOLLOW
    writer = os.open(path, write_flags, 0o400)
    reader: int | None = None
    try:
        digest = hashlib.sha256()
        consumed = 0
        while block := os.pread(
            source_descriptor,
            min(1024 * 1024, MAX_EXECUTION_ARCHIVE_BYTES - consumed + 1),
            consumed,
        ):
            consumed += len(block)
            if consumed > MAX_EXECUTION_ARCHIVE_BYTES:
                raise RuntimeError("Claude execution archive is too large")
            digest.update(block)
            _write_all(writer, block)
        os.fsync(writer)
        target_info = os.fstat(writer)
        if (
            consumed != source_before.st_size
            or digest.hexdigest() != expected_digest
            or _file_state(source_before) != _file_state(os.fstat(source_descriptor))
            or target_info.st_size != consumed
            or target_info.st_uid != os.geteuid()
            or stat.S_IMODE(target_info.st_mode) != 0o400
        ):
            raise RuntimeError("Claude execution archive copy is untrusted")
        reader = os.open(path, read_flags)
        reader_info = os.fstat(reader)
        if (
            reader_info.st_dev != target_info.st_dev
            or reader_info.st_ino != target_info.st_ino
            or _file_state(reader_info) != _file_state(path.stat(follow_symlinks=False))
        ):
            raise RuntimeError("Claude execution archive changed during materialization")
        path.unlink()
        result, reader = reader, None
        return result
    finally:
        os.close(writer)
        if reader is not None:
            os.close(reader)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _descriptor_digest(
    descriptor: int,
    limit: int,
    *,
    required_markers: tuple[bytes, ...],
) -> tuple[str, int]:
    digest = hashlib.sha256()
    observed: set[bytes] = set()
    overlap = max((len(marker) for marker in required_markers), default=1) - 1
    tail = b""
    consumed = 0
    while block := os.pread(descriptor, min(1024 * 1024, limit - consumed + 1), consumed):
        consumed += len(block)
        if consumed > limit:
            raise RuntimeError("Claude runtime file is too large")
        digest.update(block)
        sample = tail + block
        observed.update(marker for marker in required_markers if marker in sample)
        tail = sample[-overlap:] if overlap else b""
    if observed != set(required_markers):
        raise RuntimeError("Claude CLI lacks required isolation capabilities")
    return digest.hexdigest(), consumed


def _read_descriptor(
    descriptor: int,
    limit: int,
    *,
    before: os.stat_result,
    required_markers: tuple[bytes, ...],
    capture: bool,
) -> tuple[str, bytes | None]:
    digest = hashlib.sha256()
    content = bytearray() if capture else None
    observed: set[bytes] = set()
    overlap = max((len(marker) for marker in required_markers), default=1) - 1
    tail = b""
    consumed = 0
    while block := os.read(descriptor, min(1024 * 1024, limit - consumed + 1)):
        consumed += len(block)
        if consumed > limit:
            raise RuntimeError("Claude runtime file is too large")
        digest.update(block)
        if content is not None:
            content.extend(block)
        sample = tail + block
        observed.update(marker for marker in required_markers if marker in sample)
        tail = sample[-overlap:] if overlap else b""
    if consumed != before.st_size or _file_state(before) != _file_state(os.fstat(descriptor)):
        raise RuntimeError("Claude runtime changed during snapshot")
    if observed != set(required_markers):
        raise RuntimeError("Claude CLI lacks required isolation capabilities")
    return digest.hexdigest(), bytes(content) if content is not None else None


def _copy_regular_file(
    source: Path,
    target: Path,
    limit: int,
    *,
    required_markers: tuple[bytes, ...],
) -> str:
    source_flags = os.O_RDONLY | os.O_CLOEXEC
    target_flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        source_flags |= os.O_NOFOLLOW
        target_flags |= os.O_NOFOLLOW
    source_descriptor = os.open(source, source_flags)
    target_descriptor = os.open(target, target_flags, 0o500)
    try:
        before = _trusted_regular(os.fstat(source_descriptor), limit, allow_empty=False)
        digest = hashlib.sha256()
        observed: set[bytes] = set()
        overlap = max(len(marker) for marker in required_markers) - 1
        tail = b""
        consumed = 0
        while block := os.read(source_descriptor, min(1024 * 1024, limit - consumed + 1)):
            consumed += len(block)
            if consumed > limit:
                raise RuntimeError("Claude runtime file is too large")
            digest.update(block)
            sample = tail + block
            observed.update(marker for marker in required_markers if marker in sample)
            tail = sample[-overlap:] if overlap else b""
            _write_all(target_descriptor, block)
        after = os.fstat(source_descriptor)
        if consumed != before.st_size or _file_state(before) != _file_state(after):
            raise RuntimeError("Claude runtime changed during snapshot")
        if observed != set(required_markers):
            raise RuntimeError("Claude CLI lacks required isolation capabilities")
        os.fsync(target_descriptor)
        target_info = os.fstat(target_descriptor)
        if target_info.st_size != consumed or target_info.st_uid != os.geteuid():
            raise RuntimeError("Claude CLI snapshot is incomplete")
        return digest.hexdigest()
    finally:
        os.close(source_descriptor)
        os.close(target_descriptor)


def _read_regular_file(
    path: Path,
    limit: int,
    *,
    allow_empty: bool,
    required_markers: tuple[bytes, ...] = (),
    capture: bool = False,
) -> tuple[str, bytes | None]:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        before = _trusted_regular(os.fstat(descriptor), limit, allow_empty=allow_empty)
        digest, content = _read_descriptor(
            descriptor,
            limit,
            before=before,
            required_markers=required_markers,
            capture=capture,
        )
        path_info = path.stat(follow_symlinks=False)
        if _file_state(before) != _file_state(path_info):
            raise RuntimeError("Claude runtime changed during snapshot")
        return digest, content
    finally:
        os.close(descriptor)


def _trusted_regular(info: os.stat_result, limit: int, *, allow_empty: bool) -> os.stat_result:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid not in {0, os.geteuid()}
        or info.st_mode & 0o022
        or not (0 if allow_empty else 1) <= info.st_size <= limit
    ):
        raise RuntimeError("untrusted Claude runtime file")
    return info


def _file_state(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        getattr(info, "st_flags", 0),
    )


def _directory_identity(info: os.stat_result) -> tuple[int, int, int]:
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("Claude runtime source path is not a directory")
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _distribution_record_path(distribution: importlib.metadata.Distribution) -> Path:
    candidates = [
        item
        for item in distribution.files or ()
        if item.name == "RECORD" and item.parent.name.endswith(".dist-info")
    ]
    if len(candidates) != 1:
        raise RuntimeError("Claude SDK RECORD is unavailable")
    return Path(distribution.locate_file(candidates[0]))


def _record_hashes(record: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in csv.reader(record.splitlines()):
        if len(row) != 3 or not row[1].startswith("sha256="):
            continue
        relative = row[0]
        if not relative.startswith("claude_agent_sdk/"):
            continue
        _validate_relative_path(relative)
        encoded = row[1].removeprefix("sha256=")
        try:
            digest = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).hex()
        except (ValueError, TypeError):
            continue
        if len(digest) != 64 or relative in result:
            raise RuntimeError("Claude SDK RECORD is invalid")
        result[relative] = digest
    return result


def _validate_relative_path(value: str) -> None:
    try:
        path = PurePosixPath(value)
        encoded = value.encode("ascii")
    except (UnicodeEncodeError, ValueError):
        raise RuntimeError("Claude snapshot path is invalid") from None
    if (
        not encoded
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise RuntimeError("Claude snapshot path is invalid")


def _write_all(descriptor: int, value: bytes) -> None:
    view = memoryview(value)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise RuntimeError("Claude snapshot write failed")
        view = view[written:]


def _cli_markers() -> tuple[bytes, ...]:
    return tuple(item.encode("ascii") for item in REQUIRED_CLI_CAPABILITIES)


def worker_snapshot_environment_names() -> tuple[str, str, str]:
    """Return private env names without exporting their values in public evidence."""
    return _ARCHIVE_ENV, _CLI_ENV, _DEPENDENCIES_ENV
