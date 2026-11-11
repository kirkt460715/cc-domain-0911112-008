import unittest
from researchmesh.service import ResearchMesh
from researchmesh.contracts import MeshError


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.m = ResearchMesh()
        self.addCleanup(self.m.close)
        for source in ["a", "b", "c"]:
            self.m.sources.register(source, source)
            self.m.sources.snapshot(
                source + "1", source, 1, [{"id": "one", "data": self.data()}], 0
            )

    def data(self, **extra):
        result = {
            "title": "Precision sensor calibration",
            "institution": "Institute",
            "kind": "prototype",
            "year": 2026,
            "people": ["Person"],
            "tags": ["sensor"],
        }
        result.update(extra)
        return result

    def decide(self, ident, left, right, kind="merge"):
        preview = self.m.graph.preview(left, right, kind)
        return self.m.graph.decide(
            ident, left, right, kind, preview["digest"], "operator", "synthetic review"
        )

    def test_pair_evidence_is_symmetric_and_explainable(self):
        left = self.m.evidence.compare("a:one", "b:one")
        right = self.m.evidence.compare("b:one", "a:one")
        self.assertEqual(left, right)
        self.assertEqual(left["score"], 75)
        self.assertEqual(left["recommendation"], "review")
        self.assertEqual(left["conflicts"], [])
        self.assertIn("title_similarity", [row["feature"] for row in left["features"]])
        self.assertTrue(
            self.m.evidence.explain("a:one", "b:one")["summary"][
                "requires_human_decision"
            ]
        )
        self.assertEqual(len(self.m.graph.clusters()), 3)

    def test_transitive_merge_path_and_canonical_cluster(self):
        self.decide("ab", "b:one", "a:one")
        self.decide("bc", "b:one", "c:one")
        self.assertEqual(self.m.graph.cluster("c:one"), "a:one")
        self.assertEqual(self.m.graph.members("c:one"), ["a:one", "b:one", "c:one"])
        path = self.m.graph.path("a:one", "c:one")
        self.assertTrue(path["connected"])
        self.assertEqual(path["decisions"], ["ab", "bc"])
        self.assertEqual(path["records"], ["a:one", "b:one", "c:one"])
        self.assertTrue(self.m.graph.verify()["verified"])

    def test_cannot_link_applies_across_transitive_components(self):
        self.decide("ac-no", "a:one", "c:one", "cannot")
        self.decide("ab", "a:one", "b:one")
        preview = self.m.graph.preview("b:one", "c:one")
        self.assertFalse(preview["allowed"])
        self.assertEqual(preview["blockers"][0]["kind"], "cannot_link")
        with self.assertRaises(MeshError):
            self.m.graph.decide(
                "bc",
                "b:one",
                "c:one",
                "merge",
                preview["digest"],
                "operator",
                "disallowed",
            )
        self.assertEqual(len(self.m.graph.clusters()), 2)
        self.assertFalse(self.m.graph.path("a:one", "c:one")["connected"])
        self.assertTrue(self.m.graph.verify()["verified"])

    def test_cannot_link_inside_component_is_rejected(self):
        self.decide("ab", "a:one", "b:one")
        preview = self.m.graph.preview("a:one", "b:one", "cannot")
        self.assertFalse(preview["allowed"])
        self.assertEqual(preview["blockers"][0]["kind"], "already_connected")
        with self.assertRaises(MeshError):
            self.m.graph.decide(
                "no",
                "a:one",
                "b:one",
                "cannot",
                preview["digest"],
                "operator",
                "invalid",
            )
        self.assertEqual(self.m.graph.restrictions(), [])
        self.assertTrue(self.m.graph.verify()["verified"])

    def test_identifier_conflict_is_checked_across_component(self):
        self.m.sources.snapshot(
            "a2",
            "a",
            2,
            [
                {
                    "id": "one",
                    "data": self.data(identifiers={"registry": ["local:first"]}),
                }
            ],
            1,
        )
        self.m.sources.snapshot(
            "c2",
            "c",
            2,
            [
                {
                    "id": "one",
                    "data": self.data(identifiers={"registry": ["local:second"]}),
                }
            ],
            1,
        )
        self.decide("ab", "a:one", "b:one")
        direct = self.m.evidence.compare("b:one", "c:one")
        self.assertEqual(direct["conflicts"], [])
        preview = self.m.graph.preview("b:one", "c:one")
        self.assertFalse(preview["allowed"])
        self.assertTrue(
            any(
                row["kind"] == "identifier_conflict:registry"
                for row in preview["blockers"]
            )
        )
        self.assertEqual(len(self.m.graph.clusters()), 2)
        self.assertTrue(self.m.quality.verify()["verified"])

    def test_kind_conflict_cannot_be_overridden_by_score(self):
        self.m.sources.snapshot(
            "c2", "c", 2, [{"id": "one", "data": self.data(kind="dataset")}], 1
        )
        evidence = self.m.evidence.compare("a:one", "c:one")
        self.assertIn("kind_conflict", evidence["conflicts"])
        self.assertEqual(evidence["recommendation"], "reject")
        preview = self.m.graph.preview("a:one", "c:one")
        self.assertFalse(preview["allowed"])
        with self.assertRaises(MeshError):
            self.decide("ac", "a:one", "c:one")
        self.assertEqual(len(self.m.graph.clusters()), 3)

    def test_revoke_bridge_splits_component_and_preserves_history(self):
        self.decide("ab", "a:one", "b:one")
        self.decide("bc", "b:one", "c:one")
        impact = self.m.graph.impact("bc")
        self.assertTrue(impact["splits_component"])
        self.assertEqual(impact["components"], [["a:one", "b:one"], ["c:one"]])
        self.m.graph.revoke(
            "bc", self.m.store.revision("graph"), "operator", "correction"
        )
        self.assertEqual(self.m.graph.decision("bc")["state"], "revoked")
        self.assertEqual(self.m.graph.members("a:one"), ["a:one", "b:one"])
        self.assertEqual(self.m.graph.members("c:one"), ["c:one"])
        self.assertTrue(self.m.graph.verify()["verified"])

    def test_redundant_path_prevents_split_until_all_paths_removed(self):
        self.decide("ab", "a:one", "b:one")
        self.decide("bc", "b:one", "c:one")
        self.decide("ac", "a:one", "c:one")
        self.assertFalse(self.m.graph.impact("bc")["splits_component"])
        self.m.graph.revoke(
            "bc", self.m.store.revision("graph"), "operator", "remove redundant edge"
        )
        self.assertEqual(len(self.m.graph.clusters()), 1)
        self.assertEqual(self.m.graph.path("b:one", "c:one")["decisions"], ["ab", "ac"])
        self.m.graph.revoke(
            "ac", self.m.store.revision("graph"), "operator", "remove bridge"
        )
        self.assertEqual(len(self.m.graph.clusters()), 2)
        self.assertTrue(self.m.graph.verify()["verified"])

    def test_changed_record_marks_evidence_stale_and_splits(self):
        self.decide("ab", "a:one", "b:one")
        self.m.sources.snapshot(
            "b2",
            "b",
            2,
            [{"id": "one", "data": self.data(title="Changed sensor title")}],
            1,
        )
        self.assertEqual(self.m.graph.decision("ab")["state"], "stale")
        self.assertEqual(len(self.m.graph.clusters()), 3)
        self.assertFalse(self.m.graph.path("a:one", "b:one")["connected"])
        self.assertTrue(self.m.graph.verify()["verified"])
        self.decide("ab2", "a:one", "b:one")
        self.assertEqual(len(self.m.graph.clusters()), 2)
        self.assertEqual(self.m.graph.decision("ab")["state"], "stale")

    def test_withdraw_and_reactivate_do_not_resurrect_old_merge(self):
        self.decide("ab", "a:one", "b:one")
        self.m.sources.snapshot("b2", "b", 2, [], 1)
        self.assertEqual(self.m.graph.decision("ab")["state"], "stale")
        self.assertEqual(len(self.m.graph.clusters()), 2)
        self.m.sources.snapshot("b3", "b", 3, [{"id": "one", "data": self.data()}], 2)
        self.assertEqual(len(self.m.graph.clusters()), 3)
        self.assertEqual(self.m.graph.members("b:one"), ["b:one"])
        self.assertFalse(self.m.evidence.fresh(self.m.graph.decision("ab")["evidence"]))
        self.assertTrue(self.m.graph.verify()["verified"])

    def test_decision_replay_and_stale_preview(self):
        preview = self.m.graph.preview("a:one", "b:one")
        first = self.m.graph.decide(
            "ab", "a:one", "b:one", "merge", preview["digest"], "operator", "same"
        )
        replay = self.m.graph.decide(
            "ab", "b:one", "a:one", "merge", preview["digest"], "operator", "same"
        )
        self.assertEqual(first, replay)
        with self.assertRaises(MeshError):
            self.m.graph.decide(
                "ab",
                "a:one",
                "b:one",
                "merge",
                preview["digest"],
                "operator",
                "different",
            )
        stale = self.m.graph.preview("a:one", "c:one")
        self.m.graph.rebuild()
        with self.assertRaises(MeshError):
            self.m.graph.decide(
                "ac", "a:one", "c:one", "merge", stale["digest"], "operator", "stale"
            )
        self.assertEqual(len(self.m.graph.clusters()), 2)

    def test_candidate_blocking_filters_same_source_and_tracks_capacity(self):
        report = self.m.candidates.for_record("a:one")
        self.assertEqual(report["eligible"], 2)
        self.assertTrue(report["complete_search"])
        self.assertEqual([r["right"] for r in report["candidates"]], ["b:one", "c:one"])
        limited = self.m.candidates.for_record("a:one", maximum_block=2)
        self.assertFalse(limited["complete_search"])
        self.assertTrue(limited["skipped_blocks"])
        self.assertEqual(limited["candidates"], [])
        self.assertTrue(self.m.candidates.coverage()["ok"])

    def test_batch_candidates_and_labeled_quality_are_explicit(self):
        report = self.m.candidates.batch(minimum_score=70)
        self.assertEqual(report["examined_pairs"], 3)
        self.assertEqual(report["eligible"], 3)
        labels = [
            {"left": "a:one", "right": "b:one", "same": True},
            {"left": "a:one", "right": "c:one", "same": False},
        ]
        quality = self.m.quality.labeled_pairs(labels, 70)
        self.assertEqual(quality["confusion"]["true_positive"], 1)
        self.assertEqual(quality["confusion"]["false_positive"], 1)
        self.assertEqual(quality["precision"], "1/2")
        self.assertEqual(self.m.quality.candidate_recall(labels)["recall"], "1")
        sweep = self.m.quality.threshold_sweep(labels, [70, 80])
        self.assertEqual(sweep["reports"][1]["confusion"]["false_negative"], 1)

    def test_graph_tamper_is_detected_without_repairing_away_evidence(self):
        self.decide("ab", "a:one", "b:one")
        self.m.store.connection.execute(
            "UPDATE components SET cluster='c:one' WHERE record='b:one'"
        )
        with self.assertRaises(MeshError) as caught:
            self.m.graph.verify()
        self.assertEqual(caught.exception.code, "integrity")
        self.assertEqual(self.m.graph.decision("ab")["state"], "active")
        self.m.graph.rebuild()
        self.assertTrue(self.m.graph.verify()["verified"])
        self.assertEqual(self.m.graph.cluster("b:one"), "a:one")
