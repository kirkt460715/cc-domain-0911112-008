from ..contracts import integer, bounded_text, fail, digest


class Dictionary:
    def __init__(self, store):
        self.store = store

    def resolve(self, kind, alias):
        if kind not in {"institution", "tag"}:
            fail("input", "unsupported dictionary kind")
        visited = set()
        current = alias
        while True:
            if current in visited:
                fail("cycle", "dictionary contains alias cycle")
            visited.add(current)
            row = self.store.one(
                "SELECT target FROM dictionary WHERE kind=? AND alias=?",
                (kind, current),
            )
            if row is None:
                return current
            current = row["target"]
            if len(visited) > 100:
                fail("capacity", "alias chain too long")

    def put(self, kind, alias, target, expected_revision, reason):
        from .text import fold

        integer(expected_revision)
        reason = bounded_text(reason)
        if kind not in {"institution", "tag"}:
            fail("input", "unsupported dictionary kind")
        alias = fold(bounded_text(alias, 500))
        target = fold(bounded_text(target, 500))
        if not alias or not target or alias == target:
            fail("input", "alias and target must be distinct normalized names")
        with self.store.atomic() as con:
            if self.store.revision("dictionary") != expected_revision:
                fail("conflict", "dictionary revision changed")
            con.execute(
                "INSERT INTO dictionary VALUES(?,?,?) ON CONFLICT(kind,alias) DO UPDATE SET target=excluded.target",
                (kind, alias, target),
            )
            resolved = self.resolve(kind, alias)
            revision = self.store.bump("dictionary")
            self.store.audit(
                "dictionary_changed",
                kind,
                {
                    "alias": alias,
                    "target": target,
                    "resolved": resolved,
                    "revision": revision,
                    "reason": reason,
                },
            )
        return {
            "kind": kind,
            "alias": alias,
            "target": target,
            "resolved": resolved,
            "revision": revision,
        }

    def remove(self, kind, alias, expected_revision, reason):
        from .text import fold

        integer(expected_revision)
        alias = fold(alias)
        reason = bounded_text(reason)
        with self.store.atomic() as con:
            if self.store.revision("dictionary") != expected_revision:
                fail("conflict", "dictionary revision changed")
            row = self.store.one(
                "SELECT target FROM dictionary WHERE kind=? AND alias=?", (kind, alias)
            )
            if row is None:
                fail("missing", "alias not found")
            con.execute(
                "DELETE FROM dictionary WHERE kind=? AND alias=?", (kind, alias)
            )
            revision = self.store.bump("dictionary")
            self.store.audit(
                "dictionary_removed",
                kind,
                {
                    "alias": alias,
                    "target": row["target"],
                    "reason": reason,
                    "revision": revision,
                },
            )
        return {"removed": alias, "revision": revision}

    def list(self, kind=None):
        if kind is not None and kind not in {"institution", "tag"}:
            fail("input", "unsupported dictionary kind")
        rows = self.store.rows("SELECT * FROM dictionary ORDER BY kind,alias")
        if kind is not None:
            rows = [row for row in rows if row["kind"] == kind]
        for row in rows:
            row["resolved"] = self.resolve(row["kind"], row["alias"])
        return {
            "revision": self.store.revision("dictionary"),
            "rows": rows,
            "digest": digest(rows),
        }

    def impact(self, kind, alias):
        from .text import fold
        from ..contracts import decode

        alias = fold(alias)
        affected = []
        for row in self.store.rows(
            "SELECT key,raw FROM records WHERE active=1 ORDER BY key"
        ):
            raw = decode(row["raw"])
            values = [raw["institution"]] if kind == "institution" else raw["tags"]
            if any(
                fold(value) == alias or self.resolve(kind, fold(value)) == alias
                for value in values
            ):
                affected.append(row["key"])
        return {
            "kind": kind,
            "alias": alias,
            "records": affected,
            "count": len(affected),
            "note": "Records require explicit reindexing after dictionary changes.",
        }
