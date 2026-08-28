# Private artifact storage operations

AG-002 stores hostile HTML/CSV bytes under the absolute `AGORA_ARTIFACT_ROOT`. This root is a
private application data boundary, not Django media/static content, a document root, or a raw URL.
Only metadata plus a later authorization policy may select an artifact; AG-002 exposes no HTTP
artifact route.

## Addressing and writes

- Logical user filenames never enter the storage adapter. The application stores NFC display
  metadata with a separate NFKC/casefold comparison key unique within a Revision. Oracle rejects
  non-composed or non-lowercase bulk inserts and enforces comparison-key uniqueness. Because
  Oracle does not reproduce Python's full NFKC/casefold transformation, all application writes
  must continue through the shared name normalizer.
- The adapter generates 256-bit lowercase ASCII keys shaped as
  `v1/<2 hex>/<2 hex>/<64 hex>`. Shards must match the token and every key is validated before a
  filesystem operation.
- Writes use a same-directory exclusive pending file, stream while counting and hashing SHA-256,
  fsync the bytes, re-read them from the same descriptor, then install the final name with a
  no-clobber hard link. A collision never overwrites or adopts existing bytes.
- The interface returns only key, byte count, and digest. It deliberately has no public path,
  URL, update, or rename operation.

## Database coordination and recovery

A `StorageReservation` is durable cleanup ownership for bytes that are not yet an Artifact. It is
not a Revision and is never viewer-visible. Complete-revision creation reserves keys first, writes
and verifies bytes, then durably records whether the key owns verified bytes or encountered a
collision. It next uses an outermost Oracle transaction to create immutable metadata, advance
only `latest_revision`, append an audit event, and consume reservations. Publishing is unchanged.

Known rollback removes unowned files idempotently. If cleanup fails or the process terminates, the
reservation remains. Reconcile expired reservations with a bounded command:

```powershell
uv run python manage.py cleanup_artifact_reservations --limit 100
```

Compensation and the command lock each reservation before deciding ownership, so an ambiguous
database commit must settle before bytes can be removed. A collision that this attempt did not
create is durably marked and never adopted or deleted. Cleanup deletes only exact validated
final/pending names backed by a verified ownership receipt, treats only `FileNotFoundError` as
absence, and retains inspection/deletion failures for retry. A reservation whose write outcome was
never witnessed is retained for operator reconciliation rather than risking deletion of collision
bytes. Cleanup never follows arbitrary filenames or recursively deletes the root.

## Filesystem and access requirements

- Use a local or mounted filesystem qualified to support same-directory hard links and atomic
  directory entry creation. Do not silently fall back to overwrite-capable replace behavior.
- Grant the Agora service identity exclusive write control. The adapter rejects symlinks and
  Windows reparse points in managed key components and compares final-file identity before/after
  opening, but portable path APIs cannot defeat a privileged local actor racing repeated checks.
- POSIX-created directories/files request modes `0700`/`0600`; deployment umask, ACLs, mount
  options, backup tooling, and restore permissions remain operator responsibilities.
- File data is fsynced on Windows/POSIX; each new shard entry and the containing final directory
  are fsynced on POSIX. Equivalent Windows directory-entry or network-filesystem power-loss
  guarantees must be established operationally.
- The Oracle schema must retain the checked-in constraints, function-based index, and PL/SQL
  triggers; migration validation fails if any required trigger has compilation errors.

## Backup and restore boundary

Back up Oracle metadata and `AGORA_ARTIFACT_ROOT` as one recovery set. Pause revision creation
or use storage/database snapshots with a shared recovery point. After restore, run migrations and
the cleanup command, then verify every Artifact key has matching size/digest before enabling later
artifact-delivery routes. An Artifact row without bytes is an integrity incident; a reservation
without Artifact ownership is safe to reconcile.
