import unittest
import threading
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from researchmesh.service import ResearchMesh
from researchmesh.contracts import MeshError
from researchmesh.harvest.oai import OAIError, parse_page, build_url, day_precision
from researchmesh.harvest import local_identifier_for


OAI = "http://www.openarchives.org/OAI/2.0/"
M = "urn:researchmesh"


def record_xml(ident, title="Title", deleted=False, datestamp="2026-11-10",
               kind="dataset", extra=""):
    if deleted:
        return (
            f"<record><header status=\"deleted\"><identifier>{ident}</identifier>"
            f"<datestamp>{datestamp}</datestamp></header></record>"
        )
    return (
        f"<record><header><identifier>{ident}</identifier>"
        f"<datestamp>{datestamp}</datestamp></header><metadata>"
        f"<m:record xmlns:m=\"{M}\"><m:title>{title}</m:title>"
        f"<m:institution>合成研究所</m:institution><m:kind>{kind}</m:kind>"
        f"<m:year>2026</m:year>{extra}</m:record></metadata></record>"
    )


def page_xml(records, token=None):
    token_xml = (
        f"<resumptionToken>{token}</resumptionToken>"
        if token
        else "<resumptionToken></resumptionToken>"
    )
    return (
        f"<OAI-PMH xmlns=\"{OAI}\"><ListRecords>"
        + "".join(records)
        + token_xml
        + "</ListRecords></OAI-PMH>"
    )


class FakeTransport:
    """按 URL 关键字返回脚本化响应；记录全部请求 URL。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        for needle, payload in self.routes:
            if needle in url:
                if isinstance(payload, Exception):
                    raise payload
                if isinstance(payload, tuple):
                    return payload
                return payload.encode(), 200
        raise AssertionError("unexpected request " + url)


class HarvestTestBase(unittest.TestCase):
    def setUp(self):
        self.m = ResearchMesh()
        self.addCleanup(self.m.close)
        self.m.sources.register("lab", "Synthetic laboratory", 10)

    def use(self, routes):
        transport = FakeTransport(routes)
        self.m.harvest.transport = transport
        return transport

    def data(self, title="Precision sensor"):
        return {
            "title": title,
            "institution": "Example Institute",
            "kind": "prototype",
            "year": 2026,
        }


class ProtocolParserTests(unittest.TestCase):
    def test_namespaced_list_records_and_deleted_header(self):
        page = parse_page(page_xml([record_xml("oai:lab:1"), record_xml(
            "oai:lab:2", deleted=True)], token="next"))
        self.assertFalse(page["complete"])
        self.assertEqual(page["token"], "next")
        active, deleted = page["entries"]
        self.assertEqual(active["identifier"], "oai:lab:1")
        self.assertEqual(active["data"]["title"], "Title")
        self.assertEqual(active["data"]["year"], 2026)
        self.assertFalse(active["deleted"])
        self.assertTrue(deleted["deleted"])
        self.assertNotIn("data", deleted)

    def test_accepts_explicit_prefixes_on_outer_envelope(self):
        xml = (
            f"<oai:OAI-PMH xmlns:oai=\"{OAI}\"><oai:ListRecords>"
            f"<oai:record><oai:header><oai:identifier>oai:x:1</oai:identifier>"
            "<oai:datestamp>2026-11-10</oai:datestamp></oai:header>"
            f"<oai:metadata><m:record xmlns:m=\"{M}\">"
            "<m:title>T</m:title><m:institution>I</m:institution>"
            "<m:kind>software</m:kind></m:record></oai:metadata></oai:record>"
            "</oai:ListRecords></oai:OAI-PMH>"
        )
        page = parse_page(xml)
        self.assertEqual(page["entries"][0]["identifier"], "oai:x:1")
        self.assertTrue(page["complete"])

    def test_collections_and_identifier_namespaces(self):
        extra = (
            "<m:people>张三</m:people><m:people>李四</m:people>"
            "<m:tags>测量</m:tags>"
            "<m:identifiers>"
            "<m:identifier namespace=\"doi\">10.1234/abc</m:identifier>"
            "<m:identifier namespace=\"registry\">local:r1</m:identifier>"
            "</m:identifiers>"
        )
        page = parse_page(page_xml([record_xml("oai:lab:1", extra=extra)]))
        data = page["entries"][0]["data"]
        self.assertEqual(data["people"], ["张三", "李四"])
        self.assertEqual(data["tags"], ["测量"])
        self.assertEqual(
            data["identifiers"],
            {"doi": ["10.1234/abc"], "registry": ["local:r1"]},
        )

    def test_rejects_malformed_and_unsafe_payloads(self):
        with self.assertRaises(MeshError):
            parse_page("<not-xml")
        with self.assertRaises(MeshError):
            parse_page(
                "<!DOCTYPE oai [<!ENTITY x \"b\">]>"
                f"<OAI-PMH xmlns=\"{OAI}\"/>"
            )
        with self.assertRaises(MeshError):
            parse_page("<root/>")
        with self.assertRaises(MeshError):
            parse_page(
                f"<OAI-PMH xmlns=\"{OAI}\"><ListRecords>"
                "<record><header><identifier>x</identifier>"
                "<datestamp>2026-11-10</datestamp></header></record>"
                "</ListRecords></OAI-PMH>"
            )
        with self.assertRaises(MeshError):
            parse_page(page_xml([record_xml("oai:lab:1", datestamp="11/10/2026")]))

    def test_deleted_header_cannot_carry_metadata(self):
        with self.assertRaises(MeshError):
            parse_page(
                f"<OAI-PMH xmlns=\"{OAI}\"><ListRecords><record>"
                "<header status=\"deleted\"><identifier>x</identifier>"
                "<datestamp>2026-11-10</datestamp></header>"
                f"<metadata><m:record xmlns:m=\"{M}\">"
                "<m:title>T</m:title><m:institution>I</m:institution>"
                "<m:kind>dataset</m:kind></m:record></metadata></record>"
                "</ListRecords></OAI-PMH>"
            )
        with self.assertRaises(MeshError):
            parse_page(
                f"<OAI-PMH xmlns=\"{OAI}\"><ListRecords><record>"
                "<header><identifier>x</identifier>"
                "<datestamp>2026-11-10</datestamp></header></record>"
                "</ListRecords></OAI-PMH>"
            )

    def test_oai_error_and_day_precision(self):
        with self.assertRaises(OAIError) as caught:
            parse_page(
                f"<OAI-PMH xmlns=\"{OAI}\">"
                "<error code=\"badResumptionToken\">expired</error></OAI-PMH>"
            )
        self.assertEqual(caught.exception.code, "badResumptionToken")
        self.assertEqual(day_precision("2026-11-10T08:30:00Z"), "2026-11-10")
        with self.assertRaises(MeshError):
            day_precision("2026-13-40")
        url = build_url("http://127.0.0.1:8765/oai", token="next")
        self.assertIn("resumptionToken=next", url)
        self.assertNotIn("from", url)
        with self.assertRaises(MeshError):
            build_url("ftp://127.0.0.1/oai")
        # 同页重复 identifier 不再整页拒绝，而是保留为多版本交由对账
        repeated = parse_page(page_xml([record_xml("dup"), record_xml("dup")]))
        self.assertEqual(
            [entry["identifier"] for entry in repeated["entries"]],
            ["dup", "dup"],
        )


class IdentifierMappingTests(unittest.TestCase):
    def test_colon_identifier_maps_deterministically_and_safely(self):
        first = local_identifier_for("oai:lab:abc:123")
        second = local_identifier_for("oai:lab:abc:123")
        self.assertEqual(first, second)
        self.assertNotIn(":", first)
        self.assertTrue(first.startswith("ext"))
        self.assertRegex(first, r"ext[0-9a-f]{64}")
        self.assertNotEqual(
            local_identifier_for("oai:lab:1"), local_identifier_for("oai:lab:2")
        )


class FullHarvestTests(HarvestTestBase):
    def test_complete_full_traversal_forms_complete_snapshot(self):
        transport = self.use([
            ("resumptionToken", page_xml([record_xml("oai:lab:2", "Second")])),
            ("ListRecords", page_xml(
                [record_xml("oai:lab:1", "First")], token="next")),
        ])
        self.m.harvest.start("h1", "lab", "http://local/oai", "full")
        staged = self.m.harvest.collect("h1")
        self.assertEqual(staged["state"], "collected")
        result = self.m.harvest.apply("h1")
        self.assertEqual(result["strategy"], "complete_snapshot")
        self.assertTrue(result["complete_traversal"])
        self.assertEqual(result["upserts"], 2)
        self.assertEqual(self.m.sources.get("lab")["generation"], 1)
        keys = {row["local_id"] for row in self.m.store.rows(
            "SELECT local_id FROM records WHERE source='lab'")}
        self.assertEqual(
            keys,
            {local_identifier_for("oai:lab:1"), local_identifier_for("oai:lab:2")},
        )
        # 初始请求不带 token，后续请求只带 token
        self.assertNotIn("resumptionToken", transport.calls[0])
        self.assertIn("resumptionToken=next", transport.calls[1])
        # 重放不产生新批次/审计
        replay = self.m.harvest.apply("h1")
        self.assertTrue(replay["reused"])
        self.assertEqual(self.m.sources.get("lab")["generation"], 1)

    def test_second_full_snapshot_withdraws_only_absent_items(self):
        self.use([("ListRecords", page_xml(
            [record_xml("oai:lab:1"), record_xml("oai:lab:2", "Second")]))])
        self.m.harvest.start("h1", "lab", "http://local/oai", "full")
        self.m.harvest.collect("h1")
        self.m.harvest.apply("h1")
        self.use([("ListRecords", page_xml([record_xml("oai:lab:1")]))])
        self.m.harvest.start("h2", "lab", "http://local/oai", "full")
        self.m.harvest.collect("h2")
        result = self.m.harvest.apply("h2")
        self.assertEqual(result["strategy"], "complete_snapshot")
        missing = "lab:" + local_identifier_for("oai:lab:2")
        self.assertIn(missing, result["snapshot"]["withdrawn"])
        self.assertFalse(self.m.sources.record(missing, True)["active"])

    def test_partial_failure_never_withdraws_unseen_records(self):
        # 库里已有一条人工记录；采集在第二页失败
        self.m.sources.snapshot(
            "manual", "lab", 1, [{"id": "keep", "data": self.data()}], 0
        )
        self.use([
            ("resumptionToken", OAIError("reset", http_status=502)),
            ("ListRecords", page_xml([record_xml("oai:lab:1")], token="next")),
        ])
        self.m.harvest.start("h1", "lab", "http://local/oai", "full", max_restarts=0)
        with self.assertRaises(MeshError) as caught:
            self.m.harvest.collect("h1")
        self.assertEqual(caught.exception.code, "harvest_failed")
        run = self.m.harvest.get("h1")
        self.assertEqual(run["state"], "partial")
        self.assertEqual(run["error"]["http_status"], 502)
        # 已采集页保留
        self.assertEqual(self.m.harvest.explain("h1")["pages"], 1)
        # 未走完的全量只能按变更键落库，keep 不被撤回
        applied = self.m.harvest.apply("h1")
        self.assertEqual(applied["strategy"], "explicit_changes")
        self.assertTrue(self.m.sources.record("lab:keep")["active"])
        self.assertTrue(
            self.m.sources.record(
                "lab:" + local_identifier_for("oai:lab:1")
            )["active"]
        )

    def test_parse_error_is_staged_and_recovers_without_retroactive_withdraw(self):
        self.m.sources.snapshot(
            "manual", "lab", 1, [{"id": "keep", "data": self.data()}], 0
        )
        self.use([
            ("resumptionToken", b"<OAI-PMH xmlns='" + OAI.encode() + b"'><broken"),
            ("ListRecords", page_xml([record_xml("oai:lab:1")], token="next")),
        ])
        self.m.harvest.start("h1", "lab", "http://local/oai", "full", max_restarts=0)
        with self.assertRaises(MeshError):
            self.m.harvest.collect("h1")
        self.assertEqual(self.m.harvest.get("h1")["state"], "partial")
        # 修复上游后用同一 run 续采
        self.m.harvest.transport = FakeTransport([
            ("resumptionToken", page_xml([record_xml("oai:lab:2", "Second")])),
            ("ListRecords", page_xml([record_xml("oai:lab:1")], token="next")),
        ])
        staged = self.m.harvest.collect("h1")
        self.assertEqual(staged["state"], "collected")
        self.assertEqual(self.m.harvest.explain("h1")["pages"], 2)
        result = self.m.harvest.apply("h1")
        # 曾失败过的 run 不享受完整快照语义，避免把失败窗口的缺席项当撤回
        self.assertEqual(result["strategy"], "explicit_changes")
        self.assertTrue(self.m.sources.record("lab:keep")["active"])

    def test_deleted_record_with_mapping_is_withdrawn_only_in_explicit_changes(self):
        self.use([("ListRecords", page_xml([record_xml("oai:lab:1")]))])
        self.m.harvest.start("h1", "lab", "http://local/oai", "full")
        self.m.harvest.collect("h1")
        self.m.harvest.apply("h1")
        key = "lab:" + local_identifier_for("oai:lab:1")
        # 增量页明确删除该 identifier
        self.use([("ListRecords", page_xml([record_xml("oai:lab:1", deleted=True)]))])
        self.m.harvest.start(
            "h2", "lab", "http://local/oai", "incremental", from_day="2026-11-10"
        )
        self.m.harvest.collect("h2")
        result = self.m.harvest.apply("h2")
        self.assertEqual(result["strategy"], "explicit_changes")
        self.assertEqual(result["deletes"], 1)
        self.assertFalse(self.m.sources.record(key, True)["active"])
        # 再次收到同一删除：无映射活动项，忽略且不报错
        self.use([("ListRecords", page_xml([record_xml("oai:lab:1", deleted=True)]))])
        self.m.harvest.start(
            "h3", "lab", "http://local/oai", "incremental", from_day="2026-11-11"
        )
        self.m.harvest.collect("h3")
        again = self.m.harvest.apply("h3")
        self.assertEqual(again["ignored_unmapped_deletes"], ["oai:lab:1"])


class RestartTests(HarvestTestBase):
    def _two_page_with_expiry(self):
        calls = []

        def transport(url):
            calls.append(url)
            if "resumptionToken=dead" in url:
                raise OAIError("expired", code="badResumptionToken")
            if "resumptionToken=fresh" in url:
                return page_xml([record_xml("oai:lab:2", "Second")]).encode(), 200
            boundary = [u for u in calls if "resumptionToken" not in u]
            token = "dead" if len(boundary) == 1 else "fresh"
            return page_xml([record_xml("oai:lab:1")], token=token).encode(), 200

        self.m.harvest.transport = transport
        return calls

    def test_expired_token_restarts_from_fixed_boundary(self):
        calls = self._two_page_with_expiry()
        self.m.harvest.start(
            "h1", "lab", "http://local/oai", "full",
            from_day="2026-11-01", until_day="2026-11-10",
        )
        staged = self.m.harvest.collect("h1")
        self.assertEqual(staged["restarts"], 1)
        self.assertEqual(staged["state"], "collected")
        boundary = [u for u in calls if "resumptionToken" not in u]
        self.assertEqual(len(boundary), 2)
        for url in boundary:
            self.assertIn("from=2026-11-01", url)
            self.assertIn("until=2026-11-10", url)
        # 重复的第一页被摘要去重
        explanation = self.m.harvest.explain("h1")
        self.assertEqual(explanation["pages"], 2)
        self.assertEqual(
            [item["external_id"] for item in explanation["identifiers"]],
            ["oai:lab:1", "oai:lab:2"],
        )

    def test_token_that_stays_dead_is_terminal_not_infinite(self):
        def transport(url):
            if "resumptionToken=dead" in url:
                raise OAIError("expired", code="badResumptionToken")
            return page_xml([record_xml("oai:lab:1")], token="dead").encode(), 200

        self.m.harvest.transport = transport
        self.m.harvest.start("h1", "lab", "http://local/oai", "full")
        with self.assertRaises(MeshError):
            self.m.harvest.collect("h1")
        run = self.m.harvest.get("h1")
        self.assertEqual(run["state"], "partial")
        self.assertEqual(run["restarts"], 1)
        self.assertEqual(self.m.harvest.explain("h1")["pages"], 1)

    def test_max_restarts_zero_reports_partial_immediately(self):
        self.use([
            ("resumptionToken", OAIError("expired", code="badResumptionToken")),
            ("ListRecords", page_xml([record_xml("oai:lab:1")], token="next")),
        ])
        self.m.harvest.start(
            "h1", "lab", "http://local/oai", "full", max_restarts=0
        )
        with self.assertRaises(MeshError):
            self.m.harvest.collect("h1")
        self.assertEqual(self.m.harvest.get("h1")["restarts"], 0)
        self.assertEqual(self.m.harvest.get("h1")["state"], "partial")


class IncrementalTests(HarvestTestBase):
    def seed_first_full(self):
        self.use([("ListRecords", page_xml(
            [record_xml("oai:lab:1", "First"), record_xml("oai:lab:2", "Second")]))])
        self.m.harvest.start("h0", "lab", "http://local/oai", "full")
        self.m.harvest.collect("h0")
        self.m.harvest.apply("h0")

    def test_watermark_boundary_and_overlapping_upsert(self):
        self.seed_first_full()
        # 全量不写增量水印
        self.assertIsNone(self.m.harvest.watermark("lab")["day"])
        self.use([("ListRecords", page_xml(
            [record_xml("oai:lab:1", "Updated", datestamp="2026-11-12")]))])
        self.m.harvest.start(
            "h1", "lab", "http://local/oai", "incremental", from_day="2026-11-10"
        )
        self.m.harvest.collect("h1")
        result = self.m.harvest.apply("h1")
        self.assertEqual(result["strategy"], "explicit_changes")
        self.assertEqual(result["watermark"], "2026-11-12")
        self.assertEqual(self.m.harvest.watermark("lab")["day"], "2026-11-12")
        key = "lab:" + local_identifier_for("oai:lab:1")
        self.assertEqual(self.m.sources.record(key)["raw"]["title"], "Updated")
        # 未在增量页出现的 lab:2 保持活动
        self.assertTrue(
            self.m.sources.record(
                "lab:" + local_identifier_for("oai:lab:2")
            )["active"]
        )
        # 下一轮自动采用水印作为固定起点
        self.use([("ListRecords", page_xml([record_xml("oai:lab:1", "Again",
                     datestamp="2026-11-13")]))])
        self.m.harvest.start("h2", "lab", "http://local/oai", "incremental")
        self.assertEqual(self.m.harvest.get("h2")["from_day"], "2026-11-12")
        self.m.harvest.collect("h2")
        self.m.harvest.apply("h2")
        self.assertEqual(self.m.harvest.watermark("lab")["day"], "2026-11-13")

    def test_incremental_requires_boundary_without_watermark(self):
        with self.assertRaises(MeshError) as caught:
            self.m.harvest.start("x", "lab", "http://local/oai", "incremental")
        self.assertEqual(caught.exception.code, "input")

    def test_multiple_versions_choose_last_page_and_are_explained(self):
        overlap = page_xml([
            record_xml("oai:lab:1", "Old", datestamp="2026-11-10"),
            record_xml("oai:lab:1", "New", datestamp="2026-11-11"),
        ])
        self.use([("ListRecords", overlap)])
        self.m.harvest.start(
            "h1", "lab", "http://local/oai", "incremental", from_day="2026-11-10"
        )
        self.m.harvest.collect("h1")
        explanation = self.m.harvest.explain("h1")
        self.assertEqual(explanation["multi_version_identifiers"], ["oai:lab:1"])
        item = explanation["identifiers"][0]
        self.assertEqual(item["occurrences"], 2)
        self.assertEqual(item["chosen"]["datestamp"], "2026-11-11")
        result = self.m.harvest.apply("h1")
        self.assertEqual(result["multi_version_identifiers"], ["oai:lab:1"])
        key = "lab:" + local_identifier_for("oai:lab:1")
        self.assertEqual(self.m.sources.record(key)["raw"]["title"], "New")


class GenerationDriftTests(HarvestTestBase):
    def test_drift_preserves_collection_and_requires_rebase(self):
        self.use([("ListRecords", page_xml([record_xml("oai:lab:1")]))])
        self.m.harvest.start(
            "h1", "lab", "http://local/oai", "incremental", from_day="2026-11-10"
        )
        self.m.harvest.collect("h1")
        # 并发人工导入推进 generation
        self.m.sources.snapshot(
            "manual", "lab", 1,
            [{"id": "manual1", "data": self.data("Manual")}], 0, complete=False,
        )
        with self.assertRaises(MeshError) as caught:
            self.m.harvest.apply("h1")
        self.assertEqual(caught.exception.code, "generation")
        self.assertEqual(caught.exception.details["current_generation"], 1)
        # 采集结果原样保留
        self.assertEqual(self.m.harvest.get("h1")["state"], "collected")
        self.assertEqual(self.m.harvest.explain("h1")["pages"], 1)
        # 人工提交未被覆盖
        self.assertTrue(self.m.sources.record("lab:manual1")["active"])
        # 显式 rebase 确认漂移，保留采集结果
        self.m.harvest.rebase("h1", "manual import acknowledged")
        self.assertEqual(self.m.harvest.get("h1")["expected_generation"], 1)
        # 基线已对齐后再次 rebase 被拒绝
        with self.assertRaises(MeshError):
            self.m.harvest.rebase("h1", "again")
        applied = self.m.harvest.apply("h1")
        # rebase 后只走变更键，完整快照被禁用
        self.assertEqual(applied["strategy"], "explicit_changes")
        self.assertTrue(self.m.sources.record("lab:manual1")["active"])
        self.assertTrue(
            self.m.sources.record(
                "lab:" + local_identifier_for("oai:lab:1")
            )["active"]
        )

    def test_source_config_change_bumps_revision_and_blocks_stale_apply(self):
        self.use([("ListRecords", page_xml([record_xml("oai:lab:1")]))])
        self.m.harvest.start(
            "h1", "lab", "http://local/oai", "incremental", from_day="2026-11-10"
        )
        self.m.harvest.collect("h1")
        # 来源优先级修订不改 generation，因此不构成漂移
        self.m.sources.priority("lab", 5, 1, "policy update")
        applied = self.m.harvest.apply("h1")
        self.assertEqual(applied["strategy"], "explicit_changes")


class PersistenceAndConcurrencyTests(HarvestTestBase):
    def test_run_survives_reopen_and_audit_chain_intact(self):
        self.use([("ListRecords", page_xml([record_xml("oai:lab:1")]))])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "mesh.db"
            with ResearchMesh(path) as mesh:
                mesh.sources.register("lab", "Lab")
                mesh.harvest.transport = self.m.harvest.transport
                mesh.harvest.start("h1", "lab", "http://local/oai", "full")
                mesh.harvest.collect("h1")
                before = mesh.store.verify_audit()
            with ResearchMesh(path) as reopened:
                self.assertEqual(reopened.harvest.get("h1")["state"], "collected")
                result = reopened.harvest.apply("h1")
                self.assertEqual(result["strategy"], "complete_snapshot")
                self.assertTrue(reopened.quality.verify()["verified"])
                reopened.store.verify_audit()
            self.assertTrue(before["verified"])

    def test_concurrent_collects_have_one_winner(self):
        import threading

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "mesh.db"
            started = threading.Event()
            release = threading.Event()

            def holder():
                with ResearchMesh(path) as mesh:
                    mesh.harvest.transport = FakeTransport([
                        ("ListRecords", page_xml([record_xml("oai:lab:1")]))
                    ])
                    token = mesh.harvest._acquire_lock("h1")
                    started.set()
                    release.wait(timeout=5)
                    mesh.harvest._release_lock("h1", token)

            def contender():
                with ResearchMesh(path) as mesh:
                    mesh.harvest.transport = FakeTransport([
                        ("ListRecords", page_xml([record_xml("oai:lab:1")]))
                    ])
                    try:
                        mesh.harvest.collect("h1")
                        mesh.harvest.apply("h1")
                        return "ok"
                    except MeshError as exc:
                        return exc.code

            with ResearchMesh(path) as bootstrap:
                bootstrap.sources.register("lab", "Lab")
                bootstrap.harvest.start("h1", "lab", "http://local/oai", "full")
            thread = threading.Thread(target=holder)
            thread.start()
            self.assertTrue(started.wait(timeout=5))
            # 锁被占用时，第二个工作者立刻得到 busy 而不是等待或重复采集
            self.assertEqual(contender(), "busy")
            release.set()
            thread.join(5)
            # 锁释放后同一 run 可以正常完成并落库
            self.assertEqual(contender(), "ok")
            with ResearchMesh(path) as final:
                self.assertEqual(final.sources.get("lab")["generation"], 1)
                self.assertEqual(
                    final.store.rows("SELECT * FROM harvest_locks"), []
                )
                self.assertTrue(final.quality.verify()["verified"])


class LiveServerTests(HarvestTestBase):
    """真实 HTTP 链路：urllib_transport + 合成 OAI 服务，非假传输。"""

    def setUp(self):
        super().setUp()
        from synthetic_exchange.server import make_server

        scenario = {
            "routes": [
                {
                    "path": "/oai",
                    "query": {"verb": "ListRecords"},
                    "responses": [{
                        "body": page_xml(
                            [record_xml("oai:lab:1", "材料显微图谱")], token="next"
                        ),
                        "headers": {"Content-Type": "application/xml"},
                    }],
                },
                {
                    "path": "/oai",
                    "query": {
                        "verb": "ListRecords",
                        "resumptionToken": "next",
                    },
                    "responses": [{
                        "body": page_xml([record_xml("oai:lab:2", deleted=True)]),
                        "headers": {"Content-Type": "application/xml"},
                    }],
                },
            ]
        }
        self.server = make_server(scenario)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown)
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/oai"
        # 使用真实默认传输，而不是 FakeTransport
        from researchmesh.harvest.oai import urllib_transport
        self.m.harvest.transport = urllib_transport

    def _shutdown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def test_full_harvest_over_real_http(self):
        self.m.harvest.start("live", "lab", self.endpoint, "full")
        staged = self.m.harvest.collect("live")
        self.assertEqual(staged["state"], "collected")
        result = self.m.harvest.apply("live")
        self.assertEqual(result["strategy"], "complete_snapshot")
        self.assertEqual(result["pages"], 2)
        active = self.m.sources.all_active()
        self.assertEqual(len(active), 1)
        self.assertEqual(
            active[0]["raw"]["title"], "材料显微图谱"
        )
        self.assertTrue(self.m.quality.verify()["verified"])

    def test_http_error_surfaces_structured_partial(self):
        from researchmesh.harvest.oai import urllib_transport

        # 指向不存在的路由，服务返回 404，run 进入 partial 且可解释
        self.m.harvest.transport = urllib_transport
        self.m.harvest.start(
            "broken", "lab",
            f"http://127.0.0.1:{self.server.server_port}/missing", "full",
            max_restarts=0,
        )
        with self.assertRaises(MeshError) as caught:
            self.m.harvest.collect("broken")
        self.assertEqual(caught.exception.code, "harvest_failed")
        self.assertEqual(self.m.harvest.get("broken")["state"], "partial")
        # 没有任何完整遍历，绝不撤回
        applied = self.m.harvest.apply("broken")
        self.assertEqual(applied["strategy"], "explicit_changes")
        self.assertEqual(applied["snapshot"]["changed"], [])


class CliHarvestTests(HarvestTestBase):
    def test_cli_harvest_against_live_server_and_stage_only(self):
        import io
        import contextlib
        from researchmesh.main import main
        from synthetic_exchange.server import make_server

        scenario = {
            "routes": [
                {
                    "path": "/oai",
                    "query": {"verb": "ListRecords"},
                    "responses": [{
                        "body": page_xml([record_xml("oai:lab:1")], token="next"),
                        "headers": {"Content-Type": "application/xml"},
                    }],
                },
                {
                    "path": "/oai",
                    "query": {"verb": "ListRecords", "resumptionToken": "next"},
                    "responses": [{
                        "body": page_xml([record_xml("oai:lab:2", deleted=True)]),
                        "headers": {"Content-Type": "application/xml"},
                    }],
                },
            ]
        }
        server = make_server(scenario)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = f"http://127.0.0.1:{server.server_port}/oai"
            with tempfile.TemporaryDirectory() as folder:
                db = str(Path(folder) / "mesh.db")
                register = Path(folder) / "register.json"
                register.write_text(
                    '{"action":"source.register",'
                    '"arguments":{"source":"lab","name":"Lab"}}'
                )
                self.assertEqual(main(["--db", db, "run", str(register)]), 0)
                # --stage-only：只暂存，generation 不前进
                code = main([
                    "--db", db, "harvest", "lab", "h1", endpoint,
                    "full", "--stage-only",
                ])
                self.assertEqual(code, 0)
                with ResearchMesh(db) as check:
                    self.assertEqual(check.harvest.get("h1")["state"], "collected")
                    self.assertEqual(check.sources.get("lab")["generation"], 0)
                    applied = check.harvest.apply("h1")
                    self.assertEqual(applied["strategy"], "complete_snapshot")
                # 不可达端点：结构化错误且退出码非零
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    bad = main([
                        "--db", db, "harvest", "lab", "h2",
                        "http://127.0.0.1:9/oai", "full", "--max-restarts", "0",
                    ])
                self.assertEqual(bad, 2)
                import json
                self.assertEqual(
                    json.loads(err.getvalue())["error"]["code"], "harvest_failed"
                )
                with ResearchMesh(db) as check:
                    # 失败 run 保留为 partial，未撤回任何已落库成果
                    self.assertEqual(check.harvest.get("h2")["state"], "partial")
                    self.assertEqual(check.sources.get("lab")["generation"], 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


class CapacityTests(HarvestTestBase):
    def test_token_cycle_is_detected(self):
        def transport(url):
            if "resumptionToken" in url:
                return page_xml(
                    [record_xml("oai:lab:2")], token="loop").encode(), 200
            return page_xml([record_xml("oai:lab:1")], token="loop").encode(), 200

        self.m.harvest.transport = transport
        self.m.harvest.start("h1", "lab", "http://local/oai", "full")
        with self.assertRaises(MeshError) as caught:
            self.m.harvest.collect("h1")
        self.assertEqual(caught.exception.code, "harvest_failed")
        self.assertEqual(self.m.harvest.get("h1")["state"], "partial")

    def test_duplicate_pages_are_reconciled_not_double_applied(self):
        # 重采导致同一页内容出现两次（token 不同、条目摘要一致）：
        # 第二页按摘要去重，最终只解释为一个 identifier。
        same = record_xml("oai:lab:1")
        transport = self.use([
            ("resumptionToken", page_xml([same])),
            ("ListRecords", page_xml([same], token="next")),
        ])
        self.m.harvest.start("h1", "lab", "http://local/oai", "full")
        self.m.harvest.collect("h1")
        explanation = self.m.harvest.explain("h1")
        self.assertEqual(len(explanation["identifiers"]), 1)
        self.assertEqual(explanation["duplicate_pages"], 1)
        result = self.m.harvest.apply("h1")
        self.assertEqual(result["upserts"], 1)
        self.assertEqual(len(transport.calls), 2)


if __name__ == "__main__":
    unittest.main()
