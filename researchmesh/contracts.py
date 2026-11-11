import json
import re
import hashlib
from datetime import date


class MeshError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.details = details

    def record(self):
        return {"code": self.code, "message": str(self), "details": self.details}


def fail(code, message, **details):
    raise MeshError(code, message, **details)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value
    ):
        fail("input", "invalid local identifier")
    return value


def record_key(source, local_id):
    return identifier(source) + ":" + identifier(local_id)


def split_key(value):
    if not isinstance(value, str) or value.count(":") != 1:
        fail("input", "record key must be source:id")
    source, local_id = value.split(":")
    identifier(source)
    identifier(local_id)
    return source, local_id


def bounded_text(value, limit=1000, optional=False):
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        fail("input", "bounded text required")
    value = value.strip()
    if not optional and not value:
        fail("input", "nonempty text required")
    return value


def integer(value, minimum=0, maximum=2**53 - 1):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        fail("input", "bounded integer required")
    return value


def fields(value, allowed, required=()):
    if not isinstance(value, dict):
        fail("input", "object required")
    missing = set(required) - value.keys()
    extra = value.keys() - set(allowed)
    if missing or extra:
        fail(
            "input",
            "unexpected object fields",
            missing=sorted(missing),
            extra=sorted(extra),
        )
    return value


def sequence(value, minimum=0, maximum=10000):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        fail("input", "bounded list required")
    return value


def canonical(value):
    try:
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (ValueError, TypeError) as exc:
        fail("input", "value cannot be canonicalized", reason=str(exc))


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def decode(content):
    def pairs(values):
        result = {}
        for name, value in values:
            if name in result:
                fail("input", "duplicate JSON key", key=name)
            result[name] = value
        return result

    def constant(value):
        fail("input", "nonfinite JSON token", token=value)

    try:
        return json.loads(content, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError) as exc:
        fail("input", "malformed JSON", reason=str(exc))


def pair(left, right):
    split_key(left)
    split_key(right)
    if left == right:
        fail("input", "pair requires distinct records")
    return tuple(sorted([left, right]))


def source_contract(data):
    allowed = {
        "title",
        "institution",
        "kind",
        "year",
        "identifiers",
        "people",
        "tags",
        "abstract",
        "country",
    }
    fields(data, allowed, {"title", "institution", "kind"})
    title = bounded_text(data["title"], 1000)
    institution = bounded_text(data["institution"], 500)
    kind = data["kind"]
    if kind not in {"patent", "prototype", "process", "dataset", "software"}:
        fail("input", "unsupported achievement kind")
    year = data.get("year")
    if year is not None:
        integer(year, 1900, 2200)
    country = bounded_text(data.get("country", ""), 2, True).upper()
    if country and not re.fullmatch("[A-Z]{2}", country):
        fail("input", "country must use two letters")
    people = sorted(
        set(
            bounded_text(value, 200)
            for value in sequence(data.get("people", []), 0, 100)
        )
    )
    tags = sorted(
        set(
            bounded_text(value, 100) for value in sequence(data.get("tags", []), 0, 100)
        )
    )
    identifiers = data.get("identifiers", {})
    fields(identifiers, {"patent", "doi", "registry"})
    normalized = {}
    for namespace, values in identifiers.items():
        normalized[namespace] = sorted(
            set(bounded_text(value, 200) for value in sequence(values, 0, 20))
        )
    return {
        "title": title,
        "institution": institution,
        "kind": kind,
        "year": year,
        "country": country,
        "people": people,
        "tags": tags,
        "identifiers": normalized,
        "abstract": bounded_text(data.get("abstract", ""), 10000, True),
    }
