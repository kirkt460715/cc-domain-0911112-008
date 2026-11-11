from pathlib import Path
from tempfile import TemporaryDirectory
from .service import ResearchMesh


def run():
    with TemporaryDirectory() as directory:
        with ResearchMesh(Path(directory) / "mesh.db") as mesh:
            mesh.sources.register("lab", "Synthetic research laboratory", 10)
            mesh.sources.register("factory", "Synthetic industrial register", 20)
            first = {
                "title": "Precision sensor calibration",
                "institution": "Example Institute",
                "kind": "prototype",
                "year": 2026,
                "people": ["Example Researcher"],
                "identifiers": {"registry": ["local:instrument-1"]},
                "tags": ["instrument"],
            }
            second = {
                **first,
                "title": "Precision Sensor Calibration",
                "abstract": "Synthetic pilot implementation",
            }
            mesh.sources.snapshot(
                "lab1", "lab", 1, [{"id": "sensor", "data": first}], 0
            )
            mesh.sources.snapshot(
                "factory1", "factory", 1, [{"id": "instrument", "data": second}], 0
            )
            preview = mesh.releases.preview()
            mesh.releases.freeze("separate", preview["digest"], "publisher")
            proposal = mesh.graph.preview("lab:sensor", "factory:instrument")
            assert proposal["allowed"]
            mesh.graph.decide(
                "merge",
                "lab:sensor",
                "factory:instrument",
                "merge",
                proposal["digest"],
                "reviewer",
                "same synthetic registration",
            )
            assert len(mesh.graph.clusters()) == 1
            preview = mesh.releases.preview()
            mesh.releases.freeze("merged", preview["digest"], "publisher")
            difference = mesh.releases.diff("separate", "merged")
            assert len(difference["merges"]) == 1
            assert mesh.graph.impact("merge")["splits_component"]
            mesh.graph.revoke(
                "merge",
                mesh.store.revision("graph"),
                "reviewer",
                "withdraw synthetic decision",
            )
            assert len(mesh.graph.clusters()) == 2
            preview = mesh.releases.preview()
            mesh.releases.freeze("split", preview["digest"], "publisher")
            assert len(mesh.releases.diff("merged", "split")["splits"]) == 1
            path = Path(directory) / "release.json"
            mesh.releases.export("merged", path)
            assert mesh.releases.verify(path)["verified"]
            assert mesh.quality.verify()["verified"]
            return {
                "status": "ok",
                "merged_masters": 1,
                "revoked_masters": 2,
                "immutable_release_verified": True,
                "offline": True,
            }
