import argparse
import json
import tempfile
from pathlib import Path

from .archive import Archive
from .client import Client, collect
from .decoding import records
from .directory import AmbiguousVersion, Directory, UnknownFacet
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


def directory_demo():
    """从本地合成来源接入目录，并分别展示确认、未读取与待补关系。"""
    from .origin import load_scenario

    scenario = load_scenario()
    with tempfile.TemporaryDirectory() as temporary, serve(scenario) as origin:
        directory = Directory(Path(temporary) / "directory.sqlite3")
        try:
            for item in scenario["source_records"]:
                directory.ingest_record(item)
            client = Client(origin.base_url)
            directory.fetch_detail(client, "archive-a", "/scim/v2/Users/u1", resource_type="Users")
            omega = directory.coverage("archive-a", "Users", "u1", version='W/"omega"')
            beta = directory.relations("archive-a", "g1", version='W/"beta"')
            zeta = directory.relations("archive-a", "g1", version='W/"zeta"')
            result = {
                "operation": "directory_coverage",
                "identities": directory.identities(),
                "group_facets": [
                    {
                        "version": facet["version"],
                        "membership": (zeta if facet["version"] == 'W/"zeta"' else beta)["membership"],
                        "items": (zeta if facet["version"] == 'W/"zeta"' else beta)["items"],
                    }
                    for facet in directory.versions("archive-a", "Groups", "g1")
                ],
                "user_facets": directory.versions("archive-a", "Users", "u1"),
                "user_confirmed": sorted(omega["confirmed_attributes"]),
                "user_unread_count": len(omega["unread_attributes"]),
                "pending_relations": directory.pending_relations(),
                "attempts": directory.attempts(),
            }
        finally:
            directory.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


def directory_report(archive_path, directory_path, tenant, resource_type, resource_id, version, observation):
    """接入一份已完成采集的归档，并按需查询覆盖范围。"""
    archive = Archive(archive_path)
    directory = Directory(directory_path)
    try:
        captures = archive.captures()
        ingested = []
        for capture in captures:
            if capture["status"] != 200:
                continue
            for result in directory.ingest_capture(archive, capture["id"], tenant=tenant, resource_type=resource_type):
                ingested.append(result.queries[0])
        selectors = {}
        if version is not None:
            selectors["version"] = version
        if observation is not None:
            selectors["observation"] = observation
        result = {
            "operation": "directory_report",
            "captures": len(captures),
            "queries_ingested": ingested,
            "identities": directory.identities(tenant=tenant, resource_type=resource_type),
            "attempts": directory.attempts(tenant=tenant, resource_type=resource_type),
        }
        try:
            result["coverage"] = directory.coverage(
                tenant, resource_type, resource_id, **selectors,
            )
        except AmbiguousVersion as error:
            result["coverage"] = {"ambiguous": str(error), "facets": error.facets}
        except UnknownFacet as error:
            result["coverage"] = {"unknown": str(error)}
        if resource_type.casefold().rstrip("s") == "group":
            try:
                result["relations"] = directory.relations(
                    tenant, resource_id, resource_type=resource_type, **selectors,
                )
            except AmbiguousVersion as error:
                result["relations"] = {"ambiguous": str(error), "facets": error.facets}
            except UnknownFacet as error:
                result["relations"] = {"unknown": str(error)}
        return result
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
    directory_demo_parser = subcommands.add_parser("directory-demo", help="展示目录覆盖与待补关系")
    report = subcommands.add_parser("directory-report", help="从已归档响应生成目录覆盖报告")
    report.add_argument("--archive", type=Path, required=True)
    report.add_argument("--directory", type=Path, required=True)
    report.add_argument("--tenant", required=True)
    report.add_argument("--resource-type", required=True)
    report.add_argument("--id", required=True)
    report.add_argument("--version", help="来源 meta.version 原值；有多个 facet 时必须指定")
    report.add_argument("--observation", help="来源 observation；meta.version 缺失时用它选择 facet")
    args = parser.parse_args()
    if args.command == "demo":
        with tempfile.TemporaryDirectory() as temporary, serve() as origin:
            result = capture(origin.base_url, Path(temporary) / "archive.sqlite3", "local-source", "/feed")
            print(json.dumps({"operation": "raw_capture", **result}, ensure_ascii=False, indent=2))
    elif args.command == "capture":
        print(json.dumps(capture(args.base_url, args.archive, args.source, args.path), ensure_ascii=False, indent=2))
    elif args.command == "directory-demo":
        directory_demo()
    elif args.command == "directory-report":
        print(json.dumps(
            directory_report(
                args.archive, args.directory, args.tenant, args.resource_type,
                args.id, args.version, args.observation,
            ),
            ensure_ascii=False, indent=2,
        ))
    else:
        from .origin import load_scenario
        with serve(load_scenario(args.scenario)) as origin:
            print(json.dumps({"base_url": origin.base_url}, ensure_ascii=False), flush=True)
            try:
                import threading
                threading.Event().wait()
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
