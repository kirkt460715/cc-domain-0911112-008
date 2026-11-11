import unittest
import tempfile
import json
import io
import contextlib
from pathlib import Path
from researchmesh.service import ResearchMesh
from researchmesh.contracts import MeshError, canonical
from researchmesh.demo import run
from researchmesh.main import main


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.m = ResearchMesh()
        self.addCleanup(self.m.close)
        self.m.sources.register("a", "Trusted source", 10)
        self.m.sources.register("b", "Rich source", 20)
        self.m.sources.snapshot("a1", "a", 1, [{"id": "one", "data": self.data()}], 0)
        self.m.sources.snapshot(
            "b1",
            "b",
            1,
            [
                {
                    "id": "one",
                    "data": self.data(
                        title="Precision Sensor", abstract="Rich source abstract"
                    ),
                }
            ],
            0,
        )

    def data(self, **extra):
        row = {
            "title": "precision sensor",
            "institution": "Institute",
            "kind": "prototype",
            "year": 2026,
            "country": "CN",
            "tags": ["sensor"],
            "identifiers": {"registry": ["local:sensor"]},
        }
        row.update(extra)
        return row

    def merge(self):
        preview = self.m.graph.preview("a:one", "b:one")
        return self.m.graph.decide(
            "merge",
            "a:one",
            "b:one",
            "merge",
            preview["digest"],
            "reviewer",
            "same registration",
        )

    def freeze(self, name):
        preview = self.m.releases.preview()
        return self.m.releases.freeze(name, preview["digest"], "publisher")

    def test_review_lease_excludes_other_workers(self):
        self.m.review.enqueue("task", "a:one", "b:one")
        claimed = self.m.review.claim("task", "first", 10, seconds=10)
        self.assertEqual(claimed["revision"], 2)
        self.assertEqual(claimed["lease_until"], 20)
        with self.assertRaises(MeshError):
            self.m.review.claim("task", "second", 11, expected_revision=2)
        self.assertEqual(self.m.review.pending(11, "second")["tasks"], [])
        self.assertEqual(len(self.m.review.pending(11, "first")["tasks"]), 1)
        next_claim = self.m.review.claim("task", "second", 20, expected_revision=2)
        self.assertEqual(next_claim["owner"], "second")
        self.assertEqual(next_claim["revision"], 3)
        with self.assertRaises(MeshError):
            self.m.review.dismiss("task", "first", 21, 2, "expired")

    def test_review_resolves_with_current_graph_preview(self):
        self.m.review.enqueue("task", "a:one", "b:one")
        self.m.review.claim("task", "reviewer", 10)
        preview = self.m.graph.preview("a:one", "b:one")
        result = self.m.review.resolve(
            "task",
            "reviewer",
            11,
            2,
            "decision",
            "merge",
            preview["digest"],
            "same entity",
        )
        self.assertEqual(result["state"], "resolved")
        self.assertEqual(result["result"]["decision"], "decision")
        self.assertEqual(len(self.m.graph.clusters()), 1)
        self.assertEqual(self.m.review.stats(12)["resolved"], 1)
        with self.assertRaises(MeshError):
            self.m.review.dismiss("task", "reviewer", 12, 3, "too late")
        self.assertTrue(self.m.quality.verify()["verified"])

    def test_stale_review_requires_refresh_before_resolution(self):
        self.m.review.enqueue("task", "a:one", "b:one")
        self.m.review.claim("task", "reviewer", 10)
        self.m.sources.snapshot(
            "b2", "b", 2, [{"id": "one", "data": self.data(title="Revised sensor")}], 1
        )
        self.assertFalse(self.m.review.get("task")["fresh"])
        preview = self.m.graph.preview("a:one", "b:one")
        with self.assertRaises(MeshError):
            self.m.review.resolve(
                "task", "reviewer", 11, 2, "d", "merge", preview["digest"], "same"
            )
        self.assertEqual(self.m.review.stats(12)["stale"], 1)
        refreshed = self.m.review.refresh("task", "reviewer", 12, 2)
        self.assertTrue(refreshed["fresh"])
        self.assertEqual(refreshed["revision"], 3)
        result = self.m.review.resolve(
            "task", "reviewer", 13, 3, "d", "merge", preview["digest"], "same"
        )
        self.assertEqual(result["state"], "resolved")

    def test_failed_review_decision_does_not_resolve_task(self):
        self.m.review.enqueue("task", "a:one", "b:one")
        self.m.review.claim("task", "reviewer", 10)
        with self.assertRaises(MeshError):
            self.m.review.resolve(
                "task", "reviewer", 11, 2, "d", "merge", "wrong", "same"
            )
        self.assertEqual(self.m.review.get("task")["state"], "pending")
        self.assertEqual(self.m.review.get("task")["revision"], 2)
        self.assertEqual(self.m.store.rows("SELECT * FROM decisions"), [])
        self.m.review.release("task", "reviewer", 12, 2)
        self.assertIsNone(self.m.review.get("task")["owner"])
        self.assertEqual(self.m.review.get("task")["revision"], 3)

    def test_review_dismissal_records_reason_and_preserves_separation(self):
        self.m.review.enqueue("task", "a:one", "b:one")
        self.m.review.claim("task", "reviewer", 10)
        result = self.m.review.dismiss(
            "task", "reviewer", 11, 2, "insufficient supporting evidence"
        )
        self.assertEqual(result["state"], "dismissed")
        self.assertEqual(result["result"]["reason"], "insufficient supporting evidence")
        self.assertEqual(len(self.m.graph.clusters()), 2)
        self.assertEqual(self.m.review.pending(12)["tasks"], [])
        self.assertEqual(self.m.review.stats(12)["dismissed"], 1)
        self.assertEqual(self.m.store.rows("SELECT * FROM decisions"), [])

    def test_master_uses_trust_then_completeness_with_provenance(self):
        self.merge()
        master = self.m.masters.build("b:one")
        self.assertEqual(master["id"], "a:one")
        self.assertEqual(master["data"]["title"], "precision sensor")
        self.assertEqual(master["data"]["abstract"], "Rich source abstract")
        self.assertEqual(master["provenance"]["title"]["record"], "a:one")
        self.assertEqual(master["provenance"]["abstract"]["record"], "b:one")
        self.assertEqual(master["data"]["identifiers"]["registry"], ["local:sensor"])
        self.assertEqual(master["members"], ["a:one", "b:one"])
        conflicts = self.m.masters.conflicts("a:one")
        self.assertTrue(any(row["field"] == "title" for row in conflicts["conflicts"]))

    def test_priority_change_updates_master_but_not_record_revision(self):
        self.merge()
        before = self.m.masters.build("a:one")
        revision = self.m.sources.record("b:one")["revision"]
        self.m.sources.priority("b", 1, 1, "verified source is authoritative")
        after = self.m.masters.build("a:one")
        self.assertEqual(after["data"]["title"], "Precision Sensor")
        self.assertEqual(after["provenance"]["title"]["record"], "b:one")
        self.assertNotEqual(before["digest"], after["digest"])
        self.assertEqual(self.m.sources.record("b:one")["revision"], revision)
        self.assertEqual(self.m.graph.decision("merge")["state"], "active")

    def test_pin_selected_field_and_unpin(self):
        self.merge()
        before = self.m.masters.build("a:one")
        pinned = self.m.masters.pin(
            "a:one", "title", "b:one", before["digest"], 0, "reviewed spelling"
        )
        self.assertEqual(pinned["data"]["title"], "Precision Sensor")
        self.assertEqual(pinned["provenance"]["title"]["rule"], "manual_pin")
        with self.assertRaises(MeshError):
            self.m.masters.pin(
                "a:one", "title", "a:one", before["digest"], 1, "stale review"
            )
        restored = self.m.masters.unpin("a:one", "title", 1, "remove override")
        self.assertEqual(restored["data"]["title"], "precision sensor")
        self.assertEqual(
            restored["provenance"]["title"]["rule"], "priority_completeness_key"
        )
        self.assertEqual(self.m.masters.orphan_pins(), [])

    def test_orphan_pin_blocks_release_until_reviewed_removal(self):
        self.merge()
        master = self.m.masters.build("a:one")
        self.m.masters.pin("a:one", "title", "b:one", master["digest"], 0, "spelling")
        self.m.graph.revoke(
            "merge", self.m.store.revision("graph"), "reviewer", "split entity"
        )
        self.assertEqual(len(self.m.masters.orphan_pins()), 1)
        preview = self.m.releases.preview()
        with self.assertRaises(MeshError):
            self.m.releases.freeze("blocked", preview["digest"], "publisher")
        removed = self.m.masters.discard_orphan_pin(
            "a:one", "title", 1, "split invalidated pin"
        )
        self.assertTrue(removed["discarded"])
        self.assertEqual(self.m.masters.orphan_pins(), [])
        self.assertEqual(len(self.freeze("recovered")["payload"]["masters"]), 2)

    def test_release_merge_split_diff_tracks_record_membership(self):
        self.freeze("before")
        self.merge()
        merged = self.freeze("merged")
        difference = self.m.releases.diff("before", "merged")
        self.assertEqual(len(difference["merges"]), 1)
        self.assertEqual(difference["merges"][0]["before"], ["a:one", "b:one"])
        self.assertEqual(difference["merges"][0]["after"], "a:one")
        self.m.graph.revoke(
            "merge", self.m.store.revision("graph"), "reviewer", "split"
        )
        self.freeze("after")
        split = self.m.releases.diff("merged", "after")
        self.assertEqual(split["splits"][0]["after"], ["a:one", "b:one"])
        self.assertEqual(self.m.releases.get("merged"), merged)
        lineage = self.m.releases.lineage("b:one")
        self.assertEqual(
            [row["cluster"] for row in lineage["releases"]], ["b:one", "a:one", "b:one"]
        )

    def test_release_stale_preview_and_replay(self):
        preview = self.m.releases.preview()
        self.merge()
        with self.assertRaises(MeshError):
            self.m.releases.freeze("stale", preview["digest"], "publisher")
        self.assertEqual(self.m.releases.list(), [])
        frozen = self.freeze("one")
        self.assertEqual(
            self.m.releases.freeze("one", frozen["digest"], "publisher"), frozen
        )
        with self.assertRaises(MeshError):
            self.m.releases.freeze("one", "wrong", "publisher")
        self.assertEqual(len(self.m.releases.list()), 1)
        self.assertEqual(self.m.releases.get("one")["digest"], frozen["digest"])

    def test_release_export_verification_and_no_overwrite(self):
        self.merge()
        self.freeze("one")
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "release.json"
            exported = self.m.releases.export("one", target)
            self.assertGreater(exported["bytes"], 0)
            self.assertEqual(self.m.releases.verify(target)["masters"], 1)
            self.assertEqual(self.m.releases.verify(target)["records"], 2)
            with self.assertRaises(MeshError):
                self.m.releases.export("one", target)
            envelope = json.loads(target.read_text())
            envelope["payload"]["masters"][0]["data"]["title"] = "tampered"
            target.write_text(json.dumps(envelope))
            with self.assertRaises(MeshError):
                self.m.releases.verify(target)
            self.assertEqual(list(Path(folder).glob(".mesh-*")), [])

    def test_catalogue_query_facets_and_release_bound_cursor(self):
        self.freeze("first")
        result = self.m.catalogue.search("first", query="sensor", limit=1)
        self.assertEqual(result["total"], 2)
        self.assertIsNotNone(result["next"])
        self.assertEqual(result["facets"]["countries"], {"CN": 2})
        second = self.m.catalogue.search(
            "first", query="sensor", limit=1, cursor=result["next"]
        )
        self.assertEqual(second["rows"][0]["id"], "b:one")
        self.assertIsNone(second["next"])
        with self.assertRaises(MeshError):
            self.m.catalogue.search("first", query="precision", cursor=result["next"])
        self.merge()
        self.freeze("second")
        with self.assertRaises(MeshError):
            self.m.catalogue.search("second", query="sensor", cursor=result["next"])
        self.assertEqual(self.m.catalogue.search("first")["total"], 2)
        self.assertEqual(self.m.catalogue.search("second")["total"], 1)

    def test_catalogue_external_lookup_preserves_ambiguity(self):
        self.freeze("separate")
        result = self.m.catalogue.external("separate", "registry", "LOCAL:SENSOR")
        self.assertTrue(result["ambiguous"])
        self.assertEqual(len(result["matches"]), 2)
        self.merge()
        self.freeze("merged")
        result = self.m.catalogue.external("merged", "registry", "local:sensor")
        self.assertFalse(result["ambiguous"])
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(
            self.m.catalogue.source_record("merged", "b:one")["master"]["id"], "a:one"
        )
        self.assertIn(
            "cluster,title,institution,kind,year,country,members",
            self.m.catalogue.csv("merged")["content"],
        )

    def test_catalogue_filters_are_applied_before_pagination(self):
        self.freeze("one")
        self.assertEqual(self.m.catalogue.search("one", kind="dataset")["total"], 0)
        self.assertEqual(self.m.catalogue.search("one", country="US")["total"], 0)
        self.assertEqual(self.m.catalogue.search("one", year_from=2027)["total"], 0)
        self.assertEqual(self.m.catalogue.search("one", tag="SENSOR")["total"], 2)
        self.assertEqual(
            self.m.catalogue.search("one", institution="INSTITUTE")["total"], 2
        )
        self.assertEqual(self.m.catalogue.search("one", query="absent")["rows"], [])
        with self.assertRaises(MeshError):
            self.m.catalogue.search("one", year_from=2027, year_to=2026)
        with self.assertRaises(MeshError):
            self.m.catalogue.search("one", cursor="invalid")

    def test_command_dispatch_and_full_demo(self):
        result = self.m.dispatch({"action": "source.get", "arguments": {"source": "a"}})
        self.assertEqual(result["name"], "Trusted source")
        self.assertGreater(len(self.m.describe()), 60)
        with self.assertRaises(MeshError):
            self.m.dispatch({"action": "missing"})
        with self.assertRaises(MeshError):
            self.m.dispatch({"action": "source.get", "arguments": {"wrong": "a"}})
        result = run()
        self.assertEqual(result["merged_masters"], 1)
        self.assertEqual(result["revoked_masters"], 2)
        self.assertTrue(result["immutable_release_verified"])
        self.assertTrue(result["offline"])

    def test_cli_error_is_structured_and_nonzero(self):
        with tempfile.TemporaryDirectory() as folder:
            request = Path(folder) / "request.json"
            db = Path(folder) / "mesh.db"
            request.write_text('{"action":"unknown"}')
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(["--db", str(db), "run", str(request)])
            self.assertEqual(code, 2)
            self.assertEqual(out.getvalue(), "")
            self.assertEqual(json.loads(err.getvalue())["error"]["code"], "action")
            self.assertFalse(json.loads(err.getvalue())["ok"])
            self.assertTrue(db.is_file())
