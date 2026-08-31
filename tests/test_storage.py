from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from agora.core.storage import (
    ArtifactStorageError,
    FilesystemArtifactStorage,
    InvalidStorageKey,
    StorageCleanupRequired,
    StorageCollision,
    StorageIntegrityError,
    StorageKey,
    StorageWriteCommitted,
    UnsafeStorageEntry,
)


def fixed_key(character: str = "a") -> StorageKey:
    token = character * 64
    return StorageKey(f"v1/{token[:2]}/{token[2:4]}/{token}")


def pending_path(root: Path, key: StorageKey) -> Path:
    parts = key.value.split("/")
    return root.joinpath(*parts[:-1], f".{parts[-1]}.pending")


def final_path(root: Path, key: StorageKey) -> Path:
    return root.joinpath(*key.value.split("/"))


def test_storage_key_generation_is_opaque_and_canonical(tmp_path: Path) -> None:
    storage = FilesystemArtifactStorage(tmp_path / "private")

    first = storage.generate_key()
    second = storage.generate_key()

    assert first != second
    version, first_shard, second_shard, token = first.value.split("/")
    assert version == "v1"
    assert first_shard == token[:2]
    assert second_shard == token[2:4]
    assert StorageKey(first.value) == first


@pytest.mark.parametrize(
    "value",
    [
        "",
        "../secret",
        "/absolute",
        "C:\\absolute",
        "C:relative",
        "\\\\server\\share",
        "\\\\?\\C:\\extended",
        "v1/../aa/" + "a" * 64,
        "v1\\aa\\aa\\" + "a" * 64,
        "v1/%2e/%2f/" + "a" * 64,
        "v1/aa/aa/" + "A" * 64,
        "v1/ff/ff/" + "a" * 64,
        "v1/aa/aa/" + "a" * 63,
        "v1/aa/aa/" + "a" * 64 + "\n",
        "v1/aa/aa/" + "a" * 63 + "é",
        "v1/aa/aa/" + "a" * 32 + ":stream",
        "v1/aa/aa/" + "a" * 32 + "\x00rest",
    ],
)
def test_storage_key_rejects_traversal_and_noncanonical_forms(value: str) -> None:
    with pytest.raises(InvalidStorageKey):
        StorageKey(value)


def test_storage_requires_an_absolute_root() -> None:
    with pytest.raises(ArtifactStorageError, match="absolute"):
        FilesystemArtifactStorage(Path("relative"))


def test_storage_rejects_a_file_as_its_root(tmp_path: Path) -> None:
    root = tmp_path / "not-a-directory"
    root.write_bytes(b"file")

    with pytest.raises(ArtifactStorageError, match="initialize"):
        FilesystemArtifactStorage(root)


def test_streaming_write_open_and_idempotent_delete(tmp_path: Path) -> None:
    root = tmp_path / "private"
    storage = FilesystemArtifactStorage(root)
    key = fixed_key()
    content = b"<html>" + b"payload" * 1000 + b"</html>"
    digest = hashlib.sha256(content).hexdigest()

    receipt = storage.write(
        key,
        (content[index : index + 31] for index in range(0, len(content), 31)),
        expected_size=len(content),
        expected_sha256=digest,
    )

    assert receipt.key == key
    assert receipt.byte_size == len(content)
    assert receipt.sha256 == digest
    assert final_path(root, key).read_bytes() == content
    assert not pending_path(root, key).exists()
    with storage.open(key) as stream:
        assert stream.read() == content

    storage.delete(key)
    storage.delete(key)
    assert not final_path(root, key).exists()


def test_empty_artifact_is_verified(tmp_path: Path) -> None:
    storage = FilesystemArtifactStorage(tmp_path / "private")
    receipt = storage.write(
        fixed_key(),
        (),
        expected_size=0,
        expected_sha256=hashlib.sha256(b"").hexdigest(),
    )

    assert receipt.byte_size == 0


@pytest.mark.parametrize(
    ("chunks", "expected_size", "expected_digest"),
    [
        ((b"abc",), -1, None),
        ((b"abc",), None, "A" * 64),
        ((b"abc", b"def"), 5, None),
        ((b"abc",), 4, None),
        ((b"abc",), 3, "0" * 64),
    ],
)
def test_failed_expectations_leave_no_file(
    tmp_path: Path,
    chunks: tuple[bytes, ...],
    expected_size: int | None,
    expected_digest: str | None,
) -> None:
    root = tmp_path / "private"
    storage = FilesystemArtifactStorage(root)
    key = fixed_key()

    with pytest.raises(StorageIntegrityError):
        storage.write(
            key,
            chunks,
            expected_size=expected_size,
            expected_sha256=expected_digest,
        )

    assert not final_path(root, key).exists()
    assert not pending_path(root, key).exists()


def test_iterator_failure_and_non_bytes_chunks_are_cleaned(tmp_path: Path) -> None:
    root = tmp_path / "private"
    storage = FilesystemArtifactStorage(root)
    first_key = fixed_key("a")
    second_key = fixed_key("b")

    def broken() -> Iterator[bytes]:
        yield b"partial"
        raise RuntimeError("stream disconnected")

    with pytest.raises(RuntimeError, match="disconnected"):
        storage.write(first_key, broken())
    invalid_chunk = cast(bytes, "not bytes")
    with pytest.raises(TypeError, match="bytes"):
        storage.write(second_key, [b"valid", invalid_chunk])

    for key in (first_key, second_key):
        assert not final_path(root, key).exists()
        assert not pending_path(root, key).exists()


@pytest.mark.parametrize("stream_fails", [False, True])
def test_descriptor_close_failures_still_clean_pending_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stream_fails: bool
) -> None:
    root = tmp_path / "private"
    storage = FilesystemArtifactStorage(root)
    key = fixed_key()
    original_open = os.open
    original_close = os.close
    pending_descriptor: int | None = None

    def track_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
    ) -> int:
        nonlocal pending_descriptor
        descriptor = original_open(path, flags, mode)
        if str(path).endswith(".pending"):
            pending_descriptor = descriptor
        return descriptor

    def delayed_close_error(descriptor: int) -> None:
        original_close(descriptor)
        if descriptor == pending_descriptor:
            raise OSError("simulated delayed close failure")

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "close", delayed_close_error)
    if stream_fails:
        monkeypatch.setattr(
            storage,
            "_stream_and_verify",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("stream failure")),
        )

    message = "close incomplete" if stream_fails else "close private pending"
    with pytest.raises(ArtifactStorageError, match=message):
        storage.write(key, (b"content",))

    assert not pending_path(root, key).exists()
    assert not final_path(root, key).exists()


def test_write_loops_over_short_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = FilesystemArtifactStorage(tmp_path / "private")
    content = b"0123456789"
    original_write = os.write

    def short_write(descriptor: int, data: bytes | memoryview) -> int:
        return original_write(descriptor, bytes(data[:2]))

    monkeypatch.setattr(os, "write", short_write)
    receipt = storage.write(fixed_key(), (content,))

    assert receipt.byte_size == len(content)


def test_zero_progress_write_is_cleaned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "private"
    storage = FilesystemArtifactStorage(root)
    key = fixed_key()
    monkeypatch.setattr(os, "write", lambda descriptor, data: 0)

    with pytest.raises(OSError, match="no progress"):
        storage.write(key, (b"content",))

    assert not pending_path(root, key).exists()


def test_readback_mismatch_is_cleaned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "private"
    storage = FilesystemArtifactStorage(root)
    key = fixed_key()
    monkeypatch.setattr(storage, "_readback", lambda descriptor: (0, "0" * 64))

    with pytest.raises(StorageIntegrityError, match="read-back"):
        storage.write(key, (b"content",))

    assert not final_path(root, key).exists()


def test_final_collision_never_overwrites_existing_bytes(tmp_path: Path) -> None:
    root = tmp_path / "private"
    storage = FilesystemArtifactStorage(root)
    key = fixed_key()
    storage.write(key, (b"first",))

    with pytest.raises(StorageCollision, match="final"):
        storage.write(key, (b"second",))

    assert final_path(root, key).read_bytes() == b"first"
    assert not pending_path(root, key).exists()


def test_pending_collision_is_preserved(tmp_path: Path) -> None:
    root = tmp_path / "private"
    storage = FilesystemArtifactStorage(root)
    key = fixed_key()
    pending = pending_path(root, key)
    pending.parent.mkdir(parents=True)
    pending.write_bytes(b"existing pending bytes")

    with pytest.raises(StorageCollision, match="pending"):
        storage.write(key, (b"new",))

    assert pending.read_bytes() == b"existing pending bytes"
    assert not final_path(root, key).exists()


def test_install_failure_cleans_pending_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "private"
    storage = FilesystemArtifactStorage(root)
    key = fixed_key()

    def fail_link(source: Path, destination: Path) -> None:
        raise OSError("filesystem does not support hard links")

    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(ArtifactStorageError, match="atomically install"):
        storage.write(key, (b"content",))

    assert not pending_path(root, key).exists()
    assert not final_path(root, key).exists()


def test_post_install_failure_returns_a_committed_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "private"
    storage = FilesystemArtifactStorage(root)
    key = fixed_key()
    pending = pending_path(root, key)
    original_unlink = Path.unlink

    def fail_pending_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == pending:
            raise OSError("simulated cleanup failure")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_pending_unlink)
    with pytest.raises(StorageWriteCommitted) as raised:
        storage.write(key, (b"content",))

    assert raised.value.receipt.key == key
    assert final_path(root, key).read_bytes() == b"content"
    monkeypatch.undo()
    storage.delete(key)
    assert not pending.exists()


def test_failed_write_retains_pending_bytes_when_cleanup_is_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "private"
    storage = FilesystemArtifactStorage(root)
    key = fixed_key()
    pending = pending_path(root, key)
    original_unlink = Path.unlink

    def fail_pending_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == pending:
            raise OSError("simulated cleanup failure")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_pending_unlink)
    with pytest.raises(StorageCleanupRequired, match="clean incomplete"):
        storage.write(key, (b"content",), expected_sha256="0" * 64)

    assert pending.read_bytes() == b"content"
    assert not final_path(root, key).exists()


def test_open_missing_artifact_is_generic(tmp_path: Path) -> None:
    storage = FilesystemArtifactStorage(tmp_path / "private")

    with pytest.raises(FileNotFoundError, match="artifact does not exist"):
        with storage.open(fixed_key()):
            pass


def test_open_wraps_filesystem_errors_without_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "private"
    storage = FilesystemArtifactStorage(root)
    key = fixed_key()
    storage.write(key, (b"content",))
    original_open = os.open

    def fail_final_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
    ) -> int:
        if os.fspath(path) == os.fspath(final_path(root, key)):
            raise PermissionError("sensitive absolute path")
        return original_open(path, flags, mode)

    monkeypatch.setattr(os, "open", fail_final_open)
    with pytest.raises(ArtifactStorageError, match="open private artifact") as raised:
        with storage.open(key):
            pass
    assert str(root) not in str(raised.value)


def test_open_closes_a_descriptor_rejected_as_unsafe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FilesystemArtifactStorage(tmp_path / "private")
    key = fixed_key()
    storage.write(key, (b"content",))

    def reject(entry: os.stat_result) -> None:
        raise UnsafeStorageEntry("unsafe test entry")

    monkeypatch.setattr(storage, "_assert_regular_stat", reject)
    with pytest.raises(UnsafeStorageEntry):
        with storage.open(key):
            pass


def test_open_rejects_an_entry_changed_between_inspection_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FilesystemArtifactStorage(tmp_path / "private")
    key = fixed_key()
    storage.write(key, (b"content",))
    monkeypatch.setattr(storage, "_same_file", lambda first, second: False)

    with pytest.raises(UnsafeStorageEntry, match="changed while opening"):
        with storage.open(key):
            pass


def test_delete_with_missing_shards_returns_without_creating_them(tmp_path: Path) -> None:
    root = tmp_path / "private"
    storage = FilesystemArtifactStorage(root)

    storage.delete(fixed_key())

    assert list(root.iterdir()) == []


def test_delete_treats_directory_inspection_failure_as_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "private"
    storage = FilesystemArtifactStorage(root)
    blocked = root / "v1"
    original_lstat = Path.lstat

    def deny(path: Path) -> os.stat_result:
        if path == blocked:
            raise PermissionError("sensitive absolute path")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", deny)
    with pytest.raises(ArtifactStorageError, match="inspect") as raised:
        storage.delete(fixed_key())
    assert str(root) not in str(raised.value)


def test_delete_refuses_a_directory_at_the_final_name(tmp_path: Path) -> None:
    root = tmp_path / "private"
    storage = FilesystemArtifactStorage(root)
    key = fixed_key()
    final_path(root, key).mkdir(parents=True)

    with pytest.raises(UnsafeStorageEntry):
        storage.delete(key)


def test_delete_handles_a_disappearing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "private"
    storage = FilesystemArtifactStorage(root)
    key = fixed_key()
    storage.write(key, (b"content",))
    final = final_path(root, key)
    original_unlink = Path.unlink

    def disappear(path: Path, missing_ok: bool = False) -> None:
        if path == final:
            raise FileNotFoundError
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", disappear)
    storage.delete(key)
    monkeypatch.undo()
    storage.delete(key)


def test_delete_wraps_unlink_and_durability_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "private"
    storage = FilesystemArtifactStorage(root)
    first = fixed_key("a")
    second = fixed_key("b")
    storage.write(first, (b"first",))
    storage.write(second, (b"second",))
    first_path = final_path(root, first)
    original_unlink = Path.unlink

    def deny(path: Path, missing_ok: bool = False) -> None:
        if path == first_path:
            raise PermissionError("simulated delete denial")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", deny)
    with pytest.raises(ArtifactStorageError, match="could not delete"):
        storage.delete(first)
    monkeypatch.undo()
    storage.delete(first)

    monkeypatch.setattr(
        storage,
        "_fsync_directory",
        lambda directory: (_ for _ in ()).throw(OSError("simulated fsync failure")),
    )
    with pytest.raises(ArtifactStorageError, match="durability"):
        storage.delete(second)


def test_delete_retries_the_directory_barrier_after_a_durability_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FilesystemArtifactStorage(tmp_path / "private")
    key = fixed_key()
    storage.write(key, (b"content",))
    calls = 0

    def fail_once(directory: Path) -> None:
        nonlocal calls
        del directory
        calls += 1
        if calls == 1:
            raise OSError("simulated first fsync failure")

    monkeypatch.setattr(storage, "_fsync_directory", fail_once)
    with pytest.raises(ArtifactStorageError, match="durability"):
        storage.delete(key)
    storage.delete(key)

    assert calls == 2


def test_pending_creation_and_parent_creation_errors_are_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "private"
    storage = FilesystemArtifactStorage(root)
    key = fixed_key()
    original_open = os.open

    def fail_pending_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
    ) -> int:
        if str(path).endswith(".pending"):
            raise PermissionError("sensitive pending path")
        return original_open(path, flags, mode)

    monkeypatch.setattr(os, "open", fail_pending_open)
    with pytest.raises(ArtifactStorageError, match="create private pending"):
        storage.write(key, (b"content",))
    monkeypatch.undo()

    original_mkdir = Path.mkdir

    def fail_shard_mkdir(
        path: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False
    ) -> None:
        if path.parent == root:
            raise PermissionError("sensitive shard path")
        original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", fail_shard_mkdir)
    with pytest.raises(ArtifactStorageError, match="storage directory"):
        storage.write(key, (b"content",))


def test_resolved_shard_escape_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "private"
    outside = tmp_path / "outside"
    outside.mkdir()
    storage = FilesystemArtifactStorage(root)
    original_resolve = Path.resolve

    def escape(path: Path, strict: bool = False) -> Path:
        if path == root / "v1":
            return outside
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", escape)
    with pytest.raises(UnsafeStorageEntry, match="escaped"):
        storage.write(fixed_key(), (b"content",))


def test_directory_inspection_and_pending_cleanup_errors_are_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(ArtifactStorageError, match="inspect"):
        FilesystemArtifactStorage._assert_directory(missing)
    FilesystemArtifactStorage._unlink_pending_after_failure(missing)

    pending = tmp_path / "pending"
    pending.write_bytes(b"partial")

    def deny(path: Path, missing_ok: bool = False) -> None:
        raise PermissionError("simulated cleanup denial")

    monkeypatch.setattr(Path, "unlink", deny)
    with pytest.raises(ArtifactStorageError, match="clean incomplete"):
        FilesystemArtifactStorage._unlink_pending_after_failure(pending)


def test_reparse_detection_and_posix_directory_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if marker:
        entry = cast(os.stat_result, SimpleNamespace(st_file_attributes=marker))
        assert FilesystemArtifactStorage._is_reparse(entry)

    calls: list[object] = []
    monkeypatch.setattr(os, "name", "posix")

    def fake_open(path: str | bytes | os.PathLike[str] | os.PathLike[bytes], flags: int) -> int:
        return 99

    monkeypatch.setattr(os, "open", fake_open)
    monkeypatch.setattr(os, "fsync", lambda descriptor: calls.append(("fsync", descriptor)))
    monkeypatch.setattr(os, "close", lambda descriptor: calls.append(("close", descriptor)))

    FilesystemArtifactStorage._fsync_directory(tmp_path)

    assert calls == [("fsync", 99), ("close", 99)]


def test_fresh_storage_shards_are_made_durable_in_parent_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "private"
    storage = FilesystemArtifactStorage(root)
    calls: list[Path] = []
    monkeypatch.setattr(storage, "_fsync_directory", calls.append)

    storage.write(fixed_key(), (b"content",))

    assert calls == [root, root / "v1", root / "v1" / "aa", root / "v1" / "aa" / "aa"]


def test_symlinked_key_directory_is_rejected_when_supported(tmp_path: Path) -> None:
    root = tmp_path / "private"
    outside = tmp_path / "outside"
    outside.mkdir()
    storage = FilesystemArtifactStorage(root)
    shard = root / "v1"
    try:
        shard.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")

    with pytest.raises(UnsafeStorageEntry):
        storage.write(fixed_key(), (b"content",))
    assert list(outside.iterdir()) == []


def test_final_file_symlink_is_rejected_when_supported(tmp_path: Path) -> None:
    root = tmp_path / "private"
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside bytes")
    storage = FilesystemArtifactStorage(root)
    key = fixed_key()
    final = final_path(root, key)
    final.parent.mkdir(parents=True)
    try:
        final.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")

    with pytest.raises(UnsafeStorageEntry):
        with storage.open(key):
            pass
    assert outside.read_bytes() == b"outside bytes"
