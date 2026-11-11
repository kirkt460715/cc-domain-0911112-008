from ..contracts import (
    identifier,
    record_key,
    split_key,
    integer,
    bounded_text,
    fields,
    sequence,
    source_contract,
    canonical,
    decode,
    digest,
    fail,
)


class Sources:
    def __init__(self, store, normalizer):
        self.store = store
        self.normalizer = normalizer
        self.on_change = None

    def register(self, source, name, priority=100):
        identifier(source)
        name = bounded_text(name, 500)
        integer(priority, 0, 10000)
        payload = {"id": source, "name": name}
        with self.store.atomic() as con:
            old = self.store.one("SELECT id FROM sources WHERE id=?", (source,))
            if old:
                value = self.get(source)
                if value["name"] != name or value["priority"] != priority:
                    fail(
                        "conflict", "source already registered with another definition"
                    )
                return value
            con.execute(
                "INSERT INTO sources VALUES(?,?,0,1,?,?)",
                (source, priority, canonical(payload), digest(payload)),
            )
            self.store.audit(
                "source_registered", source, {"name": name, "priority": priority}
            )
        return self.get(source)

    def get(self, source):
        row = self.store.one("SELECT * FROM sources WHERE id=?", (source,))
        if row is None:
            fail("missing", "source not registered")
        payload = decode(row["payload"])
        if digest(payload) != row["digest"]:
            fail("integrity", "source definition differs")
        return {
            **payload,
            "priority": row["priority"],
            "generation": row["generation"],
            "revision": row["revision"],
        }

    def priority(self, source, priority, expected_revision, reason):
        integer(priority, 0, 10000)
        integer(expected_revision, 1)
        reason = bounded_text(reason)
        with self.store.atomic() as con:
            current = self.get(source)
            if current["revision"] != expected_revision:
                fail("conflict", "source revision changed")
            con.execute(
                "UPDATE sources SET priority=?,revision=revision+1 WHERE id=?",
                (priority, source),
            )
            self.store.bump("data")
            self.store.audit(
                "source_priority_changed",
                source,
                {"priority": priority, "reason": reason},
            )
        return self.get(source)

    def record(self, key, include_inactive=False):
        split_key(key)
        row = self.store.one("SELECT * FROM records WHERE key=?", (key,))
        if row is None or (not row["active"] and not include_inactive):
            fail("missing", "active source record not found", key=key)
        raw = decode(row["raw"])
        normalized = decode(row["normalized"])
        if digest(raw) != row["raw_digest"] or digest(normalized) != row["norm_digest"]:
            fail("integrity", "record payload differs", key=key)
        return {
            **row,
            "raw": raw,
            "normalized": normalized,
            "active": bool(row["active"]),
        }

    def list_records(self, source=None, active_only=True, after="", limit=1000):
        integer(limit, 1, 10000)
        if source is not None:
            self.get(source)
        sql = "SELECT key FROM records WHERE key>?"
        args = [after]
        if source is not None:
            sql += " AND source=?"
            args.append(source)
        if active_only:
            sql += " AND active=1"
        sql += " ORDER BY key LIMIT ?"
        args.append(limit + 1)
        rows = self.store.rows(sql, args)
        page = [self.record(row["key"], not active_only) for row in rows[:limit]]
        return {"records": page, "next": page[-1]["key"] if len(rows) > limit else None}

    def all_active(self):
        return [
            self.record(row["key"])
            for row in self.store.rows(
                "SELECT key FROM records WHERE active=1 ORDER BY key"
            )
        ]

    def snapshot(
        self, batch, source, generation, records, expected_generation, complete=True
    ):
        identifier(batch)
        integer(generation, 1)
        integer(expected_generation)
        if not isinstance(complete, bool):
            fail("input", "complete flag must be boolean")
        sequence(records, 0, 10000)
        request_digest = digest(
            {
                "source": source,
                "generation": generation,
                "records": records,
                "complete": complete,
            }
        )
        with self.store.atomic() as con:
            old = self.store.one("SELECT * FROM batches WHERE id=?", (batch,))
            if old:
                if old["digest"] != request_digest:
                    fail("conflict", "snapshot batch identity differs")
                return {**decode(old["result"]), "reused": True}
            current = self.get(source)
            if (
                current["generation"] != expected_generation
                or generation != expected_generation + 1
            ):
                fail("sequence", "snapshot generation must advance exactly once")
            normalized = []
            seen = set()
            for item in records:
                fields(item, {"id", "data"}, {"id", "data"})
                local_id = identifier(item["id"])
                if local_id in seen:
                    fail("duplicate", "duplicate local record identity")
                seen.add(local_id)
                raw = source_contract(item["data"])
                norm = self.normalizer.normalize(raw)
                normalized.append((record_key(source, local_id), local_id, raw, norm))
            changed = []
            unchanged = []
            reactivated = []
            dictionary_revision = self.store.revision("dictionary")
            for key, local_id, raw, norm in normalized:
                prior = self.store.one("SELECT * FROM records WHERE key=?", (key,))
                raw_digest = digest(raw)
                norm_digest = digest(norm)
                if (
                    prior
                    and prior["active"]
                    and prior["raw_digest"] == raw_digest
                    and prior["norm_digest"] == norm_digest
                ):
                    con.execute(
                        "UPDATE records SET generation=?,dict_revision=? WHERE key=?",
                        (generation, dictionary_revision, key),
                    )
                    unchanged.append(key)
                    continue
                revision = prior["revision"] + 1 if prior else 1
                if prior and not prior["active"]:
                    reactivated.append(key)
                con.execute(
                    "INSERT INTO records VALUES(?,?,?,?,?,1,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET generation=excluded.generation,revision=excluded.revision,active=1,raw=excluded.raw,raw_digest=excluded.raw_digest,normalized=excluded.normalized,norm_digest=excluded.norm_digest,dict_revision=excluded.dict_revision",
                    (
                        key,
                        source,
                        local_id,
                        generation,
                        revision,
                        canonical(raw),
                        raw_digest,
                        canonical(norm),
                        norm_digest,
                        dictionary_revision,
                    ),
                )
                con.execute(
                    "INSERT INTO history VALUES(?,?,?,?,?,1)",
                    (key, revision, canonical(raw), raw_digest, generation),
                )
                self.index_record(key, norm)
                changed.append(key)
            withdrawn = []
            if complete:
                for prior in self.store.rows(
                    "SELECT * FROM records WHERE source=? AND active=1 ORDER BY key",
                    (source,),
                ):
                    if prior["local_id"] in seen:
                        continue
                    revision = prior["revision"] + 1
                    con.execute(
                        "UPDATE records SET active=0,revision=?,generation=? WHERE key=?",
                        (revision, generation, prior["key"]),
                    )
                    con.execute(
                        "INSERT INTO history VALUES(?,?,?,?,?,0)",
                        (
                            prior["key"],
                            revision,
                            prior["raw"],
                            prior["raw_digest"],
                            generation,
                        ),
                    )
                    con.execute("DELETE FROM blocks WHERE record=?", (prior["key"],))
                    withdrawn.append(prior["key"])
            con.execute(
                "UPDATE sources SET generation=? WHERE id=?", (generation, source)
            )
            revision = self.store.bump("data")
            result = {
                "batch": batch,
                "source": source,
                "generation": generation,
                "changed": changed,
                "unchanged": unchanged,
                "withdrawn": withdrawn,
                "reactivated": reactivated,
                "data_revision": revision,
            }
            con.execute(
                "INSERT INTO batches VALUES(?,?,?,?,?)",
                (batch, source, generation, request_digest, canonical(result)),
            )
            self.store.audit("snapshot_ingested", batch, result)
            if self.on_change:
                self.on_change()
        return {**result, "reused": False}

    def index_record(self, key, normalized):
        if self.store.depth == 0:
            fail("state", "index update requires transaction")
        con = self.store.connection
        con.execute("DELETE FROM blocks WHERE record=?", (key,))
        for block in self.normalizer.block_keys(normalized):
            con.execute("INSERT INTO blocks VALUES(?,?)", (block, key))

    def reindex(self, expected_dictionary_revision):
        integer(expected_dictionary_revision)
        with self.store.atomic() as con:
            actual = self.store.revision("dictionary")
            if actual != expected_dictionary_revision:
                fail("conflict", "dictionary changed before reindex")
            changed = []
            for record in self.all_active():
                normalized = self.normalizer.normalize(record["raw"])
                checksum = digest(normalized)
                if record["norm_digest"] != checksum:
                    changed.append(record["key"])
                con.execute(
                    "UPDATE records SET normalized=?,norm_digest=?,dict_revision=? WHERE key=?",
                    (canonical(normalized), checksum, actual, record["key"]),
                )
                self.index_record(record["key"], normalized)
            self.store.bump("data")
            self.store.audit(
                "records_reindexed",
                "dictionary",
                {"revision": actual, "changed": changed},
            )
            if self.on_change:
                self.on_change()
        return {"dictionary_revision": actual, "changed": changed}

    def require_index(self):
        revision = self.store.revision("dictionary")
        stale = self.store.rows(
            "SELECT key FROM records WHERE active=1 AND dict_revision!=? ORDER BY key",
            (revision,),
        )
        if stale:
            fail(
                "index",
                "dictionary changed; reindex records before matching or publishing",
                records=[row["key"] for row in stale],
            )

    def history(self, key):
        self.record(key, True)
        result = []
        for row in self.store.rows(
            "SELECT * FROM history WHERE key=? ORDER BY revision", (key,)
        ):
            raw = decode(row["raw"])
            if digest(raw) != row["raw_digest"]:
                fail("integrity", "record history changed")
            result.append({**row, "raw": raw, "active": bool(row["active"])})
        return result

    def parse(self, content):
        if not isinstance(content, str) or len(content.encode()) > 32_000_000:
            fail("input", "source input must be bounded JSONL text")
        result = []
        from ..contracts import MeshError

        for line, value in enumerate(content.splitlines(), 1):
            if not value.strip():
                continue
            try:
                row = decode(value)
                fields(row, {"id", "data"}, {"id", "data"})
                identifier(row["id"])
                source_contract(row["data"])
                result.append(row)
            except MeshError as exc:
                fail("input", "invalid source row", line=line, error=exc.record())
        sequence(result, 0, 10000)
        return result
