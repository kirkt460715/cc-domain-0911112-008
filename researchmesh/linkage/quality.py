from collections import Counter
from ..contracts import integer, digest, decode, fail, sequence, pair
from ..normalization.text import fold


class Quality:
    def __init__(self, store, sources, normalizer, candidates, graph, masters):
        self.store = store
        self.sources = sources
        self.normalizer = normalizer
        self.candidates = candidates
        self.graph = graph
        self.masters = masters

    def source_profile(self, source):
        self.sources.get(source)
        records = [row for row in self.sources.all_active() if row["source"] == source]
        missing = Counter()
        kinds = Counter()
        years = Counter()
        identities = {}
        scores = []
        for row in records:
            raw = row["raw"]
            quality = self.normalizer.completeness(raw)
            scores.append(quality["score"])
            for field, points in quality["fields"].items():
                if not points:
                    missing[field] += 1
            kinds[raw["kind"]] += 1
            if raw["year"] is not None:
                years[str(raw["year"])] += 1
            for namespace, values in row["normalized"]["identifiers"].items():
                for value in values:
                    identities.setdefault((namespace, value), []).append(row["key"])
        duplicates = [
            {"namespace": namespace, "value": value, "records": keys}
            for (namespace, value), keys in sorted(identities.items())
            if len(keys) > 1
        ]
        return {
            "source": source,
            "active_records": len(records),
            "missing_fields": dict(sorted(missing.items())),
            "kinds": dict(sorted(kinds.items())),
            "years": dict(sorted(years.items())),
            "completeness_sum": sum(scores),
            "completeness_count": len(scores),
            "duplicate_external_identifiers": duplicates,
        }

    def cluster_profile(self):
        clusters = self.graph.clusters()
        histogram = Counter(row["size"] for row in clusters)
        mixed_sources = []
        for row in clusters:
            sources = sorted({key.split(":")[0] for key in row["members"]})
            if len(sources) > 1:
                mixed_sources.append(
                    {"cluster": row["id"], "sources": sources, "size": row["size"]}
                )
        return {
            "clusters": len(clusters),
            "records": sum(row["size"] for row in clusters),
            "size_histogram": {
                str(size): count for size, count in sorted(histogram.items())
            },
            "cross_source_clusters": mixed_sources,
            "orphan_pins": self.masters.orphan_pins(),
        }

    def labeled_pairs(self, labels, threshold=80):
        sequence(labels, 1, 10000)
        integer(threshold, 0, 100)
        confusion = {
            "true_positive": 0,
            "false_positive": 0,
            "true_negative": 0,
            "false_negative": 0,
        }
        rows = []
        seen = set()
        from ..contracts import fields

        for label in labels:
            fields(label, {"left", "right", "same"}, {"left", "right", "same"})
            identity = pair(label["left"], label["right"])
            if identity in seen or not isinstance(label["same"], bool):
                fail("input", "labels require unique pairs and boolean truth")
            seen.add(identity)
            report = self.candidates.evidence.compare(*identity)
            predicted = report["score"] >= threshold and not report["conflicts"]
            truth = label["same"]
            category = ("true_" if predicted == truth else "false_") + (
                "positive" if predicted else "negative"
            )
            confusion[category] += 1
            rows.append(
                {
                    "left": identity[0],
                    "right": identity[1],
                    "truth": truth,
                    "predicted": predicted,
                    "score": report["score"],
                    "category": category,
                    "conflicts": report["conflicts"],
                }
            )
        tp, fp, fn = (
            confusion["true_positive"],
            confusion["false_positive"],
            confusion["false_negative"],
        )
        from fractions import Fraction

        return {
            "threshold": threshold,
            "confusion": confusion,
            "rows": rows,
            "precision": str(Fraction(tp, tp + fp)) if tp + fp else None,
            "recall": str(Fraction(tp, tp + fn)) if tp + fn else None,
            "basis": "caller_supplied_local_labels_not_automatic_ground_truth",
        }

    def threshold_sweep(self, labels, thresholds):
        sequence(thresholds, 1, 101)
        if len(set(thresholds)) != len(thresholds):
            fail("input", "duplicate threshold")
        reports = []
        for threshold in sorted(thresholds):
            report = self.labeled_pairs(labels, threshold)
            reports.append(
                {
                    key: report[key]
                    for key in ["threshold", "confusion", "precision", "recall"]
                }
            )
        return {"reports": reports, "labels_digest": digest(labels)}

    def candidate_recall(self, labels, maximum_block=500):
        sequence(labels, 1, 10000)
        results = []
        hits = 0
        positives = 0
        for label in labels:
            if label.get("same") is not True:
                continue
            left, right = pair(label["left"], label["right"])
            shared = self.store.rows(
                "SELECT a.block FROM blocks a JOIN blocks b ON a.block=b.block WHERE a.record=? AND b.record=? ORDER BY a.block",
                (left, right),
            )
            eligible = []
            for row in shared:
                size = self.store.one(
                    "SELECT COUNT(*) AS n FROM blocks WHERE block=?", (row["block"],)
                )["n"]
                if size <= maximum_block:
                    eligible.append(row["block"])
            positives += 1
            hits += bool(eligible)
            results.append(
                {
                    "left": left,
                    "right": right,
                    "retrievable": bool(eligible),
                    "blocks": eligible,
                }
            )
        from fractions import Fraction

        return {
            "positive_pairs": positives,
            "retrievable_pairs": hits,
            "recall": str(Fraction(hits, positives)) if positives else None,
            "rows": results,
        }

    def identifier_collisions(self):
        occurrences = {}
        for record in self.sources.all_active():
            for namespace, values in record["normalized"]["identifiers"].items():
                for value in values:
                    occurrences.setdefault((namespace, value), []).append(record["key"])
        result = []
        for (namespace, value), keys in sorted(occurrences.items()):
            clusters = sorted({self.graph.cluster(key) for key in keys})
            if len(clusters) > 1:
                result.append(
                    {
                        "namespace": namespace,
                        "value": value,
                        "records": keys,
                        "clusters": clusters,
                        "status": "unresolved_identity_collision",
                    }
                )
        return result

    def verify(self):
        self.sources.require_index()
        graph = self.graph.verify()
        index = self.candidates.coverage()
        if not index["ok"]:
            fail(
                "integrity",
                "candidate index does not cover active records",
                report=index,
            )
        for record in self.sources.all_active():
            expected = self.normalizer.normalize(record["raw"])
            if digest(expected) != record["norm_digest"]:
                fail(
                    "integrity",
                    "normalized record differs from raw data and dictionary",
                    record=record["key"],
                )
            actual = [
                row["block"]
                for row in self.store.rows(
                    "SELECT block FROM blocks WHERE record=? ORDER BY block",
                    (record["key"],),
                )
            ]
            if actual != self.normalizer.block_keys(expected):
                fail(
                    "integrity",
                    "blocking index differs from normalized record",
                    record=record["key"],
                )
        return {
            "verified": True,
            "graph": graph,
            "index": index,
            "audit": self.store.verify_audit(),
        }
