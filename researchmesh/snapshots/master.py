from ..contracts import (
    bounded_text,
    integer,
    identifier,
    canonical,
    decode,
    digest,
    fail,
)

FIELDS = ("title", "institution", "kind", "year", "country", "abstract")


class MasterRecords:
    def __init__(self, store, sources, normalizer, graph):
        self.store = store
        self.sources = sources
        self.normalizer = normalizer
        self.graph = graph

    def rank(self, record):
        source = self.sources.get(record["source"])
        quality = self.normalizer.completeness(record["raw"])["score"]
        return (source["priority"], -quality, record["key"])

    def build(self, key):
        self.sources.require_index()
        cluster = self.graph.cluster(key)
        records = [self.sources.record(member) for member in self.graph.members(key)]
        records.sort(key=self.rank)
        pins = {
            row["field"]: row
            for row in self.store.rows("SELECT * FROM pins WHERE cluster=?", (cluster,))
        }
        selected = {}
        provenance = {}
        warnings = []
        for field in FIELDS:
            candidates = [row for row in records if row["raw"][field] not in [None, ""]]
            pin = pins.get(field)
            chosen = None
            if pin:
                pinned = next(
                    (row for row in candidates if row["key"] == pin["record"]), None
                )
                if pinned and digest(pinned["raw"][field]) == pin["value_digest"]:
                    chosen = pinned
                else:
                    warnings.append(
                        {"kind": "stale_pin", "field": field, "record": pin["record"]}
                    )
            if chosen is None and candidates:
                chosen = candidates[0]
            selected[field] = chosen["raw"][field] if chosen else None
            provenance[field] = {
                "record": chosen["key"] if chosen else None,
                "revision": chosen["revision"] if chosen else None,
                "rule": "manual_pin"
                if pin
                and chosen
                and chosen["key"] == pin["record"]
                and digest(chosen["raw"][field]) == pin["value_digest"]
                else "priority_completeness_key",
                "alternatives": [
                    {
                        "record": row["key"],
                        "value": row["raw"][field],
                        "priority": self.sources.get(row["source"])["priority"],
                    }
                    for row in candidates
                    if row is not chosen
                ],
            }
        selected["people"] = sorted(
            {value for record in records for value in record["normalized"]["people"]}
        )
        selected["tags"] = sorted(
            {value for record in records for value in record["normalized"]["tags"]}
        )
        selected["identifiers"] = {}
        for record in records:
            for namespace, values in record["normalized"]["identifiers"].items():
                selected["identifiers"].setdefault(namespace, set()).update(values)
        selected["identifiers"] = {
            name: sorted(values)
            for name, values in sorted(selected["identifiers"].items())
        }
        payload = {
            "id": cluster,
            "members": sorted(row["key"] for row in records),
            "data": selected,
            "provenance": provenance,
            "warnings": warnings,
            "member_versions": {
                row["key"]: {
                    "revision": row["revision"],
                    "raw_digest": row["raw_digest"],
                    "norm_digest": row["norm_digest"],
                }
                for row in records
            },
        }
        return {**payload, "digest": digest(payload)}

    def all(self):
        return [self.build(row["id"]) for row in self.graph.clusters()]

    def pin(
        self, key, field, record, expected_master_digest, expected_revision, reason
    ):
        if field not in FIELDS:
            fail("input", "only scalar master fields may be pinned")
        integer(expected_revision)
        reason = bounded_text(reason)
        with self.store.atomic() as con:
            master = self.build(key)
            if master["digest"] != expected_master_digest:
                fail("conflict", "master changed since review")
            if record not in master["members"]:
                fail("constraint", "pinned source must belong to cluster")
            source = self.sources.record(record)
            value = source["raw"][field]
            if value in [None, ""]:
                fail("input", "cannot pin an empty scalar value")
            prior = self.store.one(
                "SELECT revision FROM pins WHERE cluster=? AND field=?",
                (master["id"], field),
            )
            revision = prior["revision"] if prior else 0
            if revision != expected_revision:
                fail("conflict", "pin revision changed")
            con.execute(
                "INSERT INTO pins VALUES(?,?,?,?,?,?) ON CONFLICT(cluster,field) DO UPDATE SET record=excluded.record,value_digest=excluded.value_digest,reason=excluded.reason,revision=excluded.revision",
                (master["id"], field, record, digest(value), reason, revision + 1),
            )
            self.store.bump("data")
            self.store.audit(
                "master_field_pinned",
                master["id"],
                {"field": field, "record": record, "reason": reason},
            )
        return self.build(key)

    def unpin(self, key, field, expected_revision, reason):
        integer(expected_revision, 1)
        reason = bounded_text(reason)
        with self.store.atomic() as con:
            cluster = self.graph.cluster(key)
            row = self.store.one(
                "SELECT revision FROM pins WHERE cluster=? AND field=?",
                (cluster, field),
            )
            if row is None or row["revision"] != expected_revision:
                fail("conflict", "pin missing or revision changed")
            con.execute(
                "DELETE FROM pins WHERE cluster=? AND field=?", (cluster, field)
            )
            self.store.bump("data")
            self.store.audit(
                "master_field_unpinned", cluster, {"field": field, "reason": reason}
            )
        return self.build(key)

    def conflicts(self, key):
        master = self.build(key)
        records = [self.sources.record(member) for member in master["members"]]
        rows = []
        for field in FIELDS:
            values = {}
            for record in records:
                value = record["raw"][field]
                if value in [None, ""]:
                    continue
                values.setdefault(canonical(value), []).append(record["key"])
            if len(values) > 1:
                rows.append(
                    {
                        "field": field,
                        "chosen": master["data"][field],
                        "variants": [
                            {"value": decode(value), "records": keys}
                            for value, keys in sorted(values.items())
                        ],
                    }
                )
        return {
            "cluster": master["id"],
            "conflicts": rows,
            "master_digest": master["digest"],
        }

    def orphan_pins(self):
        result = []
        for row in self.store.rows("SELECT * FROM pins ORDER BY cluster,field"):
            current = self.store.one(
                "SELECT cluster FROM components WHERE record=?", (row["record"],)
            )
            if current is None or current["cluster"] != row["cluster"]:
                result.append(
                    {**row, "current_cluster": current["cluster"] if current else None}
                )
        return result

    def discard_orphan_pin(self, cluster, field, expected_revision, reason):
        integer(expected_revision, 1)
        reason = bounded_text(reason)
        with self.store.atomic() as con:
            row = self.store.one(
                "SELECT * FROM pins WHERE cluster=? AND field=?", (cluster, field)
            )
            if row is None or row["revision"] != expected_revision:
                fail("conflict", "orphan pin revision differs")
            current = self.store.one(
                "SELECT cluster FROM components WHERE record=?", (row["record"],)
            )
            if current is not None and current["cluster"] == cluster:
                fail("state", "pin still belongs to its original cluster; use unpin")
            con.execute(
                "DELETE FROM pins WHERE cluster=? AND field=?", (cluster, field)
            )
            self.store.bump("data")
            self.store.audit(
                "orphan_pin_discarded",
                cluster,
                {"field": field, "record": row["record"], "reason": reason},
            )
        return {"cluster": cluster, "field": field, "discarded": True}
