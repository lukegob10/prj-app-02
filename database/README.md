# Agora Oracle schema operations

This is the DBA handoff for the current Agora migration tip. It describes the supported
application-owned Oracle schema, the generated SQL evidence in
[`oracle/schema.sql`](oracle/schema.sql), and the checks that must surround a release.

## Authority and generated evidence

The Django migration graph is the only schema authoring surface. The canonical migration modules
live in `src/agora/core/migrations/`; their intentionally preserved Django app label is
`persistence` for installed-database compatibility. Those checked-in migrations, together with
the migrations for the installed Django framework apps, define tables, columns, defaults,
indexes, constraints, identity/sequence behavior, and the Oracle-specific trigger bodies. A
schema change starts with a forward migration and the model state that migration records; it must
not start with an edit to `schema.sql`.

`database/oracle/schema.sql` is deterministic, generated evidence for the final migration state,
not a second source of truth. It is produced from the current Django/Oracle backend and migration
graph by:

```text
uv run --locked python scripts/generate_oracle_schema.py
```

The generator replaces the artifact in place and preserves its generated/provenance header. Do
not hand-edit the file, add timestamps, or maintain a second SQL copy. After a migration or model
change, regenerate it, review the diff, and commit the artifact with the migration. The canonical
offline drift check is:

```text
uv run --locked python scripts/generate_oracle_schema.py --check
```

`--check` is mutation-free, deterministic, and does not load Oracle credentials or open a network
connection. Exit status `0` means the committed bytes are current and prints exactly:

```text
Oracle schema artifact is up to date: database/oracle/schema.sql
```

When the artifact is stale it exits `1` and prints a concise command to run
`uv run --locked python scripts/generate_oracle_schema.py`. `scripts/check.py` runs this same
check before the live-Oracle migration and test gates. A repository-only pass therefore validates
reproducible generation, but it does not prove that an Oracle server accepts or compiles the SQL.

The supported production installation and upgrade path is Django's migration executor, not raw
execution of this file. The SQL artifact is suitable for DBA review, controlled rehearsal, and
object-by-object comparison. It does not by itself establish the rows in the Django migration
recorder. If a DBA executes it in a disposable rehearsal, the release owner must explicitly
reconcile migration state before treating that database as an application environment; never
point a live application at a raw-applied schema on the assumption that the recorder is current.

## Schema coverage

At the current tip, the artifact covers all 19 Agora domain tables and all 6 Django framework
tables installed by the portal composition (25 tables total). Every physical name is in the
project-owned `TB_TA_AGORA_` namespace:

| Domain tables | Framework tables |
|---|---|
| `TB_TA_AGORA_USER` | `TB_TA_AGORA_AUTH_GROUP` |
| `TB_TA_AGORA_LOGIN_THROTTLE` | `TB_TA_AGORA_AUTH_GROUP_PERMISSIONS` |
| `TB_TA_AGORA_DASHBOARD` | `TB_TA_AGORA_AUTH_PERMISSION` |
| `TB_TA_AGORA_DASH_TRANSFER` | `TB_TA_AGORA_DJANGO_CONTENT_TYPE` |
| `TB_TA_AGORA_DASHBOARD_TAG` | `TB_TA_AGORA_DJANGO_MIGRATIONS` |
| `TB_TA_AGORA_DASH_FAVORITE` | `TB_TA_AGORA_DJANGO_SESSION` |
| `TB_TA_AGORA_DASH_VIEWER_STATE` |  |
| `TB_TA_AGORA_ACCESS_REQUEST` |  |
| `TB_TA_AGORA_REVISION` |  |
| `TB_TA_AGORA_ARTIFACT` |  |
| `TB_TA_AGORA_VIEWER_GRANT` |  |
| `TB_TA_AGORA_RENDER_AUTHORIZATION` |  |
| `TB_TA_AGORA_AUTHORIZED_OPEN` |  |
| `TB_TA_AGORA_OPEN_DAILY` |  |
| `TB_TA_AGORA_VIEWER_OPEN_SUM` |  |
| `TB_TA_AGORA_OPEN_SNAPSHOT` |  |
| `TB_TA_AGORA_ANALYTICS_CKPT` |  |
| `TB_TA_AGORA_AUDIT_EVENT` |  |
| `TB_TA_AGORA_STORAGE_RESERVATION` |  |

The generated SQL also carries the migration-defined indexes (including function-based Oracle
indexes), unique/check/foreign-key constraints, deferred constraints, PL/SQL guards and marker
triggers, and the backend's identity/sequence clauses for `BigAutoField` columns. Do not create
replacement sequences, constraints, or triggers by hand. Review the generated statements when a
DBA needs exact object names or ordering; the migrations remain authoritative if the two disagree.

The framework split is intentional. `auth`, `contenttypes`, and `sessions` are installed by the
portal settings and are included in the same application-owned schema. `django_migrations` is the
migration recorder, not an optional administrative table. `messages` and `staticfiles` are
installed services without database tables here, and `admin` is not installed, so there is no
`django_admin_log` table in this contract. The content composition installs only the canonical
`agora.core` domain app (whose historical Django label remains `persistence`) and points at the
same Oracle schema. No vendor, reporting, or unrelated shared-schema tables are owned by this
runbook.

The custom Oracle backend selects the project-prefixed migration recorder when it first connects.
The historical 0009/0010 rename migrations are the only supported transition from old unprefixed
names. A final schema must not contain a manually-created unprefixed recorder or duplicate target
tables; do not bypass those migrations with ad-hoc `ALTER TABLE ... RENAME` statements.

## Preconditions and privileges

Before any install or upgrade, the operator should have:

- an immutable release checkout with the locked dependencies and the generated artifact check
  passing;
- the package-managed Treasury Analytics profile selected by `ENV` and its runtime-injected
  `TA_<ENV>_PASSWORD`; Agora does not accept duplicated host, port, service, or username values;
- a dedicated non-production schema for rehearsal, or a DBA-approved owner/schema for production;
- a private, durable `AGORA_ARTIFACT_ROOT` available to the service, with Oracle metadata and the
  filesystem included in the same backup/recovery plan; and
- a maintenance window with one migration executor and no concurrent schema or application writes.

For a fresh owner schema, the account running `migrate` must have `CREATE SESSION`, tablespace
quota, and the ability to create and alter its own tables, indexes, identity/sequence objects,
and triggers. It needs `DROP`/rename authority only when the approved operation is a rollback or a
historical namespace migration. The custom migration checks use the owner's `USER_TABLES`,
`USER_SOURCE`, and `USER_ERRORS` views; no broad catalog role should be granted solely for this
runbook. If deployment separates the migration owner from the runtime account, grant the runtime
account only the approved DML/sequence/object privileges after the migration and keep DDL in the
maintenance identity. This repository does not create roles, grants, synonyms, tablespaces, or
cross-schema privileges.

For a fresh install, verify that the target has neither existing `TB_TA_AGORA_*` objects nor
legacy unprefixed/`TB_TA_*` objects that could collide with the historical rename steps. An
existing environment must be upgraded through its recorded migration state instead. Confirm that
the Oracle release, database/NLS character sets, time-zone behavior, tablespace, quota, and
`python-oracledb`/Treasury package combination are approved for the generated Oracle dialect;
this repository does not claim compatibility with another database engine or an untested Oracle
version.

## Fresh install

Use a clean, disposable schema for rehearsal. Production installation follows the same sequence
after backup and DBA approval:

1. Install the locked environment and put the release configuration in place. Do not put an
   Oracle password or connection coordinates in source control.
2. From the repository root, run the offline artifact check. It requires no Oracle secret:

   ```text
   uv run --locked python scripts/generate_oracle_schema.py --check
   ```

3. Confirm the planned graph against the target schema:

   ```text
   uv run python manage.py migrate --plan --database default
   ```

4. Apply the migration graph through Django, in one controlled process:

   ```text
   uv run python manage.py migrate --noinput --database default
   ```

5. Confirm the recorder and final schema state:

   ```text
   uv run python manage.py showmigrations --database default
   uv run python manage.py makemigrations --check --dry-run
   ```

6. Complete the live verification in this document before enabling either service or accepting
   application traffic.

Do not run `schema.sql` instead of step 4 in an application environment. If the DBA needs a raw
DDL rehearsal, run it only against a disposable schema, capture the SQL and dictionary results,
and explicitly validate how migration-recorder rows will be established. The application-supported
path is still `migrate`.

## Normal upgrade

Forward migration is the default release operation:

1. Identify the current state with `showmigrations` and review `migrate --plan` from the exact
   release checkout. Confirm that `makemigrations --check --dry-run` and the offline artifact
   `--check` both pass before touching Oracle.
2. Back up Oracle metadata and `AGORA_ARTIFACT_ROOT` at a coordinated recovery point. Pause
   revision creation or use database/filesystem snapshots that preserve the same point in time.
3. Drain or stop all Agora processes for migrations that rename tables or replace trigger bodies,
   especially the historical 0009/0010 namespace transition. Run exactly one migration executor;
   do not let old application processes race a recorder-table rename.
4. Apply the plan with `uv run python manage.py migrate --noinput --database default` and retain
   the command output, migration rows, and Oracle dictionary checks for the release record.
5. Verify trigger status/compilation, constraints, indexes, identity/sequence metadata, and the
   prefixed recorder as described below. Run focused Oracle-backed tests where the environment
   permits.
6. Restart the application processes so every process selects the final project-prefixed table
   mapping on its first connection, then perform service health and smoke checks.

If a migration fails, stop and inspect `USER_OBJECTS`, `USER_ERRORS`, and the migration recorder
before retrying. Oracle DDL can have committed part of the operation even when Django does not mark
the migration applied; a blind retry can therefore produce a different failure or mask a partial
schema. Repair through a reviewed forward migration or an approved restore, never by deleting the
recorder row or dropping an arbitrary object to force progress.

## DBA review and handoff

For each release, hand over the release commit, migration plan/output, the generated
`database/oracle/schema.sql`, its SHA-256, the exact generator/check command result, and the
Oracle environment used for live verification. The reviewer should confirm:

- the artifact header identifies the migration/model source and has no local paths, credentials,
  timestamps, or hand-edited sections;
- the 25 expected tables are present in the `TB_TA_AGORA_` namespace and no unprefixed final
  domain/framework table is being treated as current;
- every migration-defined index, unique/check/foreign-key constraint, deferred relationship,
  identity/sequence clause, and trigger appears in the artifact and in Oracle's dictionary;
- every trigger is enabled and has no compilation errors, including the append-only, lifecycle,
  ownership, access-request, artifact, render-capture, analytics, and recorder-related guards;
- application and migration principals have only the approved privileges, and no secret or
  connection coordinate was copied into the artifact or handoff package; and
- the current migration recorder rows, artifact-root backup, and release checksum are retained
  with the change record.

Useful owner-scoped checks (adapt the `LIKE` escape to the SQL client) are:

```sql
SELECT table_name
FROM user_tables
WHERE table_name LIKE 'TB\_TA\_AGORA\_%' ESCAPE '\'
ORDER BY table_name;

SELECT object_name, object_type, status
FROM user_objects
WHERE object_name LIKE 'AGORA\_%' ESCAPE '\'
ORDER BY object_type, object_name;

SELECT trigger_name, table_name, status
FROM user_triggers
WHERE table_name LIKE 'TB\_TA\_AGORA\_%' ESCAPE '\'
ORDER BY trigger_name;

SELECT name, type, line, position, text
FROM user_errors
WHERE type IN ('TRIGGER', 'PROCEDURE', 'FUNCTION', 'PACKAGE')
ORDER BY name, sequence;

SELECT table_name, constraint_name, constraint_type, status, deferrable, deferred
FROM user_constraints
WHERE table_name LIKE 'TB\_TA\_AGORA\_%' ESCAPE '\'
ORDER BY table_name, constraint_name;

SELECT table_name, column_name, generation_type
FROM user_tab_identity_cols
WHERE table_name LIKE 'TB\_TA\_AGORA\_%' ESCAPE '\'
ORDER BY table_name, column_name;
```

If the Oracle version does not expose identity metadata through `USER_TAB_IDENTITY_COLS`, use
the corresponding `USER_TAB_COLUMNS.IDENTITY_COLUMN` and sequence metadata approved by the DBA.
Compare function-based expressions through `USER_IND_EXPRESSIONS`, not only ordinary index-column
rows. An empty `USER_ERRORS` result is required; a successful `CREATE TRIGGER` statement alone is
not proof that its PL/SQL compiled.

## Rollback and reversal policy

Prefer a forward corrective migration and a compatible application rollout. A deployment rollback
normally means rolling the application code back while retaining the forward-compatible schema,
then issuing a reviewed forward fix if needed. Do not reverse a live migration merely because an
application release was rolled back.

An exact Django reversal is a destructive schema operation and requires a disposable clone,
backup, plan review, and a data preflight. For example, to rehearse reversal of the current 0017
tip to 0016:

```text
uv run python manage.py migrate persistence 0016_guard_initial_grant_revoker --plan --database default
uv run python manage.py migrate persistence 0016_guard_initial_grant_revoker --database default
```

Use the exact target migration name for the release under test; never guess a number. A full
`uv run python manage.py migrate persistence zero --plan --database default` / apply cycle is only
for an empty disposable schema, followed by a fresh forward install. Never point that operation at
production or a shared test schema. Capture the plan and verify the schema after both directions.

Reversal must stop and move forward or restore a coordinated backup when any migration contains a
data backfill with a no-op reverse, retained history that cannot be safely rewritten, or an
explicit reversal guard. In the current chain this includes:

- 0006's storage-state backfill, whose reverse is intentionally a no-op;
- 0012's publication-version/last-published-at backfill, whose reverse is intentionally a no-op;
- 0014's viewer-publication/open-capture backfill, whose reverse is intentionally a no-op;
- 0011's retained viewer-grant epochs, where accumulated regrant history can make the historical
  relationship constraint unsafe to restore; and
- 0013's ownership-transfer chain, whose reverse explicitly rejects reversal after a real transfer
  so actor history is not erased.

Back up before any migration that drops or changes constraints, triggers, indexes, columns, or
data. Do not deduplicate rows, rewrite actor fields, delete audit/history rows, or manually drop
objects to make a reverse migration pass. If exact reversal is rejected, use a forward migration,
restore the coordinated Oracle/filesystem recovery point, or obtain a separately reviewed data
repair; there is no safe generic SQL rollback for these states.

## Oracle execution caveats

- Oracle DDL has implicit commit behavior. Django's migration bookkeeping and Oracle object state
  are not one rollback unit when `RunSQL`, trigger replacement, rename, or other DDL is involved.
  A failed migration may leave committed objects behind.
- `RunPython` backfills and trigger SQL can be adjacent in one migration. Treat data effects as
  durable and preflight row counts, locks, and available rollback/backup recovery before running.
- Trigger bodies intentionally use Oracle-specific types, LOB operations, locks, function-based
  indexes, and PL/SQL. This artifact is not PostgreSQL, SQLite, or generic SQL and must not be
  adapted by search-and-replace.
- The custom backend's first connection chooses the physical recorder table from Oracle metadata.
  Stop/restart all application processes around historical table renames and after schema changes;
  do not assume a long-running process will discover a rename automatically.
- Run schema changes serially in the owner schema. Validate `USER_ERRORS` after every trigger
  install/replacement, and investigate invalid dependent objects before allowing traffic.
- `TIMESTAMP WITH TIME ZONE`, `NCLOB`/LOB behavior, identifier case, NLS settings, and identity
  semantics must be verified on the actual supported Oracle release. Offline generation cannot
  certify those server-specific details.

## Live-Oracle verification still required

The repository can prove that generation is deterministic and that the check is offline; it cannot
prove a live Oracle apply in this environment. Before a production handoff, a DBA must run a
disposable-schema rehearsal using the same Oracle release/patch, character sets, time-zone setup,
Treasury package/profile, and privilege model:

1. Apply the full framework-plus-domain migration graph from an empty schema and verify the
   recorder has every expected applied migration under the historical `persistence` app label.
2. Compare the 25 tables, columns, types, nullability/defaults, indexes/expressions,
   constraints/deferred state, identities/sequences, and triggers against `schema.sql` and the
   owner dictionary.
3. Confirm every trigger is enabled and `USER_ERRORS` is empty. Exercise representative ORM and
   direct SQL paths that rely on trigger-enforced invariants, including immutable history,
   ownership/lifecycle checks, artifact guards, render capture, and analytics checkpoint rules.
4. Exercise a representative upgrade from the deployed migration state (including 0016→0017),
   then restart both service compositions and verify the prefixed recorder/table mapping.
5. Rehearse an exact reversal only on the disposable clone, with the target migration's data
   preflight, and then run forward again. Confirm that blocked reversals remain blocked rather than
   silently rewriting history.
6. Test the coordinated Oracle metadata and artifact-root backup/restore procedure, including
   digest/size checks for every stored artifact and bounded reservation cleanup.
7. Record the Oracle version, patch, NLS/time-zone settings, schema/principal, migration output,
   object-query results, trigger compile results, backup identifiers, and any deviations from the
   checked-in contract.

Until those checks are recorded, treat the generated SQL as review evidence only. The remaining
unverified risks are Oracle-version-specific DDL acceptance, privilege/quota configuration,
trigger compilation and runtime semantics, migration partial-commit recovery, dictionary parity,
locking/performance under production data, and backup/restore behavior.
