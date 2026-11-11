from ..contracts import (
    identifier,
    integer,
    fields,
    sequence,
    record_key,
    canonical,
    decode,
    digest,
    fail,
    source_contract,
)


class ChangeFeed:
    def __init__(self, store, sources):
        self.store = store
        self.sources = sources
        self.store.connection.executescript("""
CREATE TABLE IF NOT EXISTS feed_batches(id TEXT PRIMARY KEY,source TEXT NOT NULL,from_generation INTEGER NOT NULL,
 to_generation INTEGER NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,result TEXT NOT NULL);
""")

    def inspect(self, source, changes, expected_generation):
        integer(expected_generation)
        sequence(changes, 1, 10000)
        current = self.sources.get(source)
        if current["generation"] != expected_generation:
            fail("conflict", "source generation changed before delta preview")
        active = {
            row["local_id"]: row["raw"]
            for row in self.sources.all_active()
            if row["source"] == source
        }
        seen = set()
        operations = []
        for operation in changes:
            fields(
                operation,
                {"id", "operation", "expected_revision", "data"},
                {"id", "operation", "expected_revision"},
            )
            local_id = identifier(operation["id"])
            if local_id in seen:
                fail("duplicate", "delta may change a source identity only once")
            seen.add(local_id)
            integer(operation["expected_revision"])
            kind = operation["operation"]
            if kind not in {"upsert", "withdraw"}:
                fail("input", "delta operation must be upsert or withdraw")
            key = record_key(source, local_id)
            previous = self.store.one(
                "SELECT revision,active,raw_digest FROM records WHERE key=?", (key,)
            )
            revision = previous["revision"] if previous else 0
            if revision != operation["expected_revision"]:
                fail(
                    "conflict",
                    "record revision differs",
                    record=key,
                    expected=operation["expected_revision"],
                    actual=revision,
                )
            if kind == "withdraw":
                if "data" in operation:
                    fail("input", "withdraw operation must not include data")
                if local_id not in active:
                    fail("state", "cannot withdraw an inactive or missing record")
                active.pop(local_id)
                value = None
            else:
                if "data" not in operation:
                    fail("input", "upsert requires data")
                value = source_contract(operation["data"])
                self.sources.normalizer.normalize(value)
                active[local_id] = value
            operations.append(
                {
                    "id": local_id,
                    "operation": kind,
                    "expected_revision": revision,
                    "before_digest": previous["raw_digest"] if previous else None,
                    "after_digest": digest(value) if value else None,
                }
            )
        records = [
            {"id": local_id, "data": value}
            for local_id, value in sorted(active.items())
        ]
        payload = {
            "source": source,
            "from_generation": expected_generation,
            "to_generation": expected_generation + 1,
            "operations": operations,
            "resulting_records": records,
            "dictionary_revision": self.store.revision("dictionary"),
        }
        return {"payload": payload, "digest": digest(payload)}

    def apply(self, batch, source, changes, expected_generation, expected_digest):
        identifier(batch)
        request = {
            "source": source,
            "changes": changes,
            "expected_generation": expected_generation,
            "expected_digest": expected_digest,
        }
        checksum = digest(request)
        with self.store.atomic() as con:
            old = self.store.one("SELECT * FROM feed_batches WHERE id=?", (batch,))
            if old:
                if old["digest"] != checksum:
                    fail("conflict", "delta batch identity differs")
                return {**decode(old["result"]), "reused": True}
            preview = self.inspect(source, changes, expected_generation)
            if preview["digest"] != expected_digest:
                fail("conflict", "delta preview differs from current input state")
            snapshot_batch = "delta-" + batch
            identifier(snapshot_batch)
            if self.store.one("SELECT id FROM batches WHERE id=?", (snapshot_batch,)):
                fail("conflict", "reserved delta snapshot identity already exists")
            result = self.sources.snapshot(
                snapshot_batch,
                source,
                expected_generation + 1,
                preview["payload"]["resulting_records"],
                expected_generation,
            )
            con.execute(
                "INSERT INTO feed_batches VALUES(?,?,?,?,?,?,?)",
                (
                    batch,
                    source,
                    expected_generation,
                    expected_generation + 1,
                    canonical(request),
                    checksum,
                    canonical(result),
                ),
            )
            self.store.audit(
                "change_feed_applied",
                batch,
                {
                    "source": source,
                    "generation": expected_generation + 1,
                    "operations": preview["payload"]["operations"],
                },
            )
        return {**result, "reused": False}

    def history(self, source):
        self.sources.get(source)
        rows = self.store.rows(
            "SELECT * FROM feed_batches WHERE source=? ORDER BY to_generation,id",
            (source,),
        )
        result = []
        for row in rows:
            request = decode(row["payload"])
            if digest(request) != row["digest"]:
                fail("integrity", "change-feed history differs")
            result.append(
                {
                    "batch": row["id"],
                    "from_generation": row["from_generation"],
                    "to_generation": row["to_generation"],
                    "digest": row["digest"],
                    "operations": len(request["changes"]),
                    "result": decode(row["result"]),
                }
            )
        return {"source": source, "batches": result}

    def compare_generations(self, source, before, after):
        self.sources.get(source)
        integer(before)
        integer(after)
        current = self.sources.get(source)["generation"]
        if before > after or after > current:
            fail("input", "generation comparison outside source history")
        result = []
        for row in self.store.rows(
            "SELECT key FROM records WHERE source=? ORDER BY key", (source,)
        ):
            history = self.sources.history(row["key"])
            previous = next(
                (item for item in reversed(history) if item["generation"] <= before),
                None,
            )
            following = next(
                (item for item in reversed(history) if item["generation"] <= after),
                None,
            )
            left = previous["raw"] if previous and previous["active"] else None
            right = following["raw"] if following and following["active"] else None
            if left == right:
                continue
            fields_changed = []
            for field in sorted(set(left or {}) | set(right or {})):
                a = left.get(field) if left else None
                b = right.get(field) if right else None
                if a != b:
                    fields_changed.append({"field": field, "before": a, "after": b})
            result.append(
                {
                    "record": row["key"],
                    "kind": "added"
                    if left is None
                    else ("withdrawn" if right is None else "changed"),
                    "fields": fields_changed,
                }
            )
        return {"source": source, "before": before, "after": after, "changes": result}
