import argparse
import json
import tempfile
from pathlib import Path

from .archive import Archive
from .client import Client, collect
from .decoding import records
from .directory import Directory, fetch_scim_resource
from .origin import serve


def capture(base_url, archive_path, source, path):
    archive = Archive(archive_path)
    try:
        capture_id, response = collect(Client(base_url), archive, source, path)
        result = {"capture_id": capture_id, "status": response.status, "bytes": len(response.body), "url": response.url}
        if response.status == 200:
            result["source_records"] = records(response.body, response.headers.get("content-type", ""))
        result["archived_observations"] = len(archive.captures())
        return result
    finally:
        archive.close()


def ingest_feed(base_url, archive_path, directory_path, source, path):
    """抓取一次 SCIM 信封列表：原始响应入归档库，逐条观察入目录库。"""
    archive = Archive(archive_path)
    directory = Directory(directory_path)
    try:
        capture_id, response = collect(Client(base_url), archive, source, path)
        ingested = 0
        if response.status == 200:
            for envelope in records(response.body, response.headers.get("content-type", "")):
                directory.ingest_observation(envelope, capture_id=capture_id)
                ingested += 1
        return {
            "operation": "directory_ingest",
            "capture_id": capture_id,
            "status": response.status,
            "ingested_observations": ingested,
            "known_resources": directory.resources(),
        }
    finally:
        directory.close()
        archive.close()


def main():
    parser = argparse.ArgumentParser(description="来源响应接收与归档")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("demo")
    collector = subcommands.add_parser("capture")
    collector.add_argument("--base-url", required=True)
    collector.add_argument("--archive", type=Path, required=True)
    collector.add_argument("--source", required=True)
    collector.add_argument("--path", default="/feed")
    server = subcommands.add_parser("serve")
    server.add_argument("--scenario", type=Path)
    directory = subcommands.add_parser("directory", help="SCIM 目录解释")
    directory_actions = directory.add_subparsers(dest="directory_action", required=True)

    ingest = directory_actions.add_parser("ingest", help="抓取信封列表并登记观察")
    ingest.add_argument("--base-url", required=True)
    ingest.add_argument("--archive", type=Path, required=True)
    ingest.add_argument("--directory", type=Path, required=True)
    ingest.add_argument("--source", required=True)
    ingest.add_argument("--path", default="/feed")

    show = directory_actions.add_parser("show", help="查看一个资源的覆盖范围")
    show.add_argument("--directory", type=Path, required=True)
    show.add_argument("--tenant", required=True)
    show.add_argument("--resource-type", required=True)
    show.add_argument("--id", required=True)

    pending = directory_actions.add_parser("pending", help="列出待补成员关系")
    pending.add_argument("--directory", type=Path, required=True)
    pending.add_argument("--tenant")

    fetch_one = directory_actions.add_parser("fetch", help="按需补单个资源详情")
    fetch_one.add_argument("--base-url", required=True)
    fetch_one.add_argument("--archive", type=Path, required=True)
    fetch_one.add_argument("--directory", type=Path, required=True)
    fetch_one.add_argument("--tenant", required=True)
    fetch_one.add_argument("--resource-type", required=True)
    fetch_one.add_argument("--id", required=True)
    fetch_one.add_argument("--attributes", help="逗号分隔的 SCIM attributes 投影")
    fetch_one.add_argument("--source")

    args = parser.parse_args()
    if args.command == "demo":
        with tempfile.TemporaryDirectory() as temporary, serve() as origin:
            raw_result = capture(origin.base_url, Path(temporary) / "archive.sqlite3", "local-source", "/feed")
            directory_result = ingest_feed(
                origin.base_url,
                Path(temporary) / "archive.sqlite3",
                Path(temporary) / "directory.sqlite3",
                "local-source",
                "/feed",
            )
            print(json.dumps(
                {"operation": "raw_capture", **{k: v for k, v in raw_result.items() if k != "source_records"}},
                ensure_ascii=False, indent=2,
            ))
            print(json.dumps(directory_result, ensure_ascii=False, indent=2))
    elif args.command == "capture":
        print(json.dumps(capture(args.base_url, args.archive, args.source, args.path), ensure_ascii=False, indent=2))
    elif args.command == "serve":
        from .origin import load_scenario
        with serve(load_scenario(args.scenario)) as origin:
            print(json.dumps({"base_url": origin.base_url}, ensure_ascii=False), flush=True)
            try:
                import threading
                threading.Event().wait()
            except KeyboardInterrupt:
                pass
    elif args.command == "directory":
        if args.directory_action == "ingest":
            print(json.dumps(
                ingest_feed(args.base_url, args.archive, args.directory, args.source, args.path),
                ensure_ascii=False, indent=2,
            ))
        elif args.directory_action == "show":
            db = Directory(args.directory)
            try:
                print(json.dumps(
                    db.resource_view(args.tenant, args.resource_type, args.id),
                    ensure_ascii=False, indent=2,
                ))
            finally:
                db.close()
        elif args.directory_action == "pending":
            db = Directory(args.directory)
            try:
                print(json.dumps(db.pending_relationships(args.tenant), ensure_ascii=False, indent=2))
            finally:
                db.close()
        else:
            archive = Archive(args.archive)
            db = Directory(args.directory)
            try:
                attributes = args.attributes.split(",") if args.attributes else None
                capture_id, response, outcome = fetch_scim_resource(
                    Client(args.base_url), archive, db, args.tenant,
                    args.resource_type, args.id, attributes=attributes, source=args.source,
                )
                print(json.dumps(
                    {"capture_id": capture_id, "status": response.status, "outcome": outcome,
                     "url": response.url},
                    ensure_ascii=False, indent=2,
                ))
            finally:
                db.close()
                archive.close()


if __name__ == "__main__":
    main()
