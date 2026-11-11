from fractions import Fraction
from ..contracts import pair, digest, fail


def jaccard(left, right):
    a, b = set(left), set(right)
    if not a and not b:
        return Fraction(0)
    return Fraction(len(a & b), len(a | b))


def bigrams(value):
    value = value.replace(" ", "")
    return (
        {value[i : i + 2] for i in range(len(value) - 1)}
        if len(value) > 1
        else set(value)
    )


def conflict_reasons(left, right):
    reasons = []
    if left["kind"] != right["kind"]:
        reasons.append("kind_conflict")
    for namespace in sorted(left["identifiers"].keys() & right["identifiers"].keys()):
        a, b = set(left["identifiers"][namespace]), set(right["identifiers"][namespace])
        if a and b and not a & b:
            reasons.append("identifier_conflict:" + namespace)
    return reasons


class Evidence:
    def __init__(self, sources):
        self.sources = sources

    def compare(self, left, right):
        self.sources.require_index()
        left, right = pair(left, right)
        first = self.sources.record(left)
        second = self.sources.record(right)
        a, b = first["normalized"], second["normalized"]
        details = []
        score = 0
        common_ids = []
        for namespace in sorted(a["identifiers"].keys() & b["identifiers"].keys()):
            common = sorted(
                set(a["identifiers"][namespace]) & set(b["identifiers"][namespace])
            )
            for value in common:
                common_ids.append({"namespace": namespace, "value": value})
        if common_ids:
            score += 70
            details.append(
                {"feature": "shared_identifier", "points": 70, "values": common_ids}
            )
        title_words = jaccard(a["title_tokens"], b["title_tokens"])
        title_chars = jaccard(bigrams(a["title"]), bigrams(b["title"]))
        title_similarity = max(title_words, title_chars)
        title_points = int(title_similarity * 35)
        score += title_points
        details.append(
            {
                "feature": "title_similarity",
                "points": title_points,
                "ratio": str(title_similarity),
                "shared_tokens": sorted(
                    set(a["title_tokens"]) & set(b["title_tokens"])
                ),
            }
        )
        if a["institution_key"] == b["institution_key"]:
            score += 20
            details.append(
                {
                    "feature": "institution_exact",
                    "points": 20,
                    "value": a["institution"],
                }
            )
        people = sorted(set(a["people"]) & set(b["people"]))
        if people:
            score += 10
            details.append({"feature": "shared_people", "points": 10, "values": people})
        tags = sorted(set(a["tags"]) & set(b["tags"]))
        if tags:
            score += 5
            details.append({"feature": "shared_tags", "points": 5, "values": tags})
        if a["year"] is not None and b["year"] is not None:
            delta = abs(a["year"] - b["year"])
            points = 5 if delta == 0 else (2 if delta == 1 else -10)
            score += points
            details.append(
                {"feature": "year_distance", "points": points, "years": delta}
            )
        conflicts = conflict_reasons(a, b)
        bounded = max(0, min(100, score))
        recommendation = (
            "reject"
            if conflicts
            else (
                "strong" if bounded >= 80 else ("review" if bounded >= 45 else "weak")
            )
        )
        evidence = {
            "left": left,
            "right": right,
            "score": bounded,
            "recommendation": recommendation,
            "conflicts": conflicts,
            "features": details,
            "baseline": {
                left: {
                    "revision": first["revision"],
                    "norm_digest": first["norm_digest"],
                },
                right: {
                    "revision": second["revision"],
                    "norm_digest": second["norm_digest"],
                },
            },
            "algorithm": "explainable-v1",
        }
        return {**evidence, "digest": digest(evidence)}

    def fresh(self, evidence):
        for key, baseline in evidence["baseline"].items():
            row = self.sources.store.one(
                "SELECT active,revision,norm_digest FROM records WHERE key=?", (key,)
            )
            if (
                row is None
                or not row["active"]
                or row["revision"] != baseline["revision"]
                or row["norm_digest"] != baseline["norm_digest"]
            ):
                return False
        return True

    def explain(self, left, right):
        report = self.compare(left, right)
        report["summary"] = {
            "positive_features": [
                row["feature"] for row in report["features"] if row["points"] > 0
            ],
            "negative_features": [
                row["feature"] for row in report["features"] if row["points"] < 0
            ],
            "merge_permitted_by_pair_rules": not report["conflicts"],
            "requires_human_decision": True,
        }
        return report
