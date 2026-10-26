# Schema Guard

A small, dependency-light MongoDB schema and integrity checker — plus a generic parity checker for comparing feature/endpoint manifests across platforms (web vs mobile, v1 vs v2, anything).

Not tied to any specific project. Point it at a connection string and a config file, and it tells you exactly what's wrong — in a format both humans and CI pipelines can act on.

## Why

MongoDB has no enforced schema. That's flexible, but it means drift happens silently: a field a deploy expects goes missing on old records, a status value sneaks in that the app never validates, a task still points at a member that got deleted months ago. Nobody notices until a user hits it.

Schema Guard catches this before it ships — by comparing a real database against a config file you write once, describing what "correct" looks like.

## What it checks

- **Missing fields** — the config expects a field, no sampled document has it
- **Unexpected fields** — data has a field the config doesn't know about
- **Wrong types** — a field's value type doesn't match what's expected
- **Invalid enum values** — a field holds something outside its allowed list
- **Always-null fields** — present on every document, but never actually filled in (soft warning)
- **Broken references** — an ID field points at another collection's document that no longer exists
- **Platform parity** (separate mode) — a feature/endpoint present on one platform's manifest but missing from another's

Every database check is **read-only**. It never writes, updates, deletes, or touches an index — confirmed by inspection of the code, not just by claim.

## Install

```bash
pip install motor python-dotenv
```

Requires Python 3.9+.

## Quick start

### 1. Bootstrap a config from a real database

```bash
python schema_guard.py init "<MONGO_URL>" <DB_NAME> -o schema_config.json
```

Scans real documents and writes a starting config — field names, inferred types, and whether each is ever null.

### 2. Review and refine it by hand

The scan can only infer so much. Go through the generated file and:

- Fix any field whose `"type"` says `"null"` — that just means nulls outnumbered real values in the sample
- Add `"enum": [...]` to fields that should only hold a fixed set of values
- Add `"references": "collection.field"` to ID fields that point at another collection

```json
{
  "collections": {
    "tasks": {
      "fields": {
        "id": { "type": "string" },
        "status": { "type": "string", "enum": ["todo", "in_progress", "done"] },
        "assignee_id": { "type": "string", "nullable": true, "references": "members.id" }
      }
    }
  }
}
```

This step needs a human who understands the data model — it can't be fully automated.

### 3. Run the check

```bash
python schema_guard.py check "<MONGO_URL>" <DB_NAME> -c schema_config.json
```

Exits `0` if clean, `1` if it found a real problem — safe to wire into CI as a deploy gate.

```
[tasks] 14 docs (sampled 14)
  ! status: value(s) not in allowed list: ['processing']
  ! assignee_id: 1 record(s) reference a missing members.id — e.g. ['a1b2c3...']

PROBLEMS FOUND — review above.
```

Useful flags:
- `--full` — scan every document instead of a sample
- `--json` — machine-readable output
- `--collections foo bar` — limit to specific collections

## Comparing two environments (e.g. preview vs prod)

Run `check` twice with the **same config file**:

```bash
python schema_guard.py check "<PREVIEW_URL>" <DB_NAME> -c schema_config.json
python schema_guard.py check "<PROD_URL>" <DB_NAME> -c schema_config.json
```

Whatever differs between the two runs *is* the drift between environments — the fields/references named in the output are exactly what to investigate.

## Platform parity checking

For comparing what two platforms actually support (useful when, e.g., a web app and a mobile app are separate codebases hitting a shared backend):

```bash
# Auto-list a FastAPI backend's real routes
python schema_guard.py extract-endpoints path/to/server.py -o web_features.json

# Build (or auto-extract) the other platform's manifest the same way,
# or hand-write it as a plain JSON list of feature/endpoint identifiers

python schema_guard.py parity --a web_features.json --label-a web \
                               --b mobile_features.json --label-b mobile --strict
```

**Important caveat:** if both platforms share the *same* backend, extracting endpoints from that shared backend will always show 100% parity — the server having a route doesn't mean every client's UI actually uses it. For that case, build each manifest from what each client's own codebase/UI actually implements, not from the shared server.

`--strict` makes it exit `1` on any mismatch, same CI-gate idea as `check`.

## CI example

The exact syntax depends on your CI system, but the shape is always the same:

```yaml
- name: Check database schema for gaps
  run: python schema_guard.py check "${{ secrets.MONGO_URL }}" "$DB_NAME" -c schema_config.json
```

If this step fails, the pipeline stops — no one has to remember to run it or read a log by hand.

## Finding what you need to run this

| Need | Where to find it |
|---|---|
| `MONGO_URL` | Usually in your project's `.env` file, or your hosting provider's dashboard |
| `DB_NAME` | Same `.env` file, or `show dbs` via `mongosh "<MONGO_URL>"` |
| Network access | The machine running this needs to actually reach the database — some are locked to specific hosts/IPs |

## License

MIT — use it, fork it, adapt it.
