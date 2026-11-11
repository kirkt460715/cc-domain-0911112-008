from ..contracts import integer, pair, digest, fail


class Candidates:
    def __init__(self, store, sources, evidence):
        self.store = store
        self.sources = sources
        self.evidence = evidence

    def for_record(self, key, limit=100, maximum_block=500, include_same_source=False):
        self.sources.require_index()
        record = self.sources.record(key)
        integer(limit, 1, 1000)
        integer(maximum_block, 2, 10000)
        keys = self.store.rows(
            "SELECT block FROM blocks WHERE record=? ORDER BY block", (key,)
        )
        matches = {}
        skipped = []
        for row in keys:
            peers = self.store.rows(
                "SELECT record FROM blocks WHERE block=? ORDER BY record LIMIT ?",
                (row["block"], maximum_block + 1),
            )
            if len(peers) > maximum_block:
                skipped.append(
                    {
                        "block": row["block"],
                        "reason": "capacity",
                        "limit": maximum_block,
                    }
                )
                continue
            for peer in peers:
                other = peer["record"]
                if other == key:
                    continue
                if (
                    not include_same_source
                    and other.split(":", 1)[0] == record["source"]
                ):
                    continue
                matches.setdefault(other, []).append(row["block"])
        reports = []
        for other, shared in sorted(matches.items()):
            report = self.evidence.compare(key, other)
            report["shared_blocks"] = shared
            reports.append(report)
        reports.sort(key=lambda row: (-row["score"], row["left"], row["right"]))
        return {
            "record": key,
            "candidates": reports[:limit],
            "eligible": len(reports),
            "truncated": len(reports) > limit,
            "skipped_blocks": skipped,
            "complete_search": not skipped and len(reports) <= limit,
        }

    def batch(self, limit=1000, minimum_score=45, maximum_block=500, cross_source=True):
        self.sources.require_index()
        integer(limit, 1, 10000)
        integer(minimum_score, 0, 100)
        integer(maximum_block, 2, 10000)
        pairs = set()
        skipped = []
        for row in self.store.rows(
            "SELECT block,COUNT(*) AS count FROM blocks GROUP BY block ORDER BY block"
        ):
            if row["count"] > maximum_block:
                skipped.append({"block": row["block"], "count": row["count"]})
                continue
            keys = [
                item["record"]
                for item in self.store.rows(
                    "SELECT record FROM blocks WHERE block=? ORDER BY record",
                    (row["block"],),
                )
            ]
            for index, left in enumerate(keys):
                for right in keys[index + 1 :]:
                    if cross_source and left.split(":")[0] == right.split(":")[0]:
                        continue
                    pairs.add((left, right))
                    if len(pairs) > 100000:
                        fail(
                            "capacity",
                            "candidate pair budget exceeded; narrow source snapshots",
                        )
        reports = []
        for left, right in sorted(pairs):
            report = self.evidence.compare(left, right)
            if report["score"] >= minimum_score:
                reports.append(report)
        reports.sort(key=lambda row: (-row["score"], row["left"], row["right"]))
        return {
            "candidates": reports[:limit],
            "eligible": len(reports),
            "examined_pairs": len(pairs),
            "truncated": len(reports) > limit,
            "skipped_blocks": skipped,
            "data_revision": self.store.revision("data"),
            "dictionary_revision": self.store.revision("dictionary"),
        }

    def index_stats(self):
        rows = self.store.rows(
            "SELECT block,COUNT(*) AS count FROM blocks GROUP BY block ORDER BY count DESC,block"
        )
        histogram = {}
        for row in rows:
            size = row["count"]
            bucket = (
                "singleton"
                if size == 1
                else (
                    "2-10" if size <= 10 else ("11-100" if size <= 100 else "over-100")
                )
            )
            histogram[bucket] = histogram.get(bucket, 0) + 1
        return {
            "blocks": len(rows),
            "memberships": sum(row["count"] for row in rows),
            "histogram": histogram,
            "largest": rows[:20],
            "active_records": self.store.one(
                "SELECT COUNT(*) AS n FROM records WHERE active=1"
            )["n"],
        }

    def coverage(self):
        missing = self.store.rows(
            "SELECT r.key FROM records r LEFT JOIN blocks b ON b.record=r.key WHERE r.active=1 GROUP BY r.key HAVING COUNT(b.block)=0"
        )
        orphan = self.store.rows(
            "SELECT DISTINCT b.record FROM blocks b LEFT JOIN records r ON r.key=b.record WHERE r.key IS NULL OR r.active=0"
        )
        return {
            "ok": not missing and not orphan,
            "unindexed": [r["key"] for r in missing],
            "orphan_index": [r["record"] for r in orphan],
        }
