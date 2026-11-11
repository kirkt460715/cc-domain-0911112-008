import argparse
import sys
from pathlib import Path
from .contracts import decode, canonical, MeshError, fail
from .service import ResearchMesh


def read_local(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 32000000:
        fail("path", "input must be bounded local regular file")
    return path.read_text("utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="researchmesh")
    parser.add_argument("--db", default="researchmesh.sqlite3")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo")
    commands.add_parser("describe")
    execute = commands.add_parser("run")
    execute.add_argument("request")
    ingest = commands.add_parser("ingest")
    ingest.add_argument("source")
    ingest.add_argument("batch")
    ingest.add_argument("generation", type=int)
    ingest.add_argument("file")
    ingest.add_argument("--expected-generation", type=int, required=True)
    ingest.add_argument("--partial", action="store_true")
    verify = commands.add_parser("verify")
    verify.add_argument("file")
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            from .demo import run

            result = run()
        elif args.command == "verify":
            from .snapshots.releases import Releases

            result = Releases.verify(args.file)
        else:
            with ResearchMesh(args.db) as mesh:
                if args.command == "describe":
                    result = mesh.describe()
                elif args.command == "ingest":
                    records = mesh.sources.parse(read_local(args.file))
                    result = mesh.sources.snapshot(
                        args.batch,
                        args.source,
                        args.generation,
                        records,
                        args.expected_generation,
                        not args.partial,
                    )
                else:
                    result = mesh.dispatch(decode(read_local(args.request)))
        print(canonical({"ok": True, "result": result}))
        return 0
    except MeshError as exc:
        print(canonical({"ok": False, "error": exc.record()}), file=sys.stderr)
        return 2
    except (OSError, UnicodeError) as exc:
        print(
            canonical({"ok": False, "error": {"code": "io", "message": str(exc)}}),
            file=sys.stderr,
        )
        return 3
