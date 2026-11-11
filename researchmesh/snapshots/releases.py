import os
import tempfile
from pathlib import Path
from ..contracts import identifier, canonical, decode, digest, fail, fields


class Releases:
    def __init__(self, store, sources, graph, masters):
        self.store = store
        self.sources = sources
        self.graph = graph
        self.masters = masters

    def preview(self):
        with self.store.atomic():
            self.sources.require_index()
            self.graph.verify()
            masters = self.masters.all()
            payload = {
                "format_version": 1,
                "masters": masters,
                "sources": [
                    self.sources.get(row["id"])
                    for row in self.store.rows("SELECT id FROM sources ORDER BY id")
                ],
                "dictionary_revision": self.store.revision("dictionary"),
                "graph_revision": self.store.revision("graph"),
                "data_revision": self.store.revision("data"),
                "active_decisions": [row["id"] for row in self.graph.active_edges()],
                "cannot_links": [row["id"] for row in self.graph.restrictions()],
            }
        return {"payload": payload, "digest": digest(payload)}

    def freeze(self, release, expected_digest, operator):
        identifier(release)
        identifier(operator)
        with self.store.atomic() as con:
            old = self.store.one("SELECT id FROM releases WHERE id=?", (release,))
            if old:
                row = self.get(release)
                if row["digest"] != expected_digest:
                    fail("conflict", "release identity differs")
                return row
            preview = self.preview()
            if preview["digest"] != expected_digest:
                fail("conflict", "master records changed after release preview")
            warnings = [
                warning
                for master in preview["payload"]["masters"]
                for warning in master["warnings"]
            ]
            if warnings or self.masters.orphan_pins():
                fail(
                    "state",
                    "resolve stale or orphan field pins before release",
                    warnings=warnings,
                )
            con.execute(
                "INSERT INTO releases VALUES(?,?,?)",
                (release, canonical(preview["payload"]), preview["digest"]),
            )
            self.store.audit(
                "release_frozen",
                release,
                {"digest": preview["digest"], "operator": operator},
            )
        return self.get(release)

    def get(self, release):
        row = self.store.one("SELECT * FROM releases WHERE id=?", (release,))
        if row is None:
            fail("missing", "release not found")
        payload = decode(row["payload"])
        if digest(payload) != row["digest"]:
            fail("integrity", "release payload differs")
        self.verify_payload(payload)
        return {"id": release, "payload": payload, "digest": row["digest"]}

    @staticmethod
    def verify_payload(payload):
        if not isinstance(payload, dict) or payload.get("format_version") != 1:
            fail("version", "unsupported master release format")
        seen = set()
        clusters = set()
        for master in payload["masters"]:
            checksum = master["digest"]
            actual = {key: value for key, value in master.items() if key != "digest"}
            if digest(actual) != checksum:
                fail("integrity", "master fingerprint differs")
            if master["id"] in clusters or not master["members"]:
                fail("integrity", "duplicate or empty cluster")
            clusters.add(master["id"])
            if master["id"] != min(master["members"]):
                fail("integrity", "cluster identifier is not canonical")
            for member in master["members"]:
                if member in seen:
                    fail("integrity", "source record appears in multiple masters")
                seen.add(member)
            if set(master["member_versions"]) != set(master["members"]):
                fail("integrity", "master member version manifest differs")
            for provenance in master["provenance"].values():
                if (
                    provenance["record"] is not None
                    and provenance["record"] not in master["members"]
                ):
                    fail("integrity", "master provenance points outside cluster")
        return {"masters": len(clusters), "records": len(seen), "verified": True}

    def diff(self, before, after):
        left = self.get(before)["payload"]
        right = self.get(after)["payload"]
        old = {row["id"]: row for row in left["masters"]}
        new = {row["id"]: row for row in right["masters"]}
        old_members = {
            member: row["id"] for row in left["masters"] for member in row["members"]
        }
        new_members = {
            member: row["id"] for row in right["masters"] for member in row["members"]
        }
        transitions = []
        for member in sorted(old_members.keys() | new_members.keys()):
            a, b = old_members.get(member), new_members.get(member)
            if a != b:
                transitions.append(
                    {
                        "record": member,
                        "before": a,
                        "after": b,
                        "kind": "added"
                        if a is None
                        else ("withdrawn" if b is None else "reassigned"),
                    }
                )
        clusters = []
        for key in sorted(old.keys() | new.keys()):
            a, b = old.get(key), new.get(key)
            if a == b:
                continue
            changes = []
            for field in sorted(
                set(a["data"] if a else {}) | set(b["data"] if b else {})
            ):
                av = a["data"].get(field) if a else None
                bv = b["data"].get(field) if b else None
                if av != bv:
                    changes.append({"field": field, "before": av, "after": bv})
            clusters.append(
                {
                    "cluster": key,
                    "kind": "added"
                    if a is None
                    else ("removed" if b is None else "changed"),
                    "field_changes": changes,
                    "members_before": a["members"] if a else [],
                    "members_after": b["members"] if b else [],
                    "provenance_changed": bool(
                        a and b and a["provenance"] != b["provenance"]
                    ),
                }
            )
        splits = []
        merges = []
        for key, master in old.items():
            targets = sorted(
                {
                    new_members[member]
                    for member in master["members"]
                    if member in new_members
                }
            )
            if len(targets) > 1:
                splits.append({"before": key, "after": targets})
        for key, master in new.items():
            origins = sorted(
                {
                    old_members[member]
                    for member in master["members"]
                    if member in old_members
                }
            )
            if len(origins) > 1:
                merges.append({"before": origins, "after": key})
        return {
            "before": before,
            "after": after,
            "record_transitions": transitions,
            "clusters": clusters,
            "splits": splits,
            "merges": merges,
            "changed": left != right,
        }

    def export(self, release, path):
        row = self.get(release)
        envelope = {"type": "researchmesh-release", "version": 1, **row}
        content = canonical(envelope).encode("utf-8")
        target = Path(path)
        if target.exists() or target.is_symlink() or not target.parent.is_dir():
            fail("path", "export requires a new file in an existing directory")
        descriptor, name = tempfile.mkstemp(prefix=".mesh-", dir=target.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                fail("path", "export destination appeared concurrently")
        finally:
            temporary.unlink(missing_ok=True)
        return {"path": str(target), "digest": row["digest"], "bytes": len(content)}

    @staticmethod
    def verify(path):
        path = Path(path)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 100000000:
            fail("path", "release must be a bounded regular local file")
        row = decode(path.read_text("utf-8"))
        fields(
            row,
            {"type", "version", "id", "payload", "digest"},
            {"type", "version", "id", "payload", "digest"},
        )
        if row["type"] != "researchmesh-release" or row["version"] != 1:
            fail("version", "unknown release envelope")
        if digest(row["payload"]) != row["digest"]:
            fail("integrity", "release fingerprint differs")
        result = Releases.verify_payload(row["payload"])
        return {**result, "id": row["id"], "digest": row["digest"]}

    def list(self):
        result = []
        for row in self.store.rows("SELECT id FROM releases ORDER BY rowid"):
            release = self.get(row["id"])
            result.append(
                {
                    "id": row["id"],
                    "digest": release["digest"],
                    "masters": len(release["payload"]["masters"]),
                    "data_revision": release["payload"]["data_revision"],
                }
            )
        return result

    def lineage(self, record):
        from ..contracts import split_key

        split_key(record)
        rows = []
        for release in self.list():
            payload = self.get(release["id"])["payload"]
            master = next(
                (row for row in payload["masters"] if record in row["members"]), None
            )
            rows.append(
                {
                    "release": release["id"],
                    "cluster": master["id"] if master else None,
                    "record_revision": master["member_versions"][record]["revision"]
                    if master
                    else None,
                }
            )
        return {"record": record, "releases": rows}
