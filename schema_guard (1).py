"""schema_guard.py — a reusable MongoDB schema/integrity checker.

Not tied to any one project. It reads a JSON config file describing what
"correct" looks like for a given database (per collection: field names,
types, allowed values, and references to other collections), and checks a
real database against it. Same script works for any MongoDB-backed project —
only the config file changes.

Two modes:

  1. Bootstrap a config by scanning a real database:
       python schema_guard.py init "<MONGO_URL>" <DB_NAME> -o schema_config.json

     This looks at real data and writes a starting config file — field
     names, inferred types, and whether each field is ever null. You then
     hand-edit it to add enums (allowed values) and references (foreign-key
     style links between collections), since those can't be guessed from
     data alone.

  2. Check a database against an existing config:
       python schema_guard.py check "<MONGO_URL>" <DB_NAME> -c schema_config.json

     Exits 0 if clean, 1 if any real problem is found — safe to wire into a
     CI pipeline as a deploy gate.

Checks performed in "check" mode:
  - missing fields   : config expects a field, no sampled doc has it
  - unexpected fields: data has a field the config doesn't know about
  - type mismatches  : a field's value type doesn't match the config
  - enum violations  : a field's value isn't in its allowed value list
  - always-null      : a field is present but null on every sampled doc (warning only)
  - broken references: a field points to another collection's field by ID,
                        but that ID doesn't exist there (e.g. a task's
                        assignee_id pointing to a deleted member)

  3. Auto-list a FastAPI backend's real endpoints as a manifest:
       python schema_guard.py extract-endpoints backend/server.py -o web_features.json

     Generic to any FastAPI app (looks for @router.get/post/put/delete-style
     decorators) — not specific to this project.

  4. Compare two feature/endpoint manifests for parity (e.g. web vs mobile,
     or v1 vs v2 of an API — any two lists of "what this platform supports"):
       python schema_guard.py parity --a web_features.json --b mobile_features.json

     Reports what's in one but not the other. This is a database-adjacent
     tool, not a database check — it just diffs two lists of feature/endpoint
     identifiers, so the manifests can come from anywhere (auto-extracted,
     hand-written, exported from a project tracker, etc).
"""
import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorClient


# ---------- Type inference ----------

def infer_type(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


# ---------- init: build a starting config from real data ----------

async def init_config(db, collections, sample_size):
    config = {"collections": {}}
    names = collections or await db.list_collection_names()
    for name in sorted(names):
        field_types = {}   # field -> Counter of types seen
        field_null = {}    # field -> saw a null at least once
        doc_count = 0
        async for doc in db[name].find({}).limit(sample_size):
            doc_count += 1
            for k, v in doc.items():
                if k == "_id":
                    continue
                field_types.setdefault(k, Counter())[infer_type(v)] += 1
                if v is None:
                    field_null[k] = True

        if doc_count == 0:
            config["collections"][name] = {"fields": {}, "note": "empty at init time — fill in manually"}
            continue

        fields = {}
        for field, type_counts in field_types.items():
            most_common_type = type_counts.most_common(1)[0][0]
            entry = {"type": most_common_type}
            if field_null.get(field):
                entry["nullable"] = True
            if len(type_counts) > 1:
                entry["_note"] = f"mixed types seen: {dict(type_counts)}"
            fields[field] = entry
        config["collections"][name] = {"fields": fields}

    return config


# ---------- check: compare real data against a config ----------

async def check_collection(db, name, spec, sample_size, full_scan):
    fields = spec.get("fields", {})
    expected = set(fields.keys())

    if name not in await db.list_collection_names():
        return {"status": "missing_collection"}

    doc_count = await db[name].count_documents({})
    if doc_count == 0:
        return {"status": "empty"}

    cursor = db[name].find({}) if full_scan else db[name].find({}).limit(sample_size)
    seen_fields = set()
    type_mismatches = {}     # field -> set of unexpected types seen
    enum_violations = {}     # field -> set of bad values seen
    always_null_candidates = {f: True for f in expected}
    saw_field_at_all = {f: False for f in expected}
    ref_values = {f: set() for f, spec_f in fields.items() if "references" in spec_f}

    n = 0
    async for doc in cursor:
        n += 1
        seen_fields |= (set(doc.keys()) - {"_id"})
        for field, field_spec in fields.items():
            if field not in doc:
                continue
            saw_field_at_all[field] = True
            value = doc[field]
            if value is not None:
                always_null_candidates[field] = False

            expected_type = field_spec.get("type")
            if expected_type and value is not None:
                actual_type = infer_type(value)
                if actual_type != expected_type:
                    type_mismatches.setdefault(field, set()).add(actual_type)

            allowed = field_spec.get("enum")
            if allowed and value is not None and value not in allowed:
                enum_violations.setdefault(field, set()).add(str(value))

            if field in ref_values and value is not None:
                if isinstance(value, list):
                    ref_values[field].update(v for v in value if v is not None)
                else:
                    ref_values[field].add(value)

    # Referential integrity: do the collected IDs actually exist elsewhere?
    broken_refs = {}
    for field, values in ref_values.items():
        if not values:
            continue
        ref_collection, ref_field = fields[field]["references"].split(".")
        existing = set()
        async for doc in db[ref_collection].find({ref_field: {"$in": list(values)}}, {ref_field: 1}):
            existing.add(doc[ref_field])
        missing = values - existing
        if missing:
            broken_refs[field] = {
                "points_to": fields[field]["references"],
                "broken_count": len(missing),
                "examples": list(missing)[:5],
            }

    always_null = [f for f in expected if saw_field_at_all[f] and always_null_candidates[f]]

    return {
        "status": "ok",
        "doc_count": doc_count,
        "checked": n,
        "missing_in_data": expected - seen_fields,
        "unexpected_in_data": seen_fields - expected,
        "type_mismatches": type_mismatches,
        "enum_violations": enum_violations,
        "always_null": always_null,
        "broken_refs": broken_refs,
    }


async def run_check(mongo_url, db_name, config_path, collections, sample_size, full_scan, as_json):
    config = json.loads(Path(config_path).read_text())
    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]

    target_collections = collections or list(config["collections"].keys())
    report = {}
    had_hard_failure = False

    for name in target_collections:
        spec = config["collections"].get(name, {"fields": {}})
        result = await check_collection(db, name, spec, sample_size, full_scan)
        report[name] = result

        if result["status"] in ("missing_collection", "empty"):
            continue
        if (result["missing_in_data"] or result["unexpected_in_data"]
                or result["type_mismatches"] or result["enum_violations"] or result["broken_refs"]):
            had_hard_failure = True

    client.close()

    if as_json:
        def jsonable(r):
            out = dict(r)
            for k in ("missing_in_data", "unexpected_in_data"):
                if k in out:
                    out[k] = sorted(out[k])
            for k in ("type_mismatches", "enum_violations"):
                if k in out:
                    out[k] = {f: sorted(v) for f, v in out[k].items()}
            return out
        print(json.dumps({name: jsonable(r) for name, r in report.items()}, indent=2))
    else:
        print(f"Checking {db_name} @ {mongo_url} against {config_path}\n")
        for name, r in report.items():
            if r["status"] == "missing_collection":
                print(f"[{name}] no collection yet (nothing written here)")
                continue
            if r["status"] == "empty":
                print(f"[{name}] 0 docs — nothing to check yet")
                continue

            scan_note = "full scan" if full_scan else f"sampled {r['checked']}"
            print(f"[{name}] {r['doc_count']} docs ({scan_note})")
            if r["missing_in_data"]:
                print(f"  ! missing fields: {sorted(r['missing_in_data'])}")
            if r["unexpected_in_data"]:
                print(f"  ! unexpected fields: {sorted(r['unexpected_in_data'])}")
            for field, types in r["type_mismatches"].items():
                print(f"  ! {field}: wrong type(s) seen {sorted(types)}")
            for field, values in r["enum_violations"].items():
                print(f"  ! {field}: value(s) not in allowed list: {sorted(values)}")
            for field, info in r["broken_refs"].items():
                print(f"  ! {field}: {info['broken_count']} record(s) reference a missing "
                      f"{info['points_to']} — e.g. {info['examples']}")
            if r["always_null"]:
                print(f"  (info) always empty on every sampled doc: {r['always_null']}")

        print("\n" + ("PROBLEMS FOUND — review above." if had_hard_failure else "All clean."))

    sys.exit(1 if had_hard_failure else 0)


async def run_init(mongo_url, db_name, collections, sample_size, out_path):
    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]
    config = await init_config(db, collections, sample_size)
    # If a field is sometimes null and sometimes something else, the real
    # type is the non-null one — "null" only wins init's most-common-wins
    # vote when nulls happen to outnumber real values in the sample.
    for spec in config["collections"].values():
        for field, field_spec in spec.get("fields", {}).items():
            if field_spec.get("type") == "null" and field_spec.get("nullable"):
                field_spec["type"] = "string"
                field_spec["_note"] = "inferred type was 'null' (nulls outnumbered values in sample) — defaulted to 'string', please confirm"

    client.close()
    Path(out_path).write_text(json.dumps(config, indent=2))
    print(f"Wrote starting config for {db_name} to {out_path}")
    print("Now hand-edit it to add \"enum\": [...] and \"references\": \"collection.field\" "
          "wherever they apply — those can't be guessed from data alone.")


# ---------- extract-endpoints: list a FastAPI app's real routes ----------

ROUTE_PATTERN = re.compile(
    r'@\w+\.(get|post|put|delete|patch)\(\s*["\']([^"\']+)["\']'
)


def extract_endpoints(server_file: str) -> list:
    source = Path(server_file).read_text()
    endpoints = []
    for match in ROUTE_PATTERN.finditer(source):
        method, path = match.group(1).upper(), match.group(2)
        endpoints.append(f"{method} {path}")
    return sorted(set(endpoints))


def run_extract_endpoints(server_file, out_path):
    endpoints = extract_endpoints(server_file)
    Path(out_path).write_text(json.dumps(endpoints, indent=2))
    print(f"Found {len(endpoints)} endpoints in {server_file}, wrote them to {out_path}")
    if not endpoints:
        print("(0 found — this only recognizes @something.get/post/put/delete/patch(\"path\") "
              "style decorators; adjust ROUTE_PATTERN if your framework differs.)")


# ---------- parity: diff two feature/endpoint manifests ----------

def load_manifest(path: str) -> set:
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict):
        # allow either a plain list, or {"features": [...]} / {"endpoints": [...]}
        for key in ("features", "endpoints", "items"):
            if key in data:
                data = data[key]
                break
    return set(data)


def run_parity(path_a, label_a, path_b, label_b, strict, as_json):
    set_a = load_manifest(path_a)
    set_b = load_manifest(path_b)

    only_a = sorted(set_a - set_b)
    only_b = sorted(set_b - set_a)
    shared = sorted(set_a & set_b)

    if as_json:
        print(json.dumps({
            "shared": shared,
            f"only_in_{label_a}": only_a,
            f"only_in_{label_b}": only_b,
        }, indent=2))
    else:
        print(f"Parity check: {label_a} ({len(set_a)}) vs {label_b} ({len(set_b)})\n")
        print(f"In both: {len(shared)}")
        if only_a:
            print(f"\nOnly in {label_a} ({len(only_a)}):")
            for item in only_a:
                print(f"  - {item}")
        if only_b:
            print(f"\nOnly in {label_b} ({len(only_b)}):")
            for item in only_b:
                print(f"  - {item}")
        print("\n" + ("MISMATCH" if (only_a or only_b) else "Parity OK — identical sets."))

    if strict:
        sys.exit(1 if (only_a or only_b) else 0)
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)

    p_init = sub.add_parser("init", help="scan a database and write a starting config file")
    p_init.add_argument("mongo_url")
    p_init.add_argument("db_name")
    p_init.add_argument("--collections", nargs="*", help="limit to these collections (default: all)")
    p_init.add_argument("--sample", type=int, default=200)
    p_init.add_argument("-o", "--out", default="schema_config.json")

    p_check = sub.add_parser("check", help="check a database against a config file")
    p_check.add_argument("mongo_url")
    p_check.add_argument("db_name")
    p_check.add_argument("-c", "--config", required=True)
    p_check.add_argument("--collections", nargs="*", help="limit to these collections (default: all in config)")
    p_check.add_argument("--sample", type=int, default=200)
    p_check.add_argument("--full", action="store_true", help="scan every document instead of sampling")
    p_check.add_argument("--json", action="store_true", help="machine-readable output")

    p_extract = sub.add_parser("extract-endpoints", help="list a FastAPI app's real routes as a manifest")
    p_extract.add_argument("server_file", help="path to the FastAPI server file, e.g. backend/server.py")
    p_extract.add_argument("-o", "--out", default="endpoints.json")

    p_parity = sub.add_parser("parity", help="diff two feature/endpoint manifests (e.g. web vs mobile)")
    p_parity.add_argument("--a", dest="path_a", required=True, help="first manifest file")
    p_parity.add_argument("--label-a", default="a")
    p_parity.add_argument("--b", dest="path_b", required=True, help="second manifest file")
    p_parity.add_argument("--label-b", default="b")
    p_parity.add_argument("--strict", action="store_true", help="exit 1 on any mismatch (CI gate)")
    p_parity.add_argument("--json", action="store_true", help="machine-readable output")

    args = parser.parse_args()

    if args.mode == "init":
        asyncio.run(run_init(args.mongo_url, args.db_name, args.collections, args.sample, args.out))
    elif args.mode == "check":
        asyncio.run(run_check(args.mongo_url, args.db_name, args.config, args.collections,
                               args.sample, args.full, args.json))
    elif args.mode == "extract-endpoints":
        run_extract_endpoints(args.server_file, args.out)
    elif args.mode == "parity":
        run_parity(args.path_a, args.label_a, args.path_b, args.label_b, args.strict, args.json)


if __name__ == "__main__":
    main()

