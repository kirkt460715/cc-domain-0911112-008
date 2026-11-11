import unittest
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from researchmesh.service import ResearchMesh
from researchmesh.contracts import MeshError, decode, record_key
from researchmesh.normalization.text import fold, tokens, normalize_external


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.m = ResearchMesh()
        self.addCleanup(self.m.close)
        self.m.sources.register("lab", "Laboratory", 10)

    def data(self, **changes):
        result = {
            "title": "Precision sensor calibration",
            "institution": "Example Institute",
            "kind": "prototype",
            "year": 2026,
            "people": ["Example Person"],
            "tags": ["Instrument"],
            "identifiers": {"registry": ["local:sensor1"]},
        }
        result.update(changes)
        return result

    def ingest(self, generation=1, rows=None, complete=True):
        if rows is None:
            rows = [{"id": "one", "data": self.data()}]
        return self.m.sources.snapshot(
            "batch" + str(generation), "lab", generation, rows, generation - 1, complete
        )

    def test_unicode_case_punctuation_and_chinese_tokens(self):
        self.assertEqual(fold(" ＡＢＣ—Sensor  "), "abc sensor")
        self.assertEqual(tokens("the precision sensor"), ["precision", "sensor"])
        self.assertEqual(tokens("精密传感器"), ["传感", "密传", "感器", "精密"])
        self.assertEqual(
            normalize_external("doi", "HTTPS://doi.org/10.1234/AbC".lower()),
            "10.1234/abc",
        )
        self.assertEqual(normalize_external("patent", "CN 1234-A"), "CN1234A")
        self.assertEqual(normalize_external("registry", "LOCAL:ABC"), "local:abc")
        with self.assertRaises(MeshError):
            normalize_external("doi", "not-a-doi")
        with self.assertRaises(MeshError):
            decode('{"x":1,"x":2}')

    def test_source_registration_replay_and_priority_revision(self):
        before = self.m.sources.get("lab")
        self.assertEqual(self.m.sources.register("lab", "Laboratory", 10), before)
        with self.assertRaises(MeshError):
            self.m.sources.register("lab", "Other", 10)
        changed = self.m.sources.priority("lab", 20, 1, "trust policy")
        self.assertEqual(changed["priority"], 20)
        self.assertEqual(changed["revision"], 2)
        with self.assertRaises(MeshError):
            self.m.sources.priority("lab", 30, 1, "stale")
        self.assertEqual(self.m.sources.get("lab")["priority"], 20)

    def test_snapshot_replay_does_not_advance_generation(self):
        rows = [{"id": "one", "data": self.data()}]
        result = self.ingest(rows=rows)
        before = self.m.store.verify_audit()
        replay = self.m.sources.snapshot("batch1", "lab", 1, rows, 0)
        self.assertTrue(replay["reused"])
        self.assertEqual(replay["changed"], result["changed"])
        self.assertEqual(before, self.m.store.verify_audit())
        self.assertEqual(self.m.sources.get("lab")["generation"], 1)
        with self.assertRaises(MeshError):
            self.m.sources.snapshot("batch1", "lab", 1, [], 0)
        self.assertEqual(self.m.sources.record("lab:one")["revision"], 1)

    def test_snapshot_requires_next_generation(self):
        with self.assertRaises(MeshError):
            self.m.sources.snapshot("skip", "lab", 2, [], 0)
        self.ingest()
        with self.assertRaises(MeshError):
            self.m.sources.snapshot("stale", "lab", 2, [], 0)
        self.assertEqual(self.m.sources.get("lab")["generation"], 1)
        self.assertEqual(len(self.m.sources.all_active()), 1)
        self.assertTrue(self.m.graph.verify()["verified"])
        self.assertTrue(self.m.quality.verify()["verified"])

    def test_bad_snapshot_is_atomic(self):
        before = self.m.store.verify_audit()
        rows = [
            {"id": "good", "data": self.data()},
            {"id": "bad", "data": self.data(year=True)},
        ]
        with self.assertRaises(MeshError):
            self.ingest(rows=rows)
        self.assertEqual(self.m.sources.get("lab")["generation"], 0)
        self.assertEqual(self.m.sources.all_active(), [])
        self.assertEqual(self.m.store.rows("SELECT * FROM blocks"), [])
        self.assertEqual(self.m.store.verify_audit(), before)
        self.assertEqual(self.m.store.rows("SELECT * FROM batches"), [])

    def test_duplicate_local_identity_is_not_last_write_wins(self):
        rows = [
            {"id": "one", "data": self.data()},
            {"id": "one", "data": self.data(title="Changed")},
        ]
        with self.assertRaises(MeshError):
            self.ingest(rows=rows)
        self.assertEqual(self.m.sources.all_active(), [])
        self.assertEqual(self.m.sources.get("lab")["generation"], 0)
        self.ingest()
        self.assertEqual(
            self.m.sources.record("lab:one")["raw"]["title"],
            "Precision sensor calibration",
        )
        self.assertTrue(self.m.quality.verify()["verified"])

    def test_complete_snapshot_withdraws_missing_records(self):
        self.ingest(
            rows=[
                {"id": "one", "data": self.data()},
                {"id": "two", "data": self.data(title="Second")},
            ]
        )
        result = self.ingest(2)
        self.assertEqual(result["withdrawn"], ["lab:two"])
        with self.assertRaises(MeshError):
            self.m.sources.record("lab:two")
        inactive = self.m.sources.record("lab:two", True)
        self.assertFalse(inactive["active"])
        self.assertEqual(inactive["revision"], 2)
        self.assertEqual(
            self.m.store.rows("SELECT * FROM blocks WHERE record='lab:two'"), []
        )
        self.assertEqual(len(self.m.graph.clusters()), 1)
        self.assertEqual(self.m.sources.history("lab:two")[-1]["active"], False)

    def test_partial_snapshot_preserves_unmentioned_records(self):
        self.ingest(
            rows=[
                {"id": "one", "data": self.data()},
                {"id": "two", "data": self.data(title="Second")},
            ]
        )
        result = self.ingest(
            2, rows=[{"id": "one", "data": self.data(title="Updated")}], complete=False
        )
        self.assertEqual(result["withdrawn"], [])
        self.assertEqual(self.m.sources.record("lab:two")["raw"]["title"], "Second")
        self.assertEqual(self.m.sources.record("lab:one")["revision"], 2)
        self.assertEqual(len(self.m.sources.all_active()), 2)
        self.assertTrue(self.m.quality.verify()["verified"])

    def test_reactivation_has_new_revision_and_history(self):
        self.ingest()
        self.ingest(2, rows=[])
        result = self.ingest(3)
        self.assertEqual(result["reactivated"], ["lab:one"])
        self.assertEqual(self.m.sources.record("lab:one")["revision"], 3)
        history = self.m.sources.history("lab:one")
        self.assertEqual([r["active"] for r in history], [True, False, True])
        self.assertEqual([r["generation"] for r in history], [1, 2, 3])
        self.assertTrue(self.m.candidates.coverage()["ok"])

    def test_unchanged_snapshot_does_not_create_record_revision(self):
        self.ingest()
        first = self.m.sources.record("lab:one")
        second = self.ingest(2)
        self.assertEqual(second["unchanged"], ["lab:one"])
        record = self.m.sources.record("lab:one")
        self.assertEqual(record["revision"], first["revision"])
        self.assertEqual(record["generation"], 2)
        self.assertEqual(len(self.m.sources.history("lab:one")), 1)
        self.assertEqual(record["raw_digest"], first["raw_digest"])

    def test_dictionary_change_requires_explicit_reindex(self):
        self.ingest()
        impact = self.m.dictionary.impact("institution", "Example Institute")
        self.assertEqual(impact["records"], ["lab:one"])
        self.m.dictionary.put(
            "institution", "Example Institute", "Canonical Lab", 0, "same organization"
        )
        with self.assertRaises(MeshError) as caught:
            self.m.releases.preview()
        self.assertEqual(caught.exception.code, "index")
        result = self.m.sources.reindex(1)
        self.assertEqual(result["changed"], ["lab:one"])
        self.assertEqual(
            self.m.sources.record("lab:one")["normalized"]["institution"],
            "canonical lab",
        )
        self.assertTrue(self.m.quality.verify()["verified"])

    def test_dictionary_cycle_rolls_back_revision(self):
        self.m.dictionary.put("institution", "A", "B", 0, "alias")
        with self.assertRaises(MeshError):
            self.m.dictionary.put("institution", "B", "A", 1, "cycle")
        self.assertEqual(self.m.store.revision("dictionary"), 1)
        self.assertEqual(self.m.dictionary.resolve("institution", "a"), "b")
        self.assertEqual(len(self.m.dictionary.list()["rows"]), 1)
        with self.assertRaises(MeshError):
            self.m.dictionary.put("institution", "C", "D", 0, "stale")
        self.assertEqual(
            self.m.dictionary.remove("institution", "A", 1, "remove")["revision"], 2
        )
        self.assertEqual(self.m.dictionary.resolve("institution", "a"), "a")

    def test_delta_preview_applies_upsert_and_withdraw_atomically(self):
        self.ingest(
            rows=[
                {"id": "one", "data": self.data()},
                {"id": "two", "data": self.data(title="Second")},
            ]
        )
        changes = [
            {
                "id": "one",
                "operation": "upsert",
                "expected_revision": 1,
                "data": self.data(title="Updated"),
            },
            {"id": "two", "operation": "withdraw", "expected_revision": 1},
        ]
        preview = self.m.changes.inspect("lab", changes, 1)
        self.assertEqual(self.m.sources.record("lab:one")["revision"], 1)
        result = self.m.changes.apply("delta", "lab", changes, 1, preview["digest"])
        self.assertEqual(result["changed"], ["lab:one"])
        self.assertEqual(result["withdrawn"], ["lab:two"])
        self.assertTrue(
            self.m.changes.apply("delta", "lab", changes, 1, preview["digest"])[
                "reused"
            ]
        )
        self.assertEqual(len(self.m.changes.history("lab")["batches"]), 1)
        comparison = self.m.changes.compare_generations("lab", 1, 2)
        self.assertEqual(
            [r["kind"] for r in comparison["changes"]], ["changed", "withdrawn"]
        )

    def test_delta_stale_record_or_preview_never_writes(self):
        self.ingest()
        changes = [{"id": "one", "operation": "withdraw", "expected_revision": 2}]
        with self.assertRaises(MeshError):
            self.m.changes.inspect("lab", changes, 1)
        changes[0]["expected_revision"] = 1
        preview = self.m.changes.inspect("lab", changes, 1)
        with self.assertRaises(MeshError):
            self.m.changes.apply("bad", "lab", changes, 1, "wrong")
        self.assertEqual(self.m.sources.get("lab")["generation"], 1)
        self.assertTrue(self.m.sources.record("lab:one")["active"])
        self.assertEqual(self.m.changes.history("lab")["batches"], [])
        self.assertTrue(preview["payload"]["operations"])

    def test_jsonl_protocol_and_record_pagination(self):
        import json

        content = "\n".join(
            json.dumps(
                {"id": str(index), "data": self.data(title="Title " + str(index))}
            )
            for index in range(3)
        )
        rows = self.m.sources.parse(content)
        self.ingest(rows=rows)
        first = self.m.sources.list_records(limit=2)
        self.assertEqual([r["key"] for r in first["records"]], ["lab:0", "lab:1"])
        self.assertEqual(first["next"], "lab:1")
        second = self.m.sources.list_records(after=first["next"], limit=2)
        self.assertEqual([r["key"] for r in second["records"]], ["lab:2"])
        self.assertIsNone(second["next"])
        with self.assertRaises(MeshError) as caught:
            self.m.sources.parse(content + "\n{bad}")
        self.assertEqual(caught.exception.details["line"], 4)

    def test_database_backup_reopens_verified_state(self):
        self.ingest()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "copy.db"
            self.m.store.backup(path)
            with ResearchMesh(path) as reopened:
                self.assertEqual(reopened.sources.record("lab:one")["revision"], 1)
                self.assertTrue(reopened.quality.verify()["verified"])
                self.assertEqual(
                    reopened.store.verify_audit(), self.m.store.verify_audit()
                )
            with self.assertRaises(MeshError):
                self.m.store.backup(path)
        self.assertEqual(self.m.store.depth, 0)

    def test_source_generation_concurrency_has_one_winner(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "concurrent.db"
            with ResearchMesh(path) as initial:
                initial.sources.register("lab", "Lab")
            data = self.data()

            def ingest(batch):
                with ResearchMesh(path) as worker:
                    try:
                        worker.sources.snapshot(
                            batch, "lab", 1, [{"id": "one", "data": data}], 0
                        )
                        return "ok"
                    except MeshError as exc:
                        return exc.code

            with ThreadPoolExecutor(max_workers=2) as pool:
                result = list(pool.map(ingest, ["a", "b"]))
            self.assertEqual(sorted(result), ["ok", "sequence"])
            with ResearchMesh(path) as final:
                self.assertEqual(final.sources.get("lab")["generation"], 1)
                self.assertEqual(len(final.sources.all_active()), 1)
                self.assertTrue(final.quality.verify()["verified"])
