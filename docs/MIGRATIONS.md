# Database Migrations (Alembic)

Alembic owns the Postgres schema for `userServices`. Every schema change goes
through a migration file — never through `create_all()` or hand-written SQL
against the database.

---

## 1. Prerequisites

Before running any command below:

| Requirement | How to check |
| --- | --- |
| Virtualenv active | prompt shows `(venv)` |
| Correct venv | `python -c "import sys; print(sys.prefix)"` → this project's `venv` |
| Postgres running | `Get-Service postgresql-x64-18` → `Running` |
| `.env` present | needs `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD` |
| Packages installed | `pip install -r requirement.txt` |

Activate the venv:

```powershell
.\venv\Scripts\Activate.ps1
```

**Run every command from the repo root** (the directory holding `alembic.ini`).
Alembic resolves `script_location` relative to that file, and `env.py` imports
`src.…`, which only works from the root.

---

## 2. How this project is wired

```
alembic.ini                  # script_location = migration
migration/
  env.py                     # metadata + DB URL wiring
  script.py.mako             # template for new migration files
  versions/                  # the migration files themselves
    9d7c2a5904e4_create_users_table.py
```

Two project-specific details in `migration/env.py`:

```python
target_metadata = Base.metadata
config.set_main_option("sqlalchemy.url", settings.database_url)
```

**The database URL comes from `.env` via settings, not from `alembic.ini`.**
`alembic.ini` is parsed by `configparser`, where a literal `%` means string
interpolation — the percent-encoded DB password would crash or silently mangle.
This also keeps the password out of a tracked file. Leave the placeholder
`sqlalchemy.url` in `alembic.ini` alone; it is overridden at runtime.

The database it targets is `user_db` — the name is pinned in
`src/services/userServices/config.py`, not in `.env`.

---

## 3. Everyday workflow

Changing the schema is always these five steps:

```powershell
# 1. Edit the model, e.g. src/services/userServices/models/user_model.py

# 2. If you added a NEW model file, import it in migration/env.py  (see §6)

# 3. Generate the migration
alembic revision --autogenerate -m "add last_login to users"

# 4. READ the generated file in migration/versions/ and fix it if needed

# 5. Apply it
alembic upgrade head
```

Step 4 is not optional. Autogenerate is a first draft, not an answer — see §5.

---

## 4. Command reference

### Creating migrations

```powershell
# Autogenerate by diffing models against the live database
alembic revision --autogenerate -m "add last_login to users"

# Empty migration, for data backfills or anything autogenerate can't see
alembic revision -m "backfill user display names"
```

### Applying migrations (upgrade)

```powershell
alembic upgrade head        # apply everything outstanding  ← the usual one
alembic upgrade +1          # apply exactly one step
alembic upgrade 9d7c2a5904e4    # apply up to a specific revision
```

### Reverting migrations (downgrade)

```powershell
alembic downgrade -1        # undo the last migration
alembic downgrade base      # undo everything, back to an empty schema
alembic downgrade 9d7c2a5904e4  # revert down to a specific revision
```

> `downgrade base` **drops every table and destroys all data.** Safe in local
> dev, never on shared or production databases.

### Inspecting state

```powershell
alembic current             # revision the database is currently at
alembic heads               # latest revision(s) in the version files
alembic history --verbose   # full ordered list of migrations
alembic show head           # details of one revision
```

`current` reads the `alembic_version` table in the database; `heads` reads the
files on disk. When they disagree, the database has migrations left to apply.

### Marking without running

```powershell
alembic stamp head          # record as applied WITHOUT executing the SQL
```

Only for adopting Alembic onto a database whose tables already exist. It runs no
DDL — using it to escape an error just hides the mismatch.

### Previewing the SQL

```powershell
alembic upgrade head --sql          # print SQL instead of executing
alembic upgrade head --sql > up.sql # hand to a DBA for review
```

---

## 5. What autogenerate will NOT catch

It diffs models against the live schema, and it is partially blind. Always read
the generated file. It reliably misses:

- **Column renames** — emitted as a drop plus an add, which **destroys the
  data**. Rewrite by hand as `op.alter_column(..., new_column_name=...)`.
- **Table renames** — same problem; use `op.rename_table()`.
- **`server_default` changes** on existing columns.
- **CHECK constraints** and most constraint edits.
- **Data migrations** — backfills, transforms. Write those yourself in an empty
  revision.

Detected reliably: added/dropped tables, added/dropped columns, nullability,
index and unique-constraint changes, and most type changes.

---

## 6. Adding a new model — the step that bites

`env.py` builds `target_metadata` from `Base.metadata`, which is populated
**only by models that have actually been imported**. A model file that exists
but is never imported is invisible to Alembic — worse, if its table already
exists in the database, autogenerate reads that as a table it should **drop**.

So after creating `models/order_model.py`, add it to `migration/env.py`:

```python
from src.services.userServices.models import (  # noqa: F401
    address_model,
    order_model,
    password_reset_model,
    user_model,
)
```

The `# noqa: F401` is deliberate — linters flag these as unused, but the import
side effect is the entire point.

### This has already happened once

The `address` table was created by a **hand-written migration with no model**.
The next `--autogenerate` compared the database against `Base.metadata`, found
`address` in one and not the other, and emitted:

```python
op.drop_table('address')
```

Running `upgrade head` without reading the file dropped the table. It was
recovered with `alembic downgrade`, but **any rows in it would have been gone** —
`downgrade` recreates structure, not data.

Two rules come out of that:

1. **Every table needs a model**, even one nothing queries yet.
   `address_model.py` exists purely so Alembic can see the table.
2. **Read the generated migration before applying it.** A `drop_table` you did
   not ask for is the signal that a model is missing from `env.py`.

A quick way to confirm there is no drift: run `--autogenerate` and check the
generated `upgrade()` is just `pass`, then delete the file.

---

## 7. Troubleshooting

**"Target database is not up to date"**
Pending migrations exist. Run `alembic upgrade head`, then retry.

**Autogenerate produced an empty migration**
The models already match the database — often because `create_all()` built the
tables. Either the change isn't in an imported model (§6), or there is genuinely
nothing to do.

**"Can't locate revision identified by '…'"**

If the name you passed was a *filename*, that's the cause — Alembic matches on
the revision ID only, never the file:

```powershell
alembic upgrade 9d7c2a5904e4                        # correct
alembic upgrade 9d7c2a5904e4_create_users_table.py  # fails
```

The ID is the `revision = "..."` variable inside the migration file; the
filename just prefixes that ID onto a readable slug. `alembic history` lists the
IDs. Most of the time you want `alembic upgrade head` and no ID at all.

Otherwise, `alembic_version` names a revision whose file is missing — usually a
deleted or never-pulled migration. Pull the missing file, or in local dev reset
with `alembic downgrade base` and re-upgrade.

**Multiple heads**
Two branches each added a migration. Check with `alembic heads`, then:

```powershell
alembic merge -m "merge heads" <rev1> <rev2>
```

**`ModuleNotFoundError: No module named 'src'`**
You are not in the repo root. `cd` there and retry.

**`InterpolationSyntaxError` from configparser**
Something put a raw `%` into `alembic.ini`. The URL belongs in `env.py` (§2).

---

## 8. Rules

1. **Never edit a migration that has been applied elsewhere.** Add a new one.
2. **Always read autogenerated output** before applying it (§5).
3. **Test the downgrade** in dev — `alembic downgrade -1` then `upgrade head`.
   A broken downgrade is only discovered when you urgently need it.
4. **Commit the migration with the model change**, in the same commit.
5. **One logical change per migration**, with a message saying what it does.
6. **Do not use `create_all()`.** It builds tables behind Alembic's back, and
   the schema then silently diverges from the migration history.
