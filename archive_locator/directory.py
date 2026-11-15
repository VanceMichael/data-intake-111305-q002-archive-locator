"""按租户、资源类型和来源版本组织的 SCIM 目录覆盖视图。

这个模块解释已经收到的目录响应，不写入目录，也不把一次响应当成来源的完整
状态。刻意的边界：

* 身份是 ``(tenant, resource_type, id)``。userName、displayName、externalId
  都不是身份：改名不会新建资源，同名或相同 externalId 也不会跨租户归并。
* 同一资源的不同 ``meta.version`` 是不同 facet。跨 facet 只共享身份，不共享
  属性值；任何查询都不会返回一份拼接出来的“完整资源”。
* ``meta.version`` 是来源给出的不透明标识，本模块从不按字符串比较或排序。
* 非 2xx、SCIM Error 响应只是未读到的尝试，不是删除或空集合的证明。
* 请求过但响应中不存在的属性是未读取；响应里真正的空数组才是已观察到的
  空集合。
* 成员引用先于成员详情抵达时关系仍保留；``value`` 与 ``$ref`` 矛盾时待核，
  名称从不用于推断身份。
"""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from . import scim

_UNSET = object()


class DirectoryError(scim.ScimError):
    """目录观察或查询不符合来源契约。"""


class AmbiguousVersion(DirectoryError):
    """资源有多个来源版本 facet，调用方没有指定要查询哪一个。"""

    def __init__(self, tenant: str, resource_type: str, resource_id: str, facets: list[dict[str, Any]]):
        self.tenant = tenant
        self.resource_type = resource_type
        self.resource_id = resource_id
        self.facets = facets
        available = ", ".join(str(item.get("version")) for item in facets)
        super().__init__(
            f"{tenant}/{resource_type}/{resource_id} has {len(facets)} version facets ({available}); "
            "specify version, or observation when meta.version is unknown"
        )


class UnknownFacet(DirectoryError, LookupError):
    """找不到指定的版本 facet 或来源身份。"""


def _locked(method):
    """查询由多次读取组成；持锁保证单次读取不会看到写入的中间状态。"""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return wrapper


@dataclass(frozen=True)
class IngestResult:
    tenant: str
    resource_type: str
    status: int
    resource_ids: tuple[str, ...] = ()
    queries: tuple[int, ...] = ()
    failed: bool = False
    capture_id: int | None = None


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DirectoryError(f"{name} must be a non-empty string")
    return value.strip()


def _status(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 100 <= value <= 599:
        raise DirectoryError("status must be an HTTP status integer")
    return value


def _observation(value: Any) -> str:
    if value is None or isinstance(value, bool):
        raise DirectoryError("observation must identify the source query")
    text = str(value).strip()
    if not text:
        raise DirectoryError("observation must identify the source query")
    return text


def _scope(requested: tuple[str, ...] | None, excluded: tuple[str, ...] | None) -> str:
    if requested is not None:
        return scim.SCOPE_DECLARED
    if excluded is not None:
        return scim.SCOPE_ALL_EXCEPT
    return scim.SCOPE_UNKNOWN


class _Observation:
    __slots__ = (
        "sequence", "tenant", "resource_type", "resource_id", "source_observation",
        "version", "version_known", "status", "scope", "requested", "excluded",
        "present", "resource", "error", "url", "captured_at",
    )

    def __init__(self, row: sqlite3.Row):
        self.sequence = row["sequence"]
        self.tenant = row["tenant"]
        self.resource_type = row["resource_type"]
        self.resource_id = row["resource_id"]
        self.source_observation = row["observation"]
        self.version = row["version"]
        self.version_known = bool(row["version_known"])
        self.status = row["status"]
        self.scope = row["scope"]
        self.requested = None if row["requested"] is None else tuple(json.loads(row["requested"]))
        self.excluded = None if row["excluded"] is None else tuple(json.loads(row["excluded"]))
        self.present = tuple(json.loads(row["present"]))
        self.resource = None if row["resource"] is None else json.loads(row["resource"])
        self.error = None if row["error"] is None else json.loads(row["error"])
        self.url = row["url"]
        self.captured_at = row["captured_at"]

    @property
    def successful(self) -> bool:
        return self.resource is not None

    def describe(self) -> dict[str, Any]:
        return {
            "query": self.sequence,
            "observation": self.source_observation,
            "status": self.status,
            "version": self.version,
            "version_state": "known" if self.version_known else "unknown",
            "scope": self.scope,
            "requested": None if self.requested is None else list(self.requested),
            "excluded": None if self.excluded is None else list(self.excluded),
            "present": list(self.present),
            "url": self.url,
            "captured_at": self.captured_at,
        }


class _Facet:
    """同一来源版本下的全部成功观察。未知版本每次观察各自成 facet。"""

    def __init__(self, version: str | None, observations: list[_Observation]):
        self.version = version
        self.version_known = version is not None
        self.observations = observations

    def selector(self) -> dict[str, Any]:
        return {
            "version": self.version if self.version_known else None,
            "observation": (
                None if self.version_known
                else self.observations[0].source_observation or str(self.observations[0].sequence)
            ),
        }

    def summary(self) -> dict[str, Any]:
        return {
            "version": self.version if self.version_known else None,
            "version_state": "known" if self.version_known else "unknown",
            **self.selector(),
            "queries": [
                {"query": item.sequence, "observation": item.source_observation}
                for item in self.observations
            ],
            "statuses": [item.status for item in self.observations],
        }


class Directory:
    """只追加目录查询观察，在读取时计算版本 facet 的覆盖范围。

    每次写入和每条查询路径都在同一个锁内完成，读与写不会看到彼此的中间
    状态。增量重访与按需补详情可以并发到达，但它们进入的是各自的版本
    facet。
    """

    def __init__(self, path=":memory:"):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.lock = threading.RLock()
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS directory_queries (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                observation TEXT,
                version TEXT,
                version_known INTEGER NOT NULL,
                status INTEGER NOT NULL,
                scope TEXT NOT NULL,
                requested TEXT,
                excluded TEXT,
                present TEXT NOT NULL,
                resource TEXT,
                error TEXT,
                url TEXT,
                capture_id INTEGER,
                captured_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE INDEX IF NOT EXISTS directory_queries_identity
                ON directory_queries(tenant, resource_type, resource_id);
            CREATE TABLE IF NOT EXISTS directory_captures (
                capture_id INTEGER PRIMARY KEY,
                ingested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            """
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def close(self):
        with self.lock:
            self.connection.close()

    # ------------------------------------------------------------------
    # 接入
    # ------------------------------------------------------------------
    def ingest_record(
        self,
        record: Mapping[str, Any],
        *,
        status: int | None = None,
        url: str | None = None,
        captured_at: str | None = None,
    ) -> IngestResult:
        """接入一条目录信封（采集工具保留的原始记录）。

        信封的 ``id`` 是来源身份。成功状态却没有 ``resource`` 时拒绝写入，
        避免把“没有正文”解释成空资源。
        """
        if not isinstance(record, Mapping):
            raise DirectoryError("directory record must be an object")
        missing = [name for name in ("tenant", "resource_type", "id", "observation") if name not in record]
        if missing:
            raise DirectoryError("directory record missing " + ", ".join(missing))
        tenant = _text(record["tenant"], "tenant")
        resource_type = _text(record["resource_type"], "resource_type")
        resource_id = _text(record["id"], "id")
        observation = _observation(record["observation"])
        effective_status = _status(record.get("status", 200) if status is None else status)
        requested = scim.normalize_attributes(record.get("requested_attributes"), "requested_attributes")
        excluded = scim.normalize_attributes(record.get("excluded_attributes"), "excluded_attributes")
        if requested is not None and excluded is not None:
            raise DirectoryError("requested_attributes and excluded_attributes are mutually exclusive")
        resource = record.get("resource")
        if resource is not None and not isinstance(resource, Mapping):
            raise DirectoryError("resource must be an object")
        if isinstance(resource, Mapping):
            declared = self._payload_id(resource)
            if declared is not None and declared != resource_id:
                raise DirectoryError("resource id does not match envelope id")
        if isinstance(resource, Mapping) and scim.is_error_resource(resource):
            resource = None
            if 200 <= effective_status < 300:
                effective_status = 502
        if resource is not None and not 200 <= effective_status < 300:
            # 非成功状态伴随的正文只作为错误摘要，不能当资源读取。
            error = scim.error_summary(resource)
            resource = None
        elif resource is None:
            detail = record.get("error")
            error = scim.error_summary(detail if detail is not None else record.get("resource"))
        else:
            error = None
        if resource is None and 200 <= effective_status < 300:
            raise DirectoryError("successful directory record requires a resource object")
        version = scim.resource_version(resource) if resource is not None else None
        return self._insert(
            tenant=tenant,
            resource_type=resource_type,
            resource_id=resource_id,
            observation=observation,
            status=effective_status,
            requested=requested,
            excluded=excluded,
            present=scim.top_level_attributes(resource) if resource is not None else (),
            resource=resource,
            error=error,
            version=version,
            url=url,
            captured_at=captured_at,
        )

    def ingest_response(
        self,
        tenant: str,
        resource_type: str,
        response,
        *,
        resource_id: str | None = None,
        requested_attributes: Iterable[str] | None | object = _UNSET,
        excluded_attributes: Iterable[str] | None | object = _UNSET,
        url: str | None = None,
        captured_at: str | None = None,
    ) -> IngestResult:
        """接入一次 SCIM HTTP 响应，包括列表、详情和错误响应。

        投影范围优先取自显式参数，其次取自请求 URL 的 attributes /
        excludedAttributes。403 等状态只记录为未读尝试。
        """
        tenant = _text(tenant, "tenant")
        resource_type = _text(resource_type, "resource_type")
        status = _status(getattr(response, "status", None))
        request_url = url if url is not None else getattr(response, "url", None)
        requested, excluded, scope = self._projection(requested_attributes, excluded_attributes, request_url)
        payload = self._payload(response)
        failed = not 200 <= status < 300 or (
            isinstance(payload, Mapping) and scim.is_error_resource(payload)
        )
        if failed:
            declared = payload.get("id") if isinstance(payload, Mapping) else None
            parsed = scim.parse_member_reference(request_url) if request_url else None
            target_id = resource_id or (declared.strip() if isinstance(declared, str) and declared.strip() else None)
            if target_id is None and parsed is not None:
                target_id = parsed[1]
            if target_id is None:
                raise DirectoryError("cannot associate failed response with a resource id")
            error = scim.error_summary(payload if payload is not None else getattr(response, "body", b"")[:200])
            return self._insert(
                tenant=tenant,
                resource_type=resource_type,
                resource_id=_text(target_id, "resource_id"),
                observation=None,
                status=status,
                requested=requested,
                excluded=excluded,
                present=(),
                resource=None,
                error=error,
                version=None,
                url=request_url,
                captured_at=captured_at,
                scope=scope,
            )
        if not isinstance(payload, Mapping):
            raise DirectoryError("successful SCIM response must be a JSON object")
        listed = payload.get("Resources")
        if isinstance(listed, list):
            identifiers: list[str] = []
            queries: list[int] = []
            for item in listed:
                if not isinstance(item, Mapping):
                    raise DirectoryError("SCIM list response resources must be objects")
                item_id = self._payload_id(item)
                if item_id is None:
                    raise DirectoryError("SCIM list response resource missing id")
                result = self._ingest_resource(
                    tenant, resource_type, item_id, item, status,
                    requested, excluded, request_url, captured_at, scope,
                )
                identifiers.append(item_id)
                queries.append(result.queries[0])
            return IngestResult(tenant, resource_type, status, tuple(identifiers), tuple(queries))
        target_id = resource_id or self._payload_id(payload)
        if target_id is None:
            parsed = scim.parse_member_reference(request_url) if request_url else None
            target_id = parsed[1] if parsed is not None else None
        if target_id is None:
            raise DirectoryError("SCIM response missing resource id")
        return self._ingest_resource(
            tenant, resource_type, target_id, payload, status,
            requested, excluded, request_url, captured_at, scope,
        )

    def ingest_capture(
        self,
        archive,
        capture_id: int,
        *,
        tenant: str | None = None,
        resource_type: str | None = None,
        decoder=None,
    ) -> list[IngestResult]:
        """把归档库中的一次响应解码为目录观察，原始内容仍由 archive 保留。

        同一 capture 重复接入是幂等的，避免重跑报告时把同一响应变成多次
        观察；返回的 ``capture_id`` 指向原始归档。
        """
        capture = archive.capture(capture_id)
        body = archive.body(capture_id)
        if capture["status"] != 200:
            raise DirectoryError("only complete successful captures are decoded as directory records")
        with self.lock:
            marker = self.connection.execute(
                "SELECT capture_id FROM directory_captures WHERE capture_id=?", (capture_id,)
            ).fetchone()
            if marker is not None:
                return []
            if decoder is None:
                from .decoding import records as decoder
            payload = decoder(body, capture["headers"].get("content-type", ""))
            results = []
            for record in payload:
                if not isinstance(record, Mapping):
                    raise DirectoryError("decoded source records must be objects")
                if tenant is not None and str(record.get("tenant")) != tenant:
                    continue
                if resource_type is not None and str(record.get("resource_type")) != resource_type:
                    continue
                result = self.ingest_record(
                    record,
                    status=capture["status"],
                    url=capture["url"],
                    captured_at=capture["received_at"],
                )
                results.append(
                    IngestResult(
                        result.tenant, result.resource_type, result.status,
                        result.resource_ids, result.queries, result.failed, capture_id,
                    )
                )
            with self.connection:
                self.connection.execute("INSERT INTO directory_captures(capture_id) VALUES (?)", (capture_id,))
            return results

    def fetch_detail(
        self,
        client,
        tenant: str,
        path: str,
        *,
        resource_type: str | None = None,
        resource_id: str | None = None,
        attributes: Iterable[str] | None = None,
        excluded_attributes: Iterable[str] | None = None,
        archive=None,
        source: str | None = None,
        headers: Mapping[str, str] | None = None,
        captured_at: str | None = None,
    ) -> IngestResult:
        """按需补一份详情，并作为独立查询观察接入。

        详情按自己的 ``meta.version`` 归入 facet；与已有观察版本不同时不会
        混用属性。非成功响应写入尝试记录，不写资源。
        """
        requested = scim.normalize_attributes(attributes, "attributes")
        excluded = scim.normalize_attributes(excluded_attributes, "excludedAttributes")
        if requested is not None and excluded is not None:
            raise DirectoryError("attributes and excludedAttributes are mutually exclusive")
        query: list[tuple[str, str]] = []
        if requested is not None:
            query.append(("attributes", ",".join(requested)))
        if excluded is not None:
            query.append(("excludedAttributes", ",".join(excluded)))
        request_path = path
        if query:
            request_path += ("&" if "?" in path else "?") + urlencode(query)
        response = client.fetch(request_path, dict(headers or {}))
        parsed = scim.parse_member_reference(path)
        resolved_type = resource_type or (parsed[0] if parsed is not None else None)
        if resolved_type is None:
            raise DirectoryError("resource_type is required for this detail path")
        capture_id = None
        if archive is not None:
            capture_id = archive.append(
                source or tenant, response.url, response.status, response.headers, response.body,
            )
        declared = self._payload_id(self._payload(response))
        target_id = declared or resource_id or (parsed[1] if parsed is not None else None)
        if target_id is None:
            raise DirectoryError("cannot derive resource id from detail path")
        result = self.ingest_response(
            tenant,
            resolved_type,
            response,
            resource_id=target_id,
            requested_attributes=requested if requested is not None else _UNSET,
            excluded_attributes=excluded if excluded is not None else _UNSET,
            url=request_path,
            captured_at=captured_at,
        )
        return IngestResult(
            result.tenant, result.resource_type, result.status, result.resource_ids,
            result.queries, result.failed, capture_id,
        )

    @staticmethod
    def _payload(response) -> Any:
        body = getattr(response, "body", b"")
        if not body:
            return None
        media_type = str(getattr(response, "headers", {}).get("content-type", "")).partition(";")[0].strip().lower()
        if media_type not in ("application/json", "application/scim+json"):
            return None
        try:
            return json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DirectoryError("source response is not valid JSON") from error

    @staticmethod
    def _payload_id(payload: Any) -> str | None:
        if isinstance(payload, Mapping):
            value = payload.get("id")
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _projection(self, requested, excluded, url):
        if requested is _UNSET and excluded is _UNSET:
            if url:
                return scim.query_projection(url)
            return None, None, scim.SCOPE_UNKNOWN
        if requested is _UNSET:
            requested = None
        if excluded is _UNSET:
            excluded = None
        requested = scim.normalize_attributes(requested, "requested_attributes")
        excluded = scim.normalize_attributes(excluded, "excluded_attributes")
        if requested is not None and excluded is not None:
            raise DirectoryError("requested_attributes and excluded_attributes are mutually exclusive")
        return requested, excluded, _scope(requested, excluded)

    def _ingest_resource(
        self, tenant, resource_type, resource_id, resource, status,
        requested, excluded, url, captured_at, scope,
    ) -> IngestResult:
        declared = resource.get("id")
        if isinstance(declared, str) and declared.strip() and declared.strip() != resource_id:
            raise DirectoryError("resource id does not match requested identity")
        return self._insert(
            tenant=tenant,
            resource_type=resource_type,
            resource_id=resource_id,
            observation=None,
            status=status,
            requested=requested,
            excluded=excluded,
            present=scim.top_level_attributes(resource),
            resource=resource,
            error=None,
            version=scim.resource_version(resource),
            url=url,
            captured_at=captured_at,
            scope=scope,
        )

    def _insert(
        self, *, tenant, resource_type, resource_id, observation, status,
        requested, excluded, present, resource, error, version, url, captured_at,
        scope=None,
    ) -> IngestResult:
        with self.lock, self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO directory_queries(
                    tenant, resource_type, resource_id, observation, version,
                    version_known, status, scope, requested, excluded, present,
                    resource, error, url, captured_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,COALESCE(?, strftime('%Y-%m-%dT%H:%M:%fZ','now')))
                """,
                (
                    tenant, resource_type, resource_id, observation, version,
                    1 if version is not None else 0, status,
                    _scope(requested, excluded) if scope is None else scope,
                    None if requested is None else json.dumps(list(requested), ensure_ascii=False),
                    None if excluded is None else json.dumps(list(excluded), ensure_ascii=False),
                    json.dumps(list(present), ensure_ascii=False),
                    None if resource is None else json.dumps(dict(resource), ensure_ascii=False, sort_keys=True),
                    None if error is None else json.dumps(error, ensure_ascii=False, sort_keys=True),
                    url,
                    captured_at,
                ),
            )
        return IngestResult(
            tenant, resource_type, status, (resource_id,), (cursor.lastrowid,),
            failed=resource is None,
        )

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def _rows(self, tenant=None, resource_type=None, resource_id=None) -> list[_Observation]:
        query = "SELECT * FROM directory_queries"
        clauses: list[str] = []
        params: list[Any] = []
        for name, value in (("tenant", tenant), ("resource_type", resource_type), ("resource_id", resource_id)):
            if value is not None:
                clauses.append(f"{name}=?")
                params.append(value)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY sequence"
        with self.lock:
            return [_Observation(row) for row in self.connection.execute(query, params).fetchall()]

    @_locked
    def identities(self, tenant=None, resource_type=None) -> list[dict[str, Any]]:
        """按来源身份汇总查询情况；身份只由 tenant、resource_type、id 组成。"""
        seen: dict[tuple[str, str, str], dict[str, Any]] = {}
        order: list[tuple[str, str, str]] = []
        for item in self._rows(tenant, resource_type):
            key = (item.tenant, item.resource_type, item.resource_id)
            if key not in seen:
                seen[key] = {
                    "tenant": item.tenant,
                    "resource_type": item.resource_type,
                    "id": item.resource_id,
                    "queries": 0,
                    "failed_queries": 0,
                }
                order.append(key)
            seen[key]["queries"] += 1
            if not item.successful:
                seen[key]["failed_queries"] += 1
        return [seen[key] for key in order]

    @_locked
    def versions(self, tenant: str, resource_type: str, resource_id: str) -> list[dict[str, Any]]:
        """列出一个资源已读取的版本 facet；顺序是到达顺序，不代表版本先后。"""
        facets = self._facets(self._successful(tenant, resource_type, resource_id))
        if not facets:
            raise UnknownFacet(f"no successful observation for {tenant}/{resource_type}/{resource_id}")
        return [facet.summary() for facet in facets]

    @_locked
    def queries(
        self, tenant: str, resource_type: str, resource_id: str,
        *, version: Any = _UNSET, observation: Any = _UNSET,
    ) -> list[dict[str, Any]]:
        facet = self._select(tenant, resource_type, resource_id, version, observation)
        return [item.describe() for item in facet.observations]

    @_locked
    def attempts(self, tenant=None, resource_type=None, resource_id=None) -> list[dict[str, Any]]:
        """列出未成功的查询；这些响应不是删除、清空或资源状态。"""
        result = []
        for item in self._rows(tenant, resource_type, resource_id):
            if item.successful:
                continue
            described = item.describe()
            described["error"] = item.error
            described["effect"] = "unread_attempt"
            result.append(described)
        return result

    def _successful(self, tenant, resource_type, resource_id) -> list[_Observation]:
        return [item for item in self._rows(tenant, resource_type, resource_id) if item.successful]

    @staticmethod
    def _facets(rows: list[_Observation]) -> list[_Facet]:
        groups: dict[Any, list[_Observation]] = {}
        order: list[Any] = []
        for item in rows:
            if not item.successful:
                continue
            key = ("version", item.version) if item.version_known else ("query", item.sequence)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(item)
        return [_Facet(groups[key][0].version if groups[key][0].version_known else None, groups[key]) for key in order]

    def _select(self, tenant, resource_type, resource_id, version=_UNSET, observation=_UNSET) -> _Facet:
        facets = self._facets(self._successful(tenant, resource_type, resource_id))
        if not facets:
            raise UnknownFacet(f"no successful observation for {tenant}/{resource_type}/{resource_id}")
        candidates = facets
        if observation is not _UNSET:
            wanted = str(observation).strip()
            candidates = [
                facet for facet in candidates
                if any(
                    str(item.sequence) == wanted
                    or (item.source_observation is not None and str(item.source_observation) == wanted)
                    for item in facet.observations
                )
            ]
            if not candidates:
                raise UnknownFacet(f"observation {observation} not found for {tenant}/{resource_type}/{resource_id}")
        if version is not _UNSET:
            if version is None:
                candidates = [facet for facet in candidates if not facet.version_known]
            else:
                candidates = [
                    facet for facet in candidates
                    if facet.version_known and facet.version == version
                ]
            if not candidates:
                raise UnknownFacet(f"version {version!r} not found for {tenant}/{resource_type}/{resource_id}")
        if len(candidates) > 1:
            raise AmbiguousVersion(
                tenant, resource_type, resource_id, [facet.summary() for facet in candidates],
            )
        return candidates[0]

    @_locked
    def coverage(
        self, tenant: str, resource_type: str, resource_id: str,
        *, version: Any = _UNSET, observation: Any = _UNSET,
    ) -> dict[str, Any]:
        """解释一个版本 facet 中已确认、未读取和相互冲突的属性。

        读取从不跨 facet 合并，因此结果总是某个来源版本下的视图。
        """
        facet = self._select(tenant, resource_type, resource_id, version, observation)
        observations = facet.observations
        inventory = scim.inventory(resource_type)

        values: dict[str, list[tuple[Any, _Observation]]] = {}
        order: list[str] = []
        for item in observations:
            for name, value in (item.resource or {}).items():
                name = str(name)
                if name not in values:
                    values[name] = []
                    order.append(name)
                values[name].append((value, item))

        confirmed: dict[str, Any] = {}
        provenance: dict[str, list[dict[str, Any]]] = {}
        conflicted: list[dict[str, Any]] = []
        for name in order:
            pairs = values[name]
            if len({scim.canonical(value) for value, _ in pairs}) == 1:
                confirmed[name] = pairs[0][0]
                provenance[name] = [
                    {"query": item.sequence, "observation": item.source_observation}
                    for _, item in pairs
                ]
            else:
                conflicted.append({
                    "name": name,
                    "state": "conflicted",
                    "reason": "observations of the same source version disagree on this attribute",
                    "candidates": [
                        {"value": value, "query": item.sequence, "observation": item.source_observation}
                        for value, item in pairs
                    ],
                })

        unread = self._facet_unread(observations, inventory)
        query_descriptions = []
        for item in observations:
            described = item.describe()
            described["unread"] = self._query_unread(item, inventory)
            query_descriptions.append(described)

        reasons: list[str] = []
        if unread:
            reasons.append("some attributes were not read")
        if conflicted:
            reasons.append("some attribute values conflict within this version")
        if any(item.scope == scim.SCOPE_UNKNOWN for item in observations):
            reasons.append("request projection was not captured for every query")
        state = "known_inventory_read" if not reasons else "partial"
        return {
            "tenant": tenant,
            "resource_type": resource_type,
            "id": resource_id,
            "facet": facet.summary(),
            "version": facet.version if facet.version_known else None,
            "version_state": "known" if facet.version_known else "unknown",
            "confirmed_attributes": confirmed,
            "attribute_provenance": provenance,
            "unread_attributes": unread,
            "conflicted_attributes": conflicted,
            "queries": query_descriptions,
            "coverage": {
                "state": state,
                "complete": state == "known_inventory_read",
                "reasons": reasons,
                "inventory": list(inventory),
            },
        }

    @_locked
    def confirmed_attributes(
        self, tenant: str, resource_type: str, resource_id: str,
        *, version: Any = _UNSET, observation: Any = _UNSET,
    ) -> dict[str, Any]:
        return self.coverage(
            tenant, resource_type, resource_id, version=version, observation=observation,
        )["confirmed_attributes"]

    @_locked
    def unread_attributes(
        self, tenant: str, resource_type: str, resource_id: str,
        *, version: Any = _UNSET, observation: Any = _UNSET,
    ) -> list[dict[str, Any]]:
        return self.coverage(
            tenant, resource_type, resource_id, version=version, observation=observation,
        )["unread_attributes"]

    @staticmethod
    def _query_unread(item: _Observation, inventory: tuple[str, ...]) -> list[str]:
        resource = item.resource or {}
        unread: list[str] = []
        if item.scope == scim.SCOPE_DECLARED:
            for path in item.requested or ():
                if not scim.path_present(resource, path):
                    unread.append(path)
        elif item.scope in (scim.SCOPE_ALL, scim.SCOPE_ALL_EXCEPT):
            excluded = {str(name).casefold() for name in (item.excluded or ())}
            for name in inventory:
                if name.casefold() not in excluded and not scim.path_present(resource, name):
                    unread.append(name)
        return unread

    @staticmethod
    def _facet_unread(
        observations: list[_Observation], inventory: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        present = {
            str(name).casefold()
            for item in observations for name in (item.resource or {})
        }
        explicit: list[str] = []
        for item in observations:
            for path in item.requested or ():
                if path not in explicit:
                    explicit.append(path)
        result: list[dict[str, Any]] = []
        recorded: set[str] = set()
        for path in explicit:
            if any(scim.path_present(item.resource or {}, path) for item in observations):
                continue
            result.append({
                "name": path,
                "state": "unread",
                "reason": "requested_but_absent",
                "requested_by": [
                    {"query": item.sequence, "observation": item.source_observation}
                    for item in observations if path in (item.requested or ())
                ],
            })
            recorded.add(path.casefold())
            recorded.add(path.split(".")[0].casefold())
        for name in inventory:
            lowered = name.casefold()
            if lowered in present or lowered in recorded:
                continue
            implicitly_read = False
            excluded_everywhere = bool(observations)
            declared_only = bool(observations)
            scope_unknown = False
            for item in observations:
                excluded = {str(value).casefold() for value in (item.excluded or ())}
                if item.scope == scim.SCOPE_ALL:
                    implicitly_read = True
                elif item.scope == scim.SCOPE_ALL_EXCEPT and lowered not in excluded:
                    implicitly_read = True
                if not (item.scope == scim.SCOPE_ALL_EXCEPT and lowered in excluded):
                    excluded_everywhere = False
                if item.scope != scim.SCOPE_DECLARED:
                    declared_only = False
                if item.scope == scim.SCOPE_UNKNOWN:
                    scope_unknown = True
            if implicitly_read:
                reason = "not_returned"
            elif excluded_everywhere:
                reason = "not_requested"
            elif declared_only:
                reason = "not_requested"
            elif scope_unknown:
                reason = "request_scope_unknown"
            else:
                reason = "not_requested"
            result.append({"name": name, "state": "unread", "reason": reason})
        return result

    # ------------------------------------------------------------------
    # 成员关系
    # ------------------------------------------------------------------
    @_locked
    def relations(
        self, tenant: str, resource_id: str, *,
        resource_type: str = "Groups",
        version: Any = _UNSET, observation: Any = _UNSET,
    ) -> dict[str, Any]:
        """解释一个 Group 版本 facet 的成员关系及其覆盖范围。"""
        coverage = self.coverage(
            tenant, resource_type, resource_id, version=version, observation=observation,
        )
        members = coverage["confirmed_attributes"].get("members", _UNSET)
        conflict = next(
            (item for item in coverage["conflicted_attributes"] if item["name"].casefold() == "members"),
            None,
        )
        unread = next(
            (item for item in coverage["unread_attributes"] if item["name"].casefold() == "members"),
            None,
        )
        if conflict is not None:
            state = "members_conflicted"
            items: list[dict[str, Any]] = []
            reason: str | None = conflict["reason"]
        elif members is _UNSET:
            state = "members_unread"
            items = []
            reason = unread["reason"] if unread else "not_read"
        elif not isinstance(members, list):
            state = "members_invalid"
            items = []
            reason = "members attribute is not an array in the source response"
        else:
            state = "confirmed"
            reason = None
            items = [self._relation_item(tenant, member) for member in members]
        return {
            "tenant": tenant,
            "resource_type": resource_type,
            "id": resource_id,
            "version": coverage["version"],
            "version_state": coverage["version_state"],
            "facet": coverage["facet"],
            "membership": {
                "state": state,
                "reason": reason,
                "read": state == "confirmed",
                "empty": state == "confirmed" and not items,
            },
            "items": items,
            "pending": [item for item in items if item["state"] != "confirmed"],
            "coverage": {
                "confirmed": list(coverage["confirmed_attributes"]),
                "unread": [entry["name"] for entry in coverage["unread_attributes"]],
                "conflicted": [entry["name"] for entry in coverage["conflicted_attributes"]],
            },
        }

    def _relation_item(self, tenant: str, member: Any) -> dict[str, Any]:
        if not isinstance(member, Mapping):
            return {
                "value": None, "$ref": None, "display": None,
                "state": "unresolved", "reason": "member entry is not an object",
                "target": None, "candidates": [],
            }
        value = member.get("value")
        reference = member.get("$ref")
        value_id = value.strip() if isinstance(value, str) and value.strip() else None
        parsed = scim.parse_member_reference(reference)
        ref_type, ref_id = parsed if parsed is not None else (None, None)
        base = {
            "value": value,
            "$ref": reference,
            "display": member.get("display"),
            "target": None,
            "candidates": [],
        }
        if value_id is not None and ref_id is not None and value_id != ref_id:
            return {
                **base,
                "state": "conflict",
                "reason": "value and $ref identify different resources; hold for review",
                "candidates": [
                    {"resource_type": ref_type, "id": ref_id, "from": "$ref"},
                    {"resource_type": None, "id": value_id, "from": "value"},
                ],
            }
        if ref_type is not None and ref_id is not None:
            target_type, target_id = ref_type, ref_id
        elif value_id is not None:
            matches = self._resources_with_id(tenant, value_id)
            if len(matches) > 1:
                return {
                    **base,
                    "state": "ambiguous_target",
                    "reason": "member value matches more than one resource type in this tenant",
                    "candidates": matches,
                }
            target_type = matches[0]["resource_type"] if matches else None
            target_id = value_id
            exists = self._target_exists(tenant, target_type, target_id) if target_type else False
            return {
                **base,
                "state": "confirmed" if exists else "pending_target",
                "reason": None if exists else (
                    "member detail has not been read; resource type not yet observed"
                    if target_type is None else "member detail has not been read"
                ),
                "target": {"tenant": tenant, "resource_type": target_type, "id": target_id},
                "candidates": [],
            }
        else:
            return {
                **base,
                "state": "unresolved",
                "reason": "member has no usable value or $ref; names are not identity",
                "candidates": [],
            }
        exists = self._target_exists(tenant, target_type, target_id)
        return {
            **base,
            "state": "confirmed" if exists else "pending_target",
            "reason": None if exists else "member detail has not been read",
            "target": {"tenant": tenant, "resource_type": target_type, "id": target_id},
            "candidates": [],
        }

    def _resources_with_id(self, tenant: str, resource_id: str) -> list[dict[str, Any]]:
        return [
            {"tenant": item["tenant"], "resource_type": item["resource_type"], "id": item["id"]}
            for item in self.identities(tenant=tenant)
            if item["id"] == resource_id
        ]

    def _target_exists(self, tenant: str, resource_type: str, resource_id: str) -> bool:
        return any(
            item.successful
            for item in self._rows(tenant, resource_type, resource_id)
        )

    @_locked
    def pending_relations(self, tenant=None, resource_type: str = "Groups") -> list[dict[str, Any]]:
        """列出需要补详情的成员关系，以及成员属性本身未读取或冲突的 Group facet。"""
        result: list[dict[str, Any]] = []
        for identity in self.identities(tenant=tenant, resource_type=resource_type):
            facets = self._facets(self._successful(identity["tenant"], identity["resource_type"], identity["id"]))
            for facet in facets:
                selector = facet.selector()
                relations = self.relations(
                    identity["tenant"], identity["id"],
                    resource_type=identity["resource_type"],
                    version=selector["version"] if facet.version_known else _UNSET,
                    observation=_UNSET if facet.version_known else selector["observation"],
                )
                common = {
                    "tenant": identity["tenant"],
                    "resource_type": identity["resource_type"],
                    "id": identity["id"],
                    "version": relations["version"],
                    "version_state": relations["version_state"],
                }
                if relations["membership"]["state"] != "confirmed":
                    result.append({
                        **common,
                        "kind": "members",
                        "state": relations["membership"]["state"],
                        "reason": relations["membership"]["reason"],
                    })
                for item in relations["pending"]:
                    result.append({**common, "kind": "member", "member": item})
        return result
