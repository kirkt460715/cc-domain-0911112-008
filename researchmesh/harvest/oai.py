"""OAI-PMH 协议层：命名空间、安全 XML 解析、resumptionToken 与传输。

只依赖标准库。OAI 外层固定为
``http://www.openarchives.org/OAI/2.0/``，成果映射使用
``urn:researchmesh`` 命名空间（示例场景写作 ``m:`` 前缀）。解析对前缀
不敏感，只按命名空间名匹配，因此默认命名空间与显式前缀都能处理。
"""
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from ..contracts import bounded_text, fail

OAI_NS = "http://www.openarchives.org/OAI/2.0/"
RECORD_NS = "urn:researchmesh"

MAX_PAGE_BYTES = 32_000_000
DEFAULT_TIMEOUT = 20
DAY_PATTERN = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")


class OAIError(Exception):
    """协议错误（HTTP/OAI error/XML 结构），携带 OAI 错误码（如有）。"""

    def __init__(self, message, code=None, http_status=None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status

    def record(self):
        result = {"code": self.code or "oai", "message": str(self)}
        if self.http_status is not None:
            result["http_status"] = self.http_status
        return result


def day_precision(value):
    """把 datestamp 归一到接口的日期精度（天）。

    接受 ``YYYY-MM-DD`` 或带时间分量的 ISO 8601 值；无论上游精度如何，
    采集边界与水印都只保留到天，避免偷偷使用更细的截止点。
    """
    text = bounded_text(value, 40)
    match = DAY_PATTERN.fullmatch(text) or DAY_PATTERN.match(text)
    if not match:
        fail("input", "OAI datestamp must use YYYY-MM-DD day precision", value=value)
    year, month, day = (int(part) for part in match.groups())
    if not 1 <= month <= 12 or not 1 <= day <= 31 or not 1900 <= year <= 9999:
        fail("input", "OAI datestamp contains an impossible calendar date", value=value)
    return f"{year:04d}-{month:02d}-{day:02d}"


def _secure_parse(content):
    # 标准库 ElementTree 不解析外部实体；仍显式拒绝 DOCTYPE/ENTITY，
    # 防止 billion-laughs 等实体扩张与任何外部引用。
    head = content[:2000].lstrip()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
        fail("input", "OAI payload must not contain DTD or entity declarations")
    try:
        return ET.fromstring(content)
    except ET.ParseError as exc:
        fail("input", "OAI payload is not well-formed XML", reason=str(exc))


def _namespaced(namespace, name):
    return f"{{{namespace}}}{name}"


def _child(element, namespace, name):
    return element.find(_namespaced(namespace, name))


def _children(element, namespace, name):
    return element.findall(_namespaced(namespace, name))


def _text(element):
    value = (element.text or "").strip()
    if not value:
        fail("input", "OAI element requires non-empty text", tag=element.tag)
    return value


def _metadata_contract(metadata):
    record = _child(metadata, RECORD_NS, "record")
    if record is None:
        fail("input", "OAI metadata requires an m:record element")

    def take(name, required=False, many=False):
        found = _children(record, RECORD_NS, name)
        if many:
            return [_text(element) for element in found]
        if not found:
            if required:
                fail("input", "OAI m:record missing required field", field=name)
            return None
        return _text(found[0])

    data = {}
    for name in ("title", "institution", "kind", "abstract", "country"):
        value = take(name, name in {"title", "institution", "kind"})
        if value is not None:
            data[name] = value
    year = take("year")
    if year is not None:
        if not re.fullmatch(r"[0-9]{4}", year):
            fail("input", "OAI year must be a four-digit number", value=year)
        data["year"] = int(year)
    people = take("people", many=True)
    if people:
        data["people"] = people
    tags = take("tags", many=True)
    if tags:
        data["tags"] = tags
    identifiers = {}
    container = _child(record, RECORD_NS, "identifiers")
    if container is not None:
        for entry in _children(container, RECORD_NS, "identifier"):
            namespace = entry.get("namespace")
            if namespace not in {"patent", "doi", "registry"}:
                fail("input", "OAI identifier namespace must be patent, doi or registry")
            identifiers.setdefault(namespace, []).append(_text(entry))
    if identifiers:
        data["identifiers"] = identifiers
    return data


def parse_page(content):
    """解析一页 ListRecords 响应。

    返回 ``{"entries", "token", "complete", "page_digest_input"}``。
    entries 中每条记录带稳定外部 identifier、天精度 datestamp、deleted
    标记与按现有 source_contract 清洗前的映射 data。deleted header 没有
    metadata，因此不会出现 data 字段。
    """
    if isinstance(content, str):
        content = content.encode("utf-8")
    if not isinstance(content, (bytes, bytearray)) or not content:
        fail("input", "OAI page must be bounded non-empty XML")
    if len(content) > MAX_PAGE_BYTES:
        fail("capacity", "OAI page exceeds bounded size")
    root = _secure_parse(bytes(content))
    if root.tag != _namespaced(OAI_NS, "OAI-PMH"):
        fail("input", "response root must be an OAI-PMH element")
    error = _child(root, OAI_NS, "error")
    if error is not None:
        code = error.get("code") or "OAIError"
        raise OAIError(
            "OAI endpoint returned error: " + (error.text or code).strip(), code=code
        )
    listing = _child(root, OAI_NS, "ListRecords")
    if listing is None:
        fail("input", "OAI response must contain a ListRecords element")

    entries = []
    for record in _children(listing, OAI_NS, "record"):
        header = _child(record, OAI_NS, "header")
        if header is None:
            fail("input", "OAI record requires a header")
        identifier_element = _child(header, OAI_NS, "identifier")
        datestamp_element = _child(header, OAI_NS, "datestamp")
        if identifier_element is None or datestamp_element is None:
            fail("input", "OAI header requires identifier and datestamp")
        external_id = _text(identifier_element)
        datestamp = day_precision(_text(datestamp_element))
        deleted = header.get("status") == "deleted"
        # 同一页内重复 identifier（重叠边界常见）保留为多版本，
        # 由采集管理器按页序对账，而不是在这里直接拒绝整页。
        entry = {
            "identifier": external_id,
            "datestamp": datestamp,
            "deleted": deleted,
        }
        if deleted:
            if _child(record, OAI_NS, "metadata") is not None:
                fail("input", "deleted OAI header must not carry metadata",
                     identifier=external_id)
        else:
            metadata = _child(record, OAI_NS, "metadata")
            if metadata is None:
                fail("input", "active OAI record requires metadata",
                     identifier=external_id)
            entry["data"] = _metadata_contract(metadata)
        entries.append(entry)

    token_element = _child(listing, OAI_NS, "resumptionToken")
    token = None
    if token_element is not None and token_element.text and token_element.text.strip():
        token = token_element.text.strip()
    return {
        "entries": entries,
        "token": token,
        "complete": token is None,
    }


def build_url(endpoint, token=None, from_day=None, until_day=None,
              metadata_prefix="oai_dc"):
    """构造 ListRecords 请求；携带 token 时只发送 verb 与 token。"""
    base = bounded_text(endpoint, 2000)
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        fail("input", "OAI endpoint must be an absolute http(s) URL")
    query = {"verb": "ListRecords"}
    if token is not None:
        query["resumptionToken"] = bounded_text(token, 1000)
    else:
        query["metadataPrefix"] = bounded_text(metadata_prefix, 100)
        if from_day is not None:
            query["from"] = day_precision(from_day)
        if until_day is not None:
            query["until"] = day_precision(until_day)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path,
         urllib.parse.urlencode(query), parsed.fragment)
    )


def urllib_transport(url, timeout=DEFAULT_TIMEOUT):
    """默认传输：只允许 http/https，限制体积，结构化所有失败。"""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "researchmesh-oai"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = response.read(MAX_PAGE_BYTES + 1)
            status = getattr(response, "status", response.getcode())
    except OAIError:
        raise
    except urllib.error.HTTPError as exc:
        raise OAIError(
            "OAI endpoint rejected the request: " + exc.reason,
            http_status=exc.code,
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OAIError("OAI endpoint is unreachable: " + str(exc)) from exc
    if len(content) > MAX_PAGE_BYTES:
        raise OAIError("OAI page exceeds bounded size")
    return content, status
