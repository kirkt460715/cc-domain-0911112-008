import base64
from ..contracts import bounded_text, integer, canonical, decode, digest, fail
from ..normalization.text import fold, tokens


class PublishedCatalogue:
    def __init__(self, releases):
        self.releases = releases

    def search(
        self,
        release,
        query="",
        kind=None,
        institution=None,
        country=None,
        year_from=None,
        year_to=None,
        tag=None,
        limit=50,
        cursor=None,
    ):
        snapshot = self.releases.get(release)
        integer(limit, 1, 1000)
        query = bounded_text(query, 1000, True)
        if kind is not None and kind not in {
            "patent",
            "prototype",
            "process",
            "dataset",
            "software",
        }:
            fail("input", "unknown achievement kind")
        if year_from is not None:
            integer(year_from, 1900, 2200)
        if year_to is not None:
            integer(year_to, 1900, 2200)
        if year_from is not None and year_to is not None and year_from > year_to:
            fail("input", "year interval is reversed")
        institution = fold(institution) if institution is not None else None
        tag = fold(tag) if tag is not None else None
        country = bounded_text(country, 2).upper() if country is not None else None
        filters = {
            "query": fold(query),
            "kind": kind,
            "institution": institution,
            "country": country,
            "year_from": year_from,
            "year_to": year_to,
            "tag": tag,
        }
        filter_digest = digest(filters)
        after = None
        if cursor is not None:
            if not isinstance(cursor, str) or len(cursor) > 2000:
                fail("cursor", "invalid catalogue cursor")
            try:
                token = decode(base64.urlsafe_b64decode(cursor.encode()).decode())
            except Exception as exc:
                from ..contracts import MeshError

                if isinstance(exc, MeshError):
                    raise
                fail("cursor", "malformed catalogue cursor")
            if not isinstance(token, dict) or set(token) != {
                "release_digest",
                "filter_digest",
                "last_score",
                "last_id",
            }:
                fail("cursor", "cursor fields differ")
            if (
                token["release_digest"] != snapshot["digest"]
                or token["filter_digest"] != filter_digest
            ):
                fail("cursor", "cursor belongs to another release or filter")
            integer(token["last_score"])
            if not isinstance(token["last_id"], str):
                fail("cursor", "invalid cursor identity")
            after = (-token["last_score"], token["last_id"])
        terms = set(tokens(query))
        matches = []
        for master in snapshot["payload"]["masters"]:
            data = master["data"]
            if kind is not None and data["kind"] != kind:
                continue
            if (
                institution is not None
                and fold(data["institution"] or "") != institution
            ):
                continue
            if country is not None and data["country"] != country:
                continue
            year = data["year"]
            if year_from is not None and (year is None or year < year_from):
                continue
            if year_to is not None and (year is None or year > year_to):
                continue
            if tag is not None and tag not in data["tags"]:
                continue
            title_tokens = set(tokens(data["title"] or ""))
            abstract_tokens = set(tokens(data["abstract"] or ""))
            tag_tokens = set(value for entry in data["tags"] for value in tokens(entry))
            searchable = title_tokens | abstract_tokens | tag_tokens
            if terms and not terms <= searchable:
                continue
            score = (
                3 * len(terms & title_tokens)
                + len(terms & abstract_tokens)
                + 2 * len(terms & tag_tokens)
            )
            matches.append(
                {
                    "id": master["id"],
                    "score": score,
                    "data": data,
                    "members": master["members"],
                    "master_digest": master["digest"],
                }
            )
        matches.sort(key=lambda row: (-row["score"], row["id"]))
        total = len(matches)
        facets = self.facets(matches)
        if after is not None:
            matches = [row for row in matches if (-row["score"], row["id"]) > after]
        page = matches[:limit]
        following = None
        if len(matches) > limit:
            last = page[-1]
            token = {
                "release_digest": snapshot["digest"],
                "filter_digest": filter_digest,
                "last_score": last["score"],
                "last_id": last["id"],
            }
            following = base64.urlsafe_b64encode(canonical(token).encode()).decode()
        return {
            "release": release,
            "release_digest": snapshot["digest"],
            "rows": page,
            "total": total,
            "next": following,
            "facets": facets,
        }

    @staticmethod
    def facets(rows):
        from collections import Counter

        kinds = Counter()
        countries = Counter()
        years = Counter()
        for row in rows:
            data = row["data"]
            kinds[data["kind"]] += 1
            if data["country"]:
                countries[data["country"]] += 1
            if data["year"] is not None:
                years[str(data["year"])] += 1
        return {
            "kinds": dict(sorted(kinds.items())),
            "countries": dict(sorted(countries.items())),
            "years": dict(sorted(years.items())),
        }

    def external(self, release, namespace, value):
        from ..normalization.text import normalize_external

        normalized = normalize_external(namespace, value)
        snapshot = self.releases.get(release)
        matches = [
            master
            for master in snapshot["payload"]["masters"]
            if normalized in master["data"]["identifiers"].get(namespace, [])
        ]
        return {
            "release": release,
            "namespace": namespace,
            "value": normalized,
            "matches": matches,
            "ambiguous": len(matches) > 1,
        }

    def source_record(self, release, key):
        from ..contracts import split_key

        split_key(key)
        snapshot = self.releases.get(release)
        master = next(
            (
                master
                for master in snapshot["payload"]["masters"]
                if key in master["members"]
            ),
            None,
        )
        if master is None:
            fail("missing", "record is not present in this release")
        return {"release": release, "record": key, "master": master}

    def csv(self, release, query="", limit=1000):
        import csv
        import io

        result = self.search(release, query=query, limit=limit)
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(
            ["cluster", "title", "institution", "kind", "year", "country", "members"]
        )

        def safe(value):
            value = "" if value is None else str(value)
            return (
                "'" + value
                if value.startswith(("=", "+", "-", "@", "\t", "\r"))
                else value
            )

        for row in result["rows"]:
            writer.writerow(
                [
                    safe(row["id"]),
                    safe(row["data"]["title"]),
                    safe(row["data"]["institution"]),
                    row["data"]["kind"],
                    row["data"]["year"],
                    row["data"]["country"],
                    len(row["members"]),
                ]
            )
        return {
            "content": output.getvalue(),
            "rows": len(result["rows"]),
            "truncated": result["next"] is not None,
            "release_digest": result["release_digest"],
        }
