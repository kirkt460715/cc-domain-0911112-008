from ..contracts import (
    identifier,
    pair,
    integer,
    canonical,
    decode,
    digest,
    fail,
    bounded_text,
)


class ReviewQueue:
    def __init__(self, store, evidence, graph):
        self.store = store
        self.evidence = evidence
        self.graph = graph

    def enqueue(self, task, left, right):
        identifier(task)
        left, right = pair(left, right)
        with self.store.atomic() as con:
            old = self.store.one("SELECT id FROM review_tasks WHERE id=?", (task,))
            if old:
                result = self.get(task)
                if (result["left_key"], result["right_key"]) != (left, right):
                    fail("conflict", "review task identity differs")
                return result
            evidence = self.evidence.compare(left, right)
            con.execute(
                "INSERT INTO review_tasks VALUES(?,?,?,?,?,'pending',1,NULL,0,NULL)",
                (task, left, right, canonical(evidence), digest(evidence)),
            )
            self.store.audit(
                "review_enqueued",
                task,
                {"left": left, "right": right, "evidence": evidence["digest"]},
            )
        return self.get(task)

    def get(self, task):
        row = self.store.one("SELECT * FROM review_tasks WHERE id=?", (task,))
        if row is None:
            fail("missing", "review task not found")
        evidence = decode(row["evidence"])
        if digest(evidence) != row["digest"]:
            fail("integrity", "review evidence changed")
        return {
            **row,
            "evidence": evidence,
            "result": decode(row["result"]) if row["result"] else None,
            "fresh": self.evidence.fresh(evidence),
        }

    def claim(self, task, owner, now, seconds=300, expected_revision=1):
        identifier(owner)
        integer(now)
        integer(seconds, 1, 3600)
        integer(expected_revision, 1)
        with self.store.atomic() as con:
            row = self.get(task)
            if row["state"] != "pending" or row["revision"] != expected_revision:
                fail("conflict", "review task state changed")
            if row["owner"] and row["owner"] != owner and row["lease_until"] > now:
                fail("lease", "review task belongs to another reviewer")
            con.execute(
                "UPDATE review_tasks SET owner=?,lease_until=?,revision=revision+1 WHERE id=?",
                (owner, now + seconds, task),
            )
            self.store.audit(
                "review_claimed", task, {"owner": owner, "until": now + seconds}
            )
        return self.get(task)

    def check(self, task, owner, now, expected_revision):
        identifier(owner)
        integer(now)
        integer(expected_revision, 1)
        row = self.get(task)
        if (
            row["state"] != "pending"
            or row["owner"] != owner
            or row["lease_until"] <= now
            or row["revision"] != expected_revision
        ):
            fail("lease", "review ownership expired or revision changed")
        return row

    def refresh(self, task, owner, now, expected_revision):
        with self.store.atomic() as con:
            row = self.check(task, owner, now, expected_revision)
            evidence = self.evidence.compare(row["left_key"], row["right_key"])
            con.execute(
                "UPDATE review_tasks SET evidence=?,digest=?,revision=revision+1 WHERE id=?",
                (canonical(evidence), digest(evidence), task),
            )
            self.store.audit("review_refreshed", task, {"digest": evidence["digest"]})
        return self.get(task)

    def resolve(
        self,
        task,
        owner,
        now,
        expected_revision,
        decision,
        kind,
        expected_preview,
        reason,
    ):
        with self.store.atomic() as con:
            row = self.check(task, owner, now, expected_revision)
            if not row["fresh"]:
                fail("stale", "refresh changed source evidence before review")
            result = self.graph.decide(
                decision,
                row["left_key"],
                row["right_key"],
                kind,
                expected_preview,
                owner,
                reason,
            )
            outcome = {"decision": decision, "kind": kind, "state": result["state"]}
            con.execute(
                "UPDATE review_tasks SET state='resolved',revision=revision+1,result=? WHERE id=?",
                (canonical(outcome), task),
            )
            self.store.audit("review_resolved", task, outcome)
        return self.get(task)

    def dismiss(self, task, owner, now, expected_revision, reason):
        reason = bounded_text(reason)
        with self.store.atomic() as con:
            self.check(task, owner, now, expected_revision)
            con.execute(
                "UPDATE review_tasks SET state='dismissed',revision=revision+1,result=? WHERE id=?",
                (canonical({"reason": reason}), task),
            )
            self.store.audit(
                "review_dismissed", task, {"owner": owner, "reason": reason}
            )
        return self.get(task)

    def release(self, task, owner, now, expected_revision):
        with self.store.atomic() as con:
            self.check(task, owner, now, expected_revision)
            con.execute(
                "UPDATE review_tasks SET owner=NULL,lease_until=0,revision=revision+1 WHERE id=?",
                (task,),
            )
        return self.get(task)

    def pending(self, now, owner=None, limit=100):
        integer(now)
        integer(limit, 1, 1000)
        if owner is not None:
            identifier(owner)
        result = []
        for row in self.store.rows(
            "SELECT id FROM review_tasks WHERE state='pending' ORDER BY id"
        ):
            task = self.get(row["id"])
            if task["lease_until"] > now and task["owner"] not in {None, owner}:
                continue
            result.append(task)
        result.sort(key=lambda row: (-row["evidence"]["score"], row["id"]))
        return {
            "tasks": result[:limit],
            "eligible": len(result),
            "truncated": len(result) > limit,
        }

    def stats(self, now):
        integer(now)
        rows = self.store.rows("SELECT id FROM review_tasks ORDER BY id")
        counts = {"pending": 0, "resolved": 0, "dismissed": 0, "leased": 0, "stale": 0}
        for row in rows:
            task = self.get(row["id"])
            counts[task["state"]] += 1
            if (
                task["state"] == "pending"
                and task["owner"]
                and task["lease_until"] > now
            ):
                counts["leased"] += 1
            if task["state"] == "pending" and not task["fresh"]:
                counts["stale"] += 1
        return counts
