from collections import deque
from ..contracts import (
    identifier,
    pair,
    bounded_text,
    integer,
    canonical,
    decode,
    digest,
    fail,
)
from ..linkage.evidence import conflict_reasons


class Union:
    def __init__(self, keys):
        self.parent = {key: key for key in keys}
        self.members = {key: {key} for key in keys}

    def root(self, key):
        if key not in self.parent:
            fail("missing", "record absent from active graph")
        current = key
        while self.parent[current] != current:
            current = self.parent[current]
        while self.parent[key] != key:
            following = self.parent[key]
            self.parent[key] = current
            key = following
        return current

    def join(self, left, right):
        a, b = self.root(left), self.root(right)
        if a == b:
            return a
        a, b = sorted([a, b])
        self.parent[b] = a
        self.members[a] |= self.members.pop(b)
        return a


class DecisionGraph:
    def __init__(self, store, sources, evidence):
        self.store = store
        self.sources = sources
        self.evidence = evidence

    def decision(self, ident):
        row = self.store.one("SELECT * FROM decisions WHERE id=?", (ident,))
        if row is None:
            fail("missing", "decision not found")
        evidence = decode(row["evidence"])
        if digest(evidence) != row["digest"]:
            fail("integrity", "decision evidence changed")
        return {**row, "evidence": evidence}

    def restrictions(self):
        return [
            self.decision(row["id"])
            for row in self.store.rows(
                "SELECT id FROM decisions WHERE kind='cannot' AND state='active' ORDER BY id"
            )
        ]

    def active_edges(self):
        return [
            self.decision(row["id"])
            for row in self.store.rows(
                "SELECT id FROM decisions WHERE kind='merge' AND state='active' ORDER BY rowid"
            )
        ]

    def check_groups(self, left_members, right_members, records, restrictions):
        blockers = []
        for rule in restrictions:
            a, b = rule["left_key"], rule["right_key"]
            if (a in left_members and b in right_members) or (
                a in right_members and b in left_members
            ):
                blockers.append(
                    {
                        "kind": "cannot_link",
                        "decision": rule["id"],
                        "left": a,
                        "right": b,
                    }
                )
        for left in sorted(left_members):
            for right in sorted(right_members):
                for reason in conflict_reasons(
                    records[left]["normalized"], records[right]["normalized"]
                ):
                    blockers.append({"kind": reason, "left": left, "right": right})
        return blockers

    def rebuild(self):
        with self.store.atomic() as con:
            records = {row["key"]: row for row in self.sources.all_active()}
            union = Union(records)
            restrictions = self.restrictions()
            stale = []
            blocked = []
            for edge in self.active_edges():
                left, right = edge["left_key"], edge["right_key"]
                if (
                    left not in records
                    or right not in records
                    or not self.evidence.fresh(edge["evidence"])
                ):
                    con.execute(
                        "UPDATE decisions SET state='stale' WHERE id=?", (edge["id"],)
                    )
                    stale.append(edge["id"])
                    continue
                a, b = union.root(left), union.root(right)
                if a == b:
                    continue
                blockers = self.check_groups(
                    union.members[a], union.members[b], records, restrictions
                )
                if blockers:
                    con.execute(
                        "UPDATE decisions SET state='blocked' WHERE id=?", (edge["id"],)
                    )
                    blocked.append({"decision": edge["id"], "blockers": blockers})
                    continue
                union.join(left, right)
            con.execute("DELETE FROM components")
            for key in sorted(records):
                con.execute(
                    "INSERT INTO components VALUES(?,?)", (key, union.root(key))
                )
            revision = self.store.bump("graph")
            self.store.audit(
                "graph_rebuilt",
                "graph",
                {
                    "revision": revision,
                    "stale": stale,
                    "blocked": blocked,
                    "components": len(union.members),
                    "active_records": len(records),
                },
            )
        return {
            "revision": revision,
            "stale": stale,
            "blocked": blocked,
            "components": len(union.members),
        }

    def cluster(self, key):
        self.sources.record(key)
        row = self.store.one("SELECT cluster FROM components WHERE record=?", (key,))
        if row is None:
            fail("index", "graph materialization missing")
        return row["cluster"]

    def members(self, key):
        cluster = self.cluster(key)
        return [
            row["record"]
            for row in self.store.rows(
                "SELECT record FROM components WHERE cluster=? ORDER BY record",
                (cluster,),
            )
        ]

    def clusters(self):
        result = []
        for row in self.store.rows(
            "SELECT cluster,COUNT(*) AS count FROM components GROUP BY cluster ORDER BY cluster"
        ):
            result.append(
                {
                    "id": row["cluster"],
                    "members": self.members(row["cluster"]),
                    "size": row["count"],
                }
            )
        return result

    def preview(self, left, right, kind="merge"):
        if kind not in {"merge", "cannot"}:
            fail("input", "decision kind must be merge or cannot")
        evidence = self.evidence.compare(left, right)
        left, right = evidence["left"], evidence["right"]
        a, b = self.members(left), self.members(right)
        connected = self.cluster(left) == self.cluster(right)
        records = {key: self.sources.record(key) for key in set(a) | set(b)}
        blockers = []
        if kind == "cannot":
            if connected:
                blockers.append({"kind": "already_connected", "members": a})
        elif not connected:
            blockers = self.check_groups(set(a), set(b), records, self.restrictions())
        payload = {
            "kind": kind,
            "evidence": evidence,
            "left_members": a,
            "right_members": b,
            "already_connected": connected,
            "blockers": blockers,
            "graph_revision": self.store.revision("graph"),
            "data_revision": self.store.revision("data"),
        }
        return {**payload, "digest": digest(payload), "allowed": not blockers}

    def decide(self, decision, left, right, kind, expected_digest, operator, reason):
        identifier(decision)
        identifier(operator)
        reason = bounded_text(reason)
        left, right = pair(left, right)
        with self.store.atomic() as con:
            old = self.store.one("SELECT id FROM decisions WHERE id=?", (decision,))
            if old:
                current = self.decision(decision)
                if (
                    current["left_key"],
                    current["right_key"],
                    current["kind"],
                    current["operator"],
                    current["reason"],
                ) != (left, right, kind, operator, reason):
                    fail("conflict", "decision identity differs")
                return current
            preview = self.preview(left, right, kind)
            if preview["digest"] != expected_digest:
                fail("conflict", "decision preview changed")
            if not preview["allowed"]:
                fail(
                    "constraint",
                    "decision would violate graph constraints",
                    blockers=preview["blockers"],
                )
            evidence = preview["evidence"]
            con.execute(
                "INSERT INTO decisions VALUES(?,?,?,?,'active',?,?,?,?)",
                (
                    decision,
                    left,
                    right,
                    kind,
                    canonical(evidence),
                    digest(evidence),
                    reason,
                    operator,
                ),
            )
            self.store.audit(
                "decision_recorded",
                decision,
                {
                    "kind": kind,
                    "left": left,
                    "right": right,
                    "reason": reason,
                    "operator": operator,
                    "preview": expected_digest,
                },
            )
            self.rebuild()
        return self.decision(decision)

    def revoke(self, decision, expected_graph_revision, operator, reason):
        integer(expected_graph_revision)
        identifier(operator)
        reason = bounded_text(reason)
        with self.store.atomic() as con:
            if self.store.revision("graph") != expected_graph_revision:
                fail("conflict", "graph revision changed")
            row = self.decision(decision)
            if row["state"] == "revoked":
                fail("state", "decision already revoked")
            con.execute("UPDATE decisions SET state='revoked' WHERE id=?", (decision,))
            self.store.audit(
                "decision_revoked",
                decision,
                {"operator": operator, "reason": reason, "previous": row["state"]},
            )
            self.rebuild()
        return self.decision(decision)

    def path(self, left, right):
        left, right = pair(left, right)
        self.sources.record(left)
        self.sources.record(right)
        neighbours = {}
        for edge in self.active_edges():
            a, b = edge["left_key"], edge["right_key"]
            neighbours.setdefault(a, []).append((b, edge["id"]))
            neighbours.setdefault(b, []).append((a, edge["id"]))
        queue = deque([left])
        previous = {left: None}
        while queue:
            key = queue.popleft()
            if key == right:
                break
            for peer, decision in sorted(neighbours.get(key, [])):
                if peer not in previous:
                    previous[peer] = (key, decision)
                    queue.append(peer)
        if right not in previous:
            return {"connected": False, "records": [], "decisions": []}
        records, decisions = [right], []
        current = right
        while current != left:
            current, decision = previous[current]
            records.append(current)
            decisions.append(decision)
        return {
            "connected": True,
            "records": list(reversed(records)),
            "decisions": list(reversed(decisions)),
        }

    def impact(self, decision):
        target = self.decision(decision)
        if target["kind"] != "merge" or target["state"] != "active":
            return {"decision": decision, "splits_component": False, "components": []}
        members = self.members(target["left_key"])
        union = Union(members)
        for edge in self.active_edges():
            if (
                edge["id"] != decision
                and edge["left_key"] in union.parent
                and edge["right_key"] in union.parent
            ):
                union.join(edge["left_key"], edge["right_key"])
        components = [sorted(values) for name, values in sorted(union.members.items())]
        return {
            "decision": decision,
            "splits_component": len(components) > 1,
            "components": components,
        }

    def verify(self):
        records = {row["key"]: row for row in self.sources.all_active()}
        union = Union(records)
        for edge in self.active_edges():
            if not self.evidence.fresh(edge["evidence"]):
                fail("integrity", "active edge has stale evidence", decision=edge["id"])
            union.join(edge["left_key"], edge["right_key"])
        actual = {
            row["record"]: row["cluster"]
            for row in self.store.rows("SELECT * FROM components")
        }
        expected = {key: union.root(key) for key in records}
        if actual != expected:
            fail("integrity", "materialized components differ from decisions")
        restrictions = self.restrictions()
        for rule in restrictions:
            if rule["left_key"] in records and rule["right_key"] in records:
                if union.root(rule["left_key"]) == union.root(rule["right_key"]):
                    fail("integrity", "cannot-link rule violated", decision=rule["id"])
        for members in union.members.values():
            ordered = sorted(members)
            for index, left in enumerate(ordered):
                for right in ordered[index + 1 :]:
                    if conflict_reasons(
                        records[left]["normalized"], records[right]["normalized"]
                    ):
                        fail(
                            "integrity",
                            "cluster contains conflicting identifiers or kinds",
                        )
        return {
            "verified": True,
            "components": len(union.members),
            "records": len(records),
            "graph_revision": self.store.revision("graph"),
        }
