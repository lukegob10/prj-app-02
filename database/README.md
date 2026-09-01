# Oracle schema

[`oracle/schema.sql`](oracle/schema.sql) is the readable structural reference for Agora's
current Oracle schema. It is deliberately ordinary SQL: table definitions, keys, constraints,
and indexes.

Django migrations in [`src/agora/core/migrations/`](../src/agora/core/migrations/) remain the
executable source of truth for installation and upgrades. They also own data transitions and the
procedural triggers that enforce runtime invariants. Those operations are intentionally not copied
into the structural reference.

Apply the schema with:

```powershell
python manage.py migrate --noinput
```

Do not execute `schema.sql` as an installation or upgrade script. It does not run data migrations
or populate Django's migration recorder.

When a model changes, commit the model, migration, focused tests, and corresponding
`schema.sql` update together. Keep the SQL direct and reviewable; do not add another generator or
schema-specific toolchain.
