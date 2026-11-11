import unittest
import tempfile
from pathlib import Path
from researchmesh.service import ResearchMesh
from researchmesh.contracts import MeshError, canonical, digest


class RecoveryQualityTests(unittest.TestCase):
    def setUp(self):
        self.m = ResearchMesh()
        self.addCleanup(self.m.close)
        for name in ["a", "b"]:
            self.m.sources.register(name, name)
            self.m.sources.snapshot(
                name + "1", name, 1, [{"id": "x", "data": self.data()}], 0
            )

    def data(self, **extra):
        row = {
            "title": "sensor dataset",
            "institution": "Lab",
            "kind": "dataset",
            "year": 2026,
            "identifiers": {"doi": ["10.1234/sensor"]},
            "tags": ["measurement"],
        }
        row.update(extra)
        return row

    def merge(self):
        preview = self.m.graph.preview("a:x", "b:x")
        return self.m.graph.decide(
            "merge", "a:x", "b:x", "merge", preview["digest"], "reviewer", "same DOI"
        )

    def test_restart_preserves_pending_review_and_release(self):
        self.m.review.enqueue("review", "a:x", "b:x")
        self.m.review.claim("review", "old", 0, 1)
        preview = self.m.releases.preview()
        self.m.releases.freeze("before", preview["digest"], "publisher")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "restart.db"
            self.m.store.backup(path)
            with ResearchMesh(path) as restarted:
                self.assertEqual(restarted.review.get("review")["owner"], "old")
                claimed = restarted.review.claim(
                    "review", "new", 2, expected_revision=2
                )
                proposal = restarted.graph.preview("a:x", "b:x")
                result = restarted.review.resolve(
                    "review",
                    "new",
                    3,
                    claimed["revision"],
                    "merge",
                    "merge",
                    proposal["digest"],
                    "reviewed after restart",
                )
                self.assertEqual(result["state"], "resolved")
                self.assertEqual(len(restarted.graph.clusters()), 1)
                self.assertEqual(
                    len(restarted.releases.get("before")["payload"]["masters"]), 2
                )
                self.assertTrue(restarted.quality.verify()["verified"])

    def test_unchanged_source_snapshot_keeps_valid_decision(self):
        self.merge()
        original = self.m.graph.decision("merge")["evidence"]
        result = self.m.sources.snapshot(
            "a2", "a", 2, [{"id": "x", "data": self.data()}], 1
        )
        self.assertEqual(result["unchanged"], ["a:x"])
        self.assertEqual(self.m.graph.decision("merge")["state"], "active")
        self.assertTrue(self.m.evidence.fresh(original))
        self.assertEqual(len(self.m.graph.clusters()), 1)
        self.assertEqual(self.m.sources.record("a:x")["revision"], 1)
        self.assertTrue(self.m.quality.verify()["verified"])

    def test_dictionary_reindex_stales_only_changed_endpoint_evidence(self):
        self.merge()
        self.m.dictionary.put("institution", "Lab", "Canonical Lab", 0, "alias policy")
        self.assertEqual(self.m.graph.decision("merge")["state"], "active")
        with self.assertRaises(MeshError):
            self.m.evidence.compare("a:x", "b:x")
        result = self.m.sources.reindex(1)
        self.assertEqual(result["changed"], ["a:x", "b:x"])
        self.assertEqual(self.m.graph.decision("merge")["state"], "stale")
        self.assertEqual(len(self.m.graph.clusters()), 2)
        self.assertEqual(self.m.sources.record("a:x")["revision"], 1)
        self.assertTrue(self.m.quality.verify()["verified"])

    def test_snapshot_failure_restores_decisions_and_graph(self):
        self.merge()
        revision = self.m.store.revision("graph")
        audit = self.m.store.verify_audit()
        rows = [
            {"id": "x", "data": self.data(title="Changed")},
            {"id": "bad", "data": self.data(identifiers={"doi": ["invalid"]})},
        ]
        with self.assertRaises(MeshError):
            self.m.sources.snapshot("a2", "a", 2, rows, 1)
        self.assertEqual(self.m.graph.decision("merge")["state"], "active")
        self.assertEqual(self.m.store.revision("graph"), revision)
        self.assertEqual(self.m.store.verify_audit(), audit)
        self.assertEqual(self.m.sources.get("a")["generation"], 1)
        self.assertEqual(len(self.m.graph.clusters()), 1)

    def test_nested_transaction_rollback_restores_all_materializations(self):
        audit = self.m.store.verify_audit()
        with self.assertRaises(RuntimeError):
            with self.m.store.atomic():
                self.merge()
                self.assertEqual(len(self.m.graph.clusters()), 1)
                raise RuntimeError("simulated transaction failure")
        self.assertEqual(self.m.store.rows("SELECT * FROM decisions"), [])
        self.assertEqual(len(self.m.graph.clusters()), 2)
        self.assertEqual(self.m.store.verify_audit(), audit)
        self.assertEqual(self.m.store.depth, 0)
        self.assertTrue(self.m.quality.verify()["verified"])

    def test_source_profiles_and_identity_collisions(self):
        profile = self.m.quality.source_profile("a")
        self.assertEqual(profile["active_records"], 1)
        self.assertEqual(profile["missing_fields"]["abstract"], 1)
        self.assertEqual(profile["kinds"], {"dataset": 1})
        collisions = self.m.quality.identifier_collisions()
        self.assertEqual(len(collisions), 1)
        self.assertEqual(collisions[0]["clusters"], ["a:x", "b:x"])
        self.merge()
        self.assertEqual(self.m.quality.identifier_collisions(), [])
        clusters = self.m.quality.cluster_profile()
        self.assertEqual(clusters["size_histogram"], {"2": 1})
        self.assertEqual(clusters["cross_source_clusters"][0]["sources"], ["a", "b"])

    def test_quality_detects_missing_block_and_payload_tampering(self):
        self.m.store.connection.execute("DELETE FROM blocks WHERE record='a:x'")
        self.assertFalse(self.m.candidates.coverage()["ok"])
        with self.assertRaises(MeshError):
            self.m.quality.verify()
        self.m.sources.reindex(0)
        self.assertTrue(self.m.quality.verify()["verified"])
        self.m.store.connection.execute(
            "UPDATE records SET normalized='{}' WHERE key='a:x'"
        )
        with self.assertRaises(MeshError) as caught:
            self.m.sources.record("a:x")
        self.assertEqual(caught.exception.code, "integrity")
        with self.assertRaises(MeshError):
            self.m.releases.preview()

    def test_orphan_pin_cleanup_is_exposed_by_dispatch(self):
        self.merge()
        master = self.m.masters.build("a:x")
        self.m.masters.pin(
            "a:x", "title", "b:x", master["digest"], 0, "manual preference"
        )
        self.m.graph.revoke(
            "merge", self.m.store.revision("graph"), "reviewer", "split"
        )
        result = self.m.dispatch(
            {
                "action": "master.discard_orphan_pin",
                "arguments": {
                    "cluster": "a:x",
                    "field": "title",
                    "expected_revision": 1,
                    "reason": "split cluster",
                },
            }
        )
        self.assertTrue(result["discarded"])
        self.assertEqual(self.m.masters.orphan_pins(), [])
        preview = self.m.releases.preview()
        frozen = self.m.releases.freeze("clean", preview["digest"], "publisher")
        self.assertEqual(len(frozen["payload"]["masters"]), 2)
        self.assertTrue(self.m.quality.verify()["verified"])

    def test_delta_add_withdraw_reactivate_tracks_generations(self):
        changes = [
            {"id": "x", "operation": "withdraw", "expected_revision": 1},
            {
                "id": "y",
                "operation": "upsert",
                "expected_revision": 0,
                "data": self.data(title="Other dataset"),
            },
        ]
        preview = self.m.changes.inspect("a", changes, 1)
        self.m.changes.apply("delta", "a", changes, 1, preview["digest"])
        self.assertFalse(self.m.sources.record("a:x", True)["active"])
        self.assertEqual(self.m.sources.record("a:y")["revision"], 1)
        second = [
            {
                "id": "x",
                "operation": "upsert",
                "expected_revision": 2,
                "data": self.data(),
            }
        ]
        preview = self.m.changes.inspect("a", second, 2)
        self.m.changes.apply("return", "a", second, 2, preview["digest"])
        self.assertEqual(self.m.sources.record("a:x")["revision"], 3)
        self.assertTrue(self.m.sources.record("a:y")["active"])
        self.assertEqual(len(self.m.changes.history("a")["batches"]), 2)
        diff = self.m.changes.compare_generations("a", 0, 3)
        self.assertEqual([row["kind"] for row in diff["changes"]], ["added", "added"])

    def test_rehashing_release_cannot_hide_member_duplication(self):
        self.merge()
        preview = self.m.releases.preview()
        payload = preview["payload"]
        payload["masters"].append(dict(payload["masters"][0]))
        with self.assertRaises(MeshError) as caught:
            self.m.releases.verify_payload(payload)
        self.assertEqual(caught.exception.code, "integrity")
        fresh = self.m.releases.preview()
        self.assertEqual(len(fresh["payload"]["masters"]), 1)
        self.assertEqual(fresh["digest"], preview["digest"])
        self.assertTrue(self.m.graph.verify()["verified"])
