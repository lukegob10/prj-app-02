"""Private, root-confined filesystem storage for immutable artifact bytes."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

_KEY_RE = re.compile(r"v1/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}", flags=re.ASCII)
_DIGEST_RE = re.compile(r"[0-9a-f]{64}", flags=re.ASCII)
_READ_SIZE = 1024 * 1024


class ArtifactStorageError(RuntimeError):
    """Base class for sanitized storage-boundary failures."""


class InvalidStorageKey(ArtifactStorageError):
    """Raised before an invalid opaque key reaches a filesystem API."""


class StorageCollision(ArtifactStorageError):
    """Raised when an immutable final or pending name already exists."""


class StorageIntegrityError(ArtifactStorageError):
    """Raised when streamed bytes fail size or digest verification."""


class UnsafeStorageEntry(ArtifactStorageError):
    """Raised for a symlink, reparse point, or unexpected entry type."""


class StorageWriteCommitted(ArtifactStorageError):
    """Signals that final bytes exist and must be retained or explicitly cleaned."""

    def __init__(self, receipt: StoredArtifact) -> None:
        super().__init__("artifact bytes committed but finalization did not complete")
        self.receipt = receipt


@dataclass(frozen=True, slots=True)
class StorageKey:
    """An opaque adapter-owned key with one canonical ASCII grammar."""

    value: str

    def __post_init__(self) -> None:
        if _KEY_RE.fullmatch(self.value) is None:
            raise InvalidStorageKey("storage key is not canonical")
        _, first_shard, second_shard, token = self.value.split("/")
        if first_shard != token[:2] or second_shard != token[2:4]:
            raise InvalidStorageKey("storage key shards are not canonical")

    @classmethod
    def generate(cls) -> StorageKey:
        token = secrets.token_hex(32)
        return cls(f"v1/{token[:2]}/{token[2:4]}/{token}")


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    """Verified durable-write receipt without a filesystem path or URL."""

    key: StorageKey
    byte_size: int
    sha256: str


class ArtifactStorage(Protocol):
    """Object-storage-compatible operations required by domain services."""

    def generate_key(self) -> StorageKey: ...

    def write(
        self,
        key: StorageKey,
        chunks: Iterable[bytes],
        *,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> StoredArtifact: ...

    @contextmanager
    def open(self, key: StorageKey) -> Iterator[BinaryIO]: ...

    def delete(self, key: StorageKey) -> None: ...


class FilesystemArtifactStorage:
    """No-clobber, streaming storage rooted at one private directory."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute():
            raise ArtifactStorageError("artifact storage root must be absolute")
        configured_root = Path(os.path.abspath(root))
        try:
            configured_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as error:
            raise ArtifactStorageError("could not initialize private artifact storage") from error
        self._assert_directory(configured_root)
        self._root = configured_root.resolve(strict=True)

    def generate_key(self) -> StorageKey:
        return StorageKey.generate()

    def write(
        self,
        key: StorageKey,
        chunks: Iterable[bytes],
        *,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> StoredArtifact:
        self._validate_expectations(expected_size, expected_sha256)
        parent, final_path, pending_path = self._paths(key, create_parent=True)
        assert final_path is not None
        assert pending_path is not None
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(pending_path, flags, 0o600)
        except FileExistsError as error:
            raise StorageCollision("pending storage key already exists") from error
        except OSError as error:
            raise ArtifactStorageError("could not create private pending artifact") from error

        try:
            byte_size, digest = self._stream_and_verify(
                descriptor,
                chunks,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
            )
        except BaseException:
            try:
                os.close(descriptor)
            except OSError as error:
                self._unlink_pending_after_failure(pending_path)
                raise ArtifactStorageError("could not close incomplete private artifact") from error
            self._unlink_pending_after_failure(pending_path)
            raise
        try:
            os.close(descriptor)
        except OSError as error:
            self._unlink_pending_after_failure(pending_path)
            raise ArtifactStorageError("could not close private pending artifact") from error

        receipt = StoredArtifact(key=key, byte_size=byte_size, sha256=digest)
        try:
            os.link(pending_path, final_path)
        except FileExistsError as error:
            self._unlink_pending_after_failure(pending_path)
            raise StorageCollision("final storage key already exists") from error
        except OSError as error:
            self._unlink_pending_after_failure(pending_path)
            raise ArtifactStorageError("could not atomically install private artifact") from error

        try:
            pending_path.unlink()
            self._fsync_directory(parent)
        except OSError as error:
            raise StorageWriteCommitted(receipt) from error
        return receipt

    @contextmanager
    def open(self, key: StorageKey) -> Iterator[BinaryIO]:
        _, final_path, _ = self._paths(key, create_parent=False)
        if final_path is None:
            raise FileNotFoundError("artifact does not exist")
        try:
            path_entry = final_path.lstat()
        except FileNotFoundError:
            raise FileNotFoundError("artifact does not exist") from None
        except OSError as error:
            raise ArtifactStorageError("could not inspect private artifact") from error
        self._assert_regular_stat(path_entry)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(final_path, flags)
        except OSError as error:
            if isinstance(error, FileNotFoundError):
                raise FileNotFoundError("artifact does not exist") from None
            raise ArtifactStorageError("could not open private artifact") from error
        try:
            descriptor_entry = os.fstat(descriptor)
            self._assert_regular_stat(descriptor_entry)
            if not self._same_file(path_entry, descriptor_entry):
                raise UnsafeStorageEntry("artifact storage entry changed while opening")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                yield stream
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def delete(self, key: StorageKey) -> None:
        paths = self._paths(key, create_parent=False)
        parent, final_path, pending_path = paths
        if final_path is None or pending_path is None:
            return
        for path in (final_path, pending_path):
            try:
                entry = path.lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise ArtifactStorageError("could not inspect private artifact") from error
            self._assert_regular_stat(entry)
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise ArtifactStorageError("could not delete private artifact") from error
        try:
            # Also repeat this barrier when a prior attempt removed the entry but its
            # directory fsync failed. Missing is idempotent only after absence is durable.
            self._fsync_directory(parent)
        except OSError as error:
            raise ArtifactStorageError("artifact deletion durability check failed") from error

    @staticmethod
    def _validate_expectations(expected_size: int | None, expected_sha256: str | None) -> None:
        if expected_size is not None and expected_size < 0:
            raise StorageIntegrityError("expected artifact size must be nonnegative")
        if expected_sha256 is not None and _DIGEST_RE.fullmatch(expected_sha256) is None:
            raise StorageIntegrityError("expected SHA-256 must be lowercase hexadecimal")

    def _paths(
        self, key: StorageKey, *, create_parent: bool
    ) -> tuple[Path, Path | None, Path | None]:
        components = key.value.split("/")
        current = self._root
        for component in components[:-1]:
            candidate = current / component
            created = False
            if create_parent:
                try:
                    candidate.mkdir(mode=0o700)
                    created = True
                except FileExistsError:
                    pass
                except OSError as error:
                    raise ArtifactStorageError(
                        "could not create private artifact storage directory"
                    ) from error
            else:
                try:
                    candidate.lstat()
                except FileNotFoundError:
                    return current, None, None
                except OSError as error:
                    raise ArtifactStorageError(
                        "could not inspect artifact storage directory"
                    ) from error
            self._assert_directory(candidate)
            try:
                resolved = candidate.resolve(strict=True)
            except OSError as error:
                raise ArtifactStorageError(
                    "could not resolve artifact storage directory"
                ) from error
            if not resolved.is_relative_to(self._root):
                raise UnsafeStorageEntry("storage key escaped the configured root")
            if created:
                try:
                    self._fsync_directory(current)
                except OSError as error:
                    raise ArtifactStorageError(
                        "artifact storage directory durability check failed"
                    ) from error
            current = candidate
        final_path = current / components[-1]
        pending_path = current / f".{components[-1]}.pending"
        return current, final_path, pending_path

    def _stream_and_verify(
        self,
        descriptor: int,
        chunks: Iterable[bytes],
        *,
        expected_size: int | None,
        expected_sha256: str | None,
    ) -> tuple[int, str]:
        digest = hashlib.sha256()
        byte_size = 0
        for chunk in chunks:
            if not isinstance(chunk, bytes):
                raise TypeError("artifact chunks must be bytes")
            byte_size += len(chunk)
            if expected_size is not None and byte_size > expected_size:
                raise StorageIntegrityError("artifact exceeded its expected size")
            digest.update(chunk)
            self._write_all(descriptor, chunk)
        calculated = digest.hexdigest()
        if expected_size is not None and byte_size != expected_size:
            raise StorageIntegrityError("artifact size did not match its expectation")
        if expected_sha256 is not None and not hmac.compare_digest(calculated, expected_sha256):
            raise StorageIntegrityError("artifact digest did not match its expectation")

        os.fsync(descriptor)
        stored_size, stored_digest = self._readback(descriptor)
        if stored_size != byte_size or not hmac.compare_digest(stored_digest, calculated):
            raise StorageIntegrityError("stored artifact failed read-back verification")
        return byte_size, calculated

    @staticmethod
    def _write_all(descriptor: int, chunk: bytes) -> None:
        remaining = memoryview(chunk)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("artifact write made no progress")
            remaining = remaining[written:]

    @staticmethod
    def _readback(descriptor: int) -> tuple[int, str]:
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        byte_size = 0
        while chunk := os.read(descriptor, _READ_SIZE):
            byte_size += len(chunk)
            digest.update(chunk)
        return byte_size, digest.hexdigest()

    @staticmethod
    def _is_reparse(entry: os.stat_result) -> bool:
        attributes = getattr(entry, "st_file_attributes", 0)
        marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        return bool(marker and attributes & marker)

    @staticmethod
    def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
        """Compare the stable identity exposed by lstat/fstat across supported hosts."""
        return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)

    @classmethod
    def _assert_directory(cls, path: Path) -> None:
        try:
            entry = path.lstat()
        except OSError as error:
            raise ArtifactStorageError("could not inspect artifact storage directory") from error
        if not stat.S_ISDIR(entry.st_mode) or cls._is_reparse(entry):
            raise UnsafeStorageEntry("artifact storage directory is unsafe")

    @classmethod
    def _assert_regular_stat(cls, entry: os.stat_result) -> None:
        if not stat.S_ISREG(entry.st_mode) or cls._is_reparse(entry):
            raise UnsafeStorageEntry("artifact storage entry is unsafe")

    @staticmethod
    def _unlink_pending_after_failure(pending_path: Path) -> None:
        try:
            pending_path.unlink()
        except FileNotFoundError:
            return
        except OSError as error:
            raise ArtifactStorageError("could not clean incomplete artifact") from error

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        if os.name != "posix":
            return
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
