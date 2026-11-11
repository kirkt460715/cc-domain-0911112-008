import re
import unicodedata
from ..contracts import bounded_text, fail, source_contract, digest

STOPWORDS = frozenset(
    {"the", "a", "an", "of", "and", "for", "with", "in", "on", "to", "及", "和", "的"}
)


def fold(value):
    value = bounded_text(value, 10000, True)
    value = unicodedata.normalize("NFKC", value).casefold()
    result = []
    for character in value:
        category = unicodedata.category(character)
        if category.startswith(("L", "N")):
            result.append(character)
        else:
            result.append(" ")
    return " ".join("".join(result).split())


def tokens(value):
    normalized = fold(value)
    result = set()
    for word in normalized.split():
        if word in STOPWORDS:
            continue
        if re.fullmatch(r"[\u3400-\u9fff]+", word):
            if len(word) == 1:
                result.add(word)
            else:
                result.update(word[index : index + 2] for index in range(len(word) - 1))
        else:
            result.add(word)
    return sorted(result)


def compact(value):
    return fold(value).replace(" ", "")


def normalize_external(namespace, value):
    value = unicodedata.normalize("NFKC", bounded_text(value, 200)).strip()
    if namespace == "doi":
        value = value.casefold()
        for prefix in ["https://doi.org/", "http://doi.org/", "doi:"]:
            if value.startswith(prefix):
                value = value[len(prefix) :]
                break
        if not re.fullmatch(r"10\.[0-9]{4,9}/[^\s]+", value):
            fail("identifier", "invalid DOI syntax")
        return value
    if namespace == "patent":
        value = re.sub(r"[\s.-]", "", value).upper()
        if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{4,40}", value):
            fail("identifier", "invalid patent identifier syntax")
        return value
    if namespace == "registry":
        value = value.casefold().strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.:/-]{2,100}", value):
            fail("identifier", "invalid registry identifier syntax")
        return value
    fail("identifier", "unknown identifier namespace")


class Normalizer:
    def __init__(self, dictionary):
        self.dictionary = dictionary

    def normalize(self, data):
        raw = source_contract(data)
        institution = self.dictionary.resolve("institution", fold(raw["institution"]))
        normalized = {
            "title": fold(raw["title"]),
            "title_tokens": tokens(raw["title"]),
            "institution": institution,
            "institution_key": compact(institution),
            "kind": raw["kind"],
            "year": raw["year"],
            "country": raw["country"],
            "people": sorted(set(fold(value) for value in raw["people"])),
            "tags": sorted(
                set(
                    self.dictionary.resolve("tag", fold(value)) for value in raw["tags"]
                )
            ),
            "abstract_tokens": tokens(raw["abstract"]),
            "identifiers": {},
        }
        if not normalized["title"] or not normalized["institution"]:
            fail("input", "title and institution must contain letters or numbers")
        for namespace, values in raw["identifiers"].items():
            normalized["identifiers"][namespace] = sorted(
                set(normalize_external(namespace, value) for value in values)
            )
        return normalized

    def block_keys(self, normalized):
        result = set()
        for namespace, values in normalized["identifiers"].items():
            for value in values:
                result.add("id:" + namespace + ":" + value)
        title = normalized["title"]
        if title:
            result.add("title:" + digest(title)[:24])
        institution = normalized["institution_key"]
        for token in normalized["title_tokens"]:
            if len(token) >= 2:
                result.add("word:" + digest([institution, token])[:24])
        for person in normalized["people"]:
            if normalized["year"] is not None:
                result.add("person-year:" + digest([person, normalized["year"]])[:24])
        return sorted(result)

    def completeness(self, raw):
        score = 0
        contributions = {}
        weights = {
            "title": 10,
            "institution": 10,
            "kind": 5,
            "year": 5,
            "country": 2,
            "people": 4,
            "tags": 3,
            "abstract": 6,
            "identifiers": 10,
        }
        for name, weight in weights.items():
            value = raw.get(name)
            if isinstance(value, dict):
                present = any(value.values())
            else:
                present = bool(value)
            contributions[name] = weight if present else 0
            score += contributions[name]
        return {
            "score": score,
            "maximum": sum(weights.values()),
            "fields": contributions,
        }
