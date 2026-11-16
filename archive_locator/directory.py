"""SCIM 目录接入：按租户与资源类型解释来源观察的覆盖范围。

与 ``archive.py`` 的原始响应留存相互独立：本模块把采集信封
（tenant/resource_type/id/observation/requested_attributes + SCIM resource）
解释成可查询的目录状态，并严格保留每次观察的请求属性、实际返回属性及
``meta.version`` 来源版本标识。

解释规则（与目录管理员的约束一致）：

* 资源身份是来源 ID：身份键为 ``(tenant, resource_type, id)``。改 userName、
  同名 externalId 都不会新建或归并资源；不同租户即使 ID/externalId 相同也各自独立。
* 列表未返回某属性只表示“未读取”，与明确返回空集合（如 ``members: []``）不同；
  权限不足（403）是拒绝证据，不是删除证明。
* ``meta.version`` 是不透明来源版本标识，只做相等比较，绝不按字符串排序。
* 不同版本观察到的属性按版本切面（facet）分别保留，不拼成看似完整的资源。
* 组成员引用可能先于成员详情抵达；``$ref`` 与 ``value`` 矛盾时保持待核，
  不凭 displayName 等名字补齐身份。
"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit

# 未携带 meta.version 的观察归入独立切面；该标记不得与真实版本做排序比较。
UNVERSIONED = object()

# 组成员关系状态
MEMBER_CONFIRMED = "confirmed"
MEMBER_PENDING = "pending"

# 待核原因
PENDING_REFERENCE_CONFLICT = "ref_value_conflict"
PENDING_DETAIL_MISSING = "member_detail_missing"


def _normalise_identity(value, field):
    text = str(value).strip()
    if not text:
        raise ValueError(field + " must be non-empty")
    return text


def _normalise_requested(raw):
    if not isinstance(raw, list) or any(not isinstance(name, str) or not name.strip() for name in raw):
        raise ValueError("requested_attributes must be a list of attribute names")
    return [name.strip() for name in raw]


def _resource_version(resource):
    """提取不透明版本标识；缺失时返回 UNVERSIONED，绝不推断顺序。"""
    meta = resource.get("meta")
    if isinstance(meta, dict) and "version" in meta and meta["version"] is not None:
        return str(meta["version"])
    return UNVERSIONED


def ref_identity(ref):
    """从 SCIM ``$ref`` 中提取 (resource_type, id)，无法解析时返回 None。

    仅解析来源相对路径（如 ``/scim/v2/Users/u9``），不做网络访问，
    也不接受绝对地址带来的跨源跳转。
    """
    if not isinstance(ref, str) or not ref.strip():
        return None
    parts = [unquote(part) for part in urlsplit(ref.strip()).path.split("/") if part]
    marker = None
    for index, part in enumerate(parts):
        if part.lower() == "scim" and index + 1 < len(parts) and parts[index + 1] == "v2":
            marker = index + 2
            break
    if marker is None or marker + 1 >= len(parts):
        return None
    resource_type, resource_id = parts[marker], parts[marker + 1]
    if not resource_type or not resource_id:
        return None
    return resource_type, resource_id


class Directory:
    """SCIM 目录状态库。附加式留存观察，查询只解释不覆盖。"""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS scim_resources (
                resource_id INTEGER PRIMARY KEY,
                tenant TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                UNIQUE(tenant, resource_type, source_id)
            );
            CREATE TABLE IF NOT EXISTS scim_observations (
                observation_id INTEGER PRIMARY KEY,
                resource_id INTEGER NOT NULL REFERENCES scim_resources(resource_id),
                source_observation INTEGER,
                requested_attributes TEXT NOT NULL,
                returned_attributes TEXT NOT NULL,
                version TEXT,
                has_version INTEGER NOT NULL,
                capture_id INTEGER,
                observed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS scim_attributes (
                observation_id INTEGER NOT NULL REFERENCES scim_observations(observation_id),
                name TEXT NOT NULL,
                value_json TEXT NOT NULL,
                PRIMARY KEY(observation_id, name)
            );
            CREATE TABLE IF NOT EXISTS scim_group_members (
                member_row_id INTEGER PRIMARY KEY,
                observation_id INTEGER NOT NULL REFERENCES scim_observations(observation_id),
                group_resource_id INTEGER NOT NULL REFERENCES scim_resources(resource_id),
                member_value TEXT,
                member_ref TEXT,
                ref_resource_type TEXT,
                ref_resource_id TEXT,
                state TEXT NOT NULL,
                pending_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS scim_access_denials (
                denial_id INTEGER PRIMARY KEY,
                tenant TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                status INTEGER NOT NULL,
                capture_id INTEGER,
                denied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_observations_resource ON scim_observations(resource_id, observation_id);
            CREATE INDEX IF NOT EXISTS idx_members_group ON scim_group_members(group_resource_id, state);
            CREATE INDEX IF NOT EXISTS idx_denials_lookup ON scim_access_denials(tenant, resource_type, source_id);
            """
        )

    def close(self):
        self.connection.close()

    @contextmanager
    def transaction(self):
        with self.connection:
            yield self.connection

    # ------------------------------------------------------------------ 写入

    def _resource_pk(self, connection, tenant, resource_type, source_id):
        row = connection.execute(
            "SELECT resource_id FROM scim_resources WHERE tenant=? AND resource_type=? AND source_id=?",
            (tenant, resource_type, source_id),
        ).fetchone()
        if row is not None:
            return row["resource_id"]
        cursor = connection.execute(
            "INSERT INTO scim_resources(tenant,resource_type,source_id) VALUES (?,?,?)",
            (tenant, resource_type, source_id),
        )
        return cursor.lastrowid

    def _known_user(self, connection, tenant, source_id):
        return connection.execute(
            "SELECT 1 FROM scim_resources WHERE tenant=? AND resource_type='Users' AND source_id=?",
            (tenant, source_id),
        ).fetchone() is not None

    def _member_state(self, connection, tenant, member):
        """判定一条组成员引用的状态；名字字段不参与身份解析。"""
        value = member.get("value")
        value = str(value) if value is not None else None
        ref = member.get("$ref")
        parsed = ref_identity(ref) if ref is not None else None
        ref_type = parsed[0] if parsed else None
        ref_id = parsed[1] if parsed else None
        if value is not None and ref_id is not None and value != ref_id:
            # $ref 与 value 矛盾：来源自身不一致，待核，禁止凭名字补齐。
            return MEMBER_PENDING, PENDING_REFERENCE_CONFLICT, ref_type, ref_id
        candidate = value if value is not None else ref_id
        candidate_type = ref_type or "Users"
        if candidate is None:
            return MEMBER_PENDING, PENDING_DETAIL_MISSING, ref_type, ref_id
        if candidate_type == "Users" and not self._known_user(connection, tenant, candidate):
            # 引用先于成员详情抵达：详情未读取，关系待补。
            return MEMBER_PENDING, PENDING_DETAIL_MISSING, ref_type, ref_id
        return MEMBER_CONFIRMED, None, ref_type, ref_id

    def _resolve_pending_members(self, connection, tenant, source_id):
        """成员详情抵达后，重新评估此前因缺少详情而待补的同租户引用。"""
        rows = connection.execute(
            """
            SELECT m.member_row_id, m.member_value, m.ref_resource_type, m.ref_resource_id
            FROM scim_group_members m
            JOIN scim_resources g ON g.resource_id = m.group_resource_id
            WHERE g.tenant = ? AND m.state = ? AND m.pending_reason = ?
            """,
            (tenant, MEMBER_PENDING, PENDING_DETAIL_MISSING),
        ).fetchall()
        for row in rows:
            candidate = row["member_value"] or row["ref_resource_id"]
            candidate_type = row["ref_resource_type"] or "Users"
            if candidate is not None and (
                candidate_type != "Users" or self._known_user(connection, tenant, candidate)
            ):
                connection.execute(
                    "UPDATE scim_group_members SET state=?, pending_reason=NULL WHERE member_row_id=?",
                    (MEMBER_CONFIRMED, row["member_row_id"]),
                )

    def ingest_observation(self, envelope, capture_id=None):
        """登记一次 SCIM 观察（采集信封），返回 observation_id。

        必需外层字段：tenant、resource_type、id、requested_attributes、resource。
        resource 必须是 SCIM 资源对象且其 ``id`` 与外层身份一致；
        source_observation（信封 observation 序号）可选，仅作来源顺序参考。
        """
        if not isinstance(envelope, dict):
            raise ValueError("envelope must be an object")
        missing = [name for name in ("tenant", "resource_type", "id", "requested_attributes", "resource") if name not in envelope]
        if missing:
            raise ValueError("missing fields: " + ", ".join(missing))
        tenant = _normalise_identity(envelope["tenant"], "tenant")
        resource_type = _normalise_identity(envelope["resource_type"], "resource_type")
        source_id = _normalise_identity(envelope["id"], "id")
        requested = _normalise_requested(envelope["requested_attributes"])
        resource = envelope["resource"]
        if not isinstance(resource, dict):
            raise ValueError("resource must be a SCIM resource object")
        resource_inner_id = resource.get("id")
        if resource_inner_id is not None and str(resource_inner_id) != source_id:
            raise ValueError("envelope id does not match resource.id")
        source_observation = envelope.get("observation")
        if source_observation is not None:
            if isinstance(source_observation, bool) or not isinstance(source_observation, int) or source_observation < 0:
                raise ValueError("observation must be a non-negative integer")

        version = _resource_version(resource)
        has_version = version is not UNVERSIONED
        returned = [name for name in resource.keys() if not name.startswith("_")]

        with self.transaction() as connection:
            resource_pk = self._resource_pk(connection, tenant, resource_type, source_id)
            cursor = connection.execute(
                """
                INSERT INTO scim_observations
                    (resource_id, source_observation, requested_attributes, returned_attributes,
                     version, has_version, capture_id)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    resource_pk,
                    source_observation,
                    json.dumps(requested, ensure_ascii=False),
                    json.dumps(returned, ensure_ascii=False),
                    version if has_version else None,
                    1 if has_version else 0,
                    capture_id,
                ),
            )
            observation_id = cursor.lastrowid
            for name in returned:
                connection.execute(
                    "INSERT INTO scim_attributes(observation_id,name,value_json) VALUES (?,?,?)",
                    (observation_id, name, json.dumps(resource[name], ensure_ascii=False, sort_keys=True)),
                )
            if resource_type == "Groups" and "members" in resource:
                members = resource["members"]
                if not isinstance(members, list):
                    raise ValueError("SCIM members must be a list")
                for member in members:
                    if not isinstance(member, dict):
                        raise ValueError("each member must be an object")
                    state, reason, ref_type, ref_id = self._member_state(connection, tenant, member)
                    connection.execute(
                        """
                        INSERT INTO scim_group_members
                            (observation_id, group_resource_id, member_value, member_ref,
                             ref_resource_type, ref_resource_id, state, pending_reason)
                        VALUES (?,?,?,?,?,?,?,?)
                        """,
                        (
                            observation_id,
                            resource_pk,
                            str(member["value"]) if member.get("value") is not None else None,
                            str(member["$ref"]) if member.get("$ref") is not None else None,
                            ref_type,
                            ref_id,
                            state,
                            reason,
                        ),
                    )
            # 新抵达的用户详情可能补齐此前待补的成员关系。
            if resource_type == "Users":
                self._resolve_pending_members(connection, tenant, source_id)
        return observation_id

    def record_denial(self, tenant, resource_type, source_id, status, capture_id=None):
        """登记一次权限/访问拒绝（如 403）。拒绝不创建资源、也不构成删除。"""
        tenant = _normalise_identity(tenant, "tenant")
        resource_type = _normalise_identity(resource_type, "resource_type")
        source_id = _normalise_identity(source_id, "id")
        if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
            raise ValueError("invalid HTTP status")
        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO scim_access_denials(tenant,resource_type,source_id,status,capture_id) VALUES (?,?,?,?,?)",
                (tenant, resource_type, source_id, status, capture_id),
            )
            return cursor.lastrowid

    # ------------------------------------------------------------------ 查询

    def _require_resource(self, tenant, resource_type, source_id):
        row = self.connection.execute(
            "SELECT * FROM scim_resources WHERE tenant=? AND resource_type=? AND source_id=?",
            (tenant, resource_type, source_id),
        ).fetchone()
        if row is None:
            raise KeyError("/".join((tenant, resource_type, source_id)))
        return dict(row)

    def resources(self, tenant=None, resource_type=None):
        """列出目录已知资源身份。身份只来自来源 ID，不做任何跨租户归并。"""
        query = "SELECT tenant,resource_type,source_id FROM scim_resources"
        clauses, params = [], []
        if tenant is not None:
            clauses.append("tenant=?")
            params.append(tenant)
        if resource_type is not None:
            clauses.append("resource_type=?")
            params.append(resource_type)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY tenant, resource_type, source_id"
        return [dict(row) for row in self.connection.execute(query, params).fetchall()]

    def observations(self, tenant, resource_type, source_id):
        """返回每次观察的请求属性、返回属性与来源版本（按本地接收顺序）。"""
        resource = self._require_resource(tenant, resource_type, source_id)
        rows = self.connection.execute(
            "SELECT * FROM scim_observations WHERE resource_id=? ORDER BY observation_id",
            (resource["resource_id"],),
        ).fetchall()
        result = []
        for row in rows:
            requested = json.loads(row["requested_attributes"])
            returned = json.loads(row["returned_attributes"])
            item = {
                "observation_id": row["observation_id"],
                "source_observation": row["source_observation"],
                "requested_attributes": requested,
                "returned_attributes": returned,
                "unread_attributes": [name for name in requested if name not in returned],
                "version": row["version"] if row["has_version"] else None,
                "has_version": bool(row["has_version"]),
                "capture_id": row["capture_id"],
                "observed_at": row["observed_at"],
            }
            result.append(item)
        return result

    def confirmed_attributes(self, tenant, resource_type, source_id):
        """已确认属性：每个属性取其最近一次观察，附带确认时的来源版本。

        仅汇总实际返回过的属性；未返回过的属性不在此列，见
        :meth:`unread_attributes`。本方法不跨版本合并取值。
        """
        resource = self._require_resource(tenant, resource_type, source_id)
        rows = self.connection.execute(
            """
            SELECT a.name, a.value_json, o.observation_id, o.version, o.has_version, o.observed_at
            FROM scim_attributes a
            JOIN scim_observations o ON o.observation_id = a.observation_id
            WHERE o.resource_id=?
            ORDER BY o.observation_id
            """,
            (resource["resource_id"],),
        ).fetchall()
        latest = {}
        for row in rows:  # 按 observation_id 递增，后者覆盖同名属性的较早观察
            latest[row["name"]] = {
                "value": json.loads(row["value_json"]),
                "version": row["version"] if row["has_version"] else None,
                "has_version": bool(row["has_version"]),
                "observation_id": row["observation_id"],
                "observed_at": row["observed_at"],
            }
        return latest

    def unread_attributes(self, tenant, resource_type, source_id):
        """未读取属性：曾被请求但任何观察都未返回的属性。

        与“明确返回空集合”不同——空集合会作为已确认属性出现，而这里只列出
        从未在响应中出现的请求属性，目录不把它们解释成空值或删除。
        """
        resource = self._require_resource(tenant, resource_type, source_id)
        rows = self.connection.execute(
            "SELECT requested_attributes, returned_attributes FROM scim_observations WHERE resource_id=? ORDER BY observation_id",
            (resource["resource_id"],),
        ).fetchall()
        requested, returned = [], set()
        for row in rows:
            for name in json.loads(row["requested_attributes"]):
                if name not in requested:
                    requested.append(name)
            returned.update(json.loads(row["returned_attributes"]))
        return [name for name in requested if name not in returned]

    def facets(self, tenant, resource_type, source_id):
        """按来源版本切分的资源视图。

        同一 ``meta.version`` 的多次观察在切面内合并（后观察覆盖同名属性）；
        不同版本各自独立成切面，绝不把不同版本的属性拼成一份完整资源。
        缺少版本标识的观察各自独立成切面（``version`` 为 null）。
        切面顺序只反映本地首次接收顺序，不代表来源版本先后。
        """
        resource = self._require_resource(tenant, resource_type, source_id)
        rows = self.connection.execute(
            """
            SELECT o.observation_id, o.version, o.has_version, o.observed_at, a.name, a.value_json
            FROM scim_observations o
            LEFT JOIN scim_attributes a ON a.observation_id = o.observation_id
            WHERE o.resource_id=?
            ORDER BY o.observation_id
            """,
            (resource["resource_id"],),
        ).fetchall()
        ordered_keys, grouped = [], {}
        for row in rows:
            key = ("v", row["version"]) if row["has_version"] else ("u", row["observation_id"])
            if key not in grouped:
                grouped[key] = {
                    "version": row["version"] if row["has_version"] else None,
                    "has_version": bool(row["has_version"]),
                    "attributes": {},
                    "observation_ids": [],
                    "first_observed_at": row["observed_at"],
                }
                ordered_keys.append(key)
            facet = grouped[key]
            if row["observation_id"] not in facet["observation_ids"]:
                facet["observation_ids"].append(row["observation_id"])
            if row["name"] is not None:
                facet["attributes"][row["name"]] = json.loads(row["value_json"])
        return [grouped[key] for key in ordered_keys]

    def group_relationships(self, tenant, group_id):
        """组成员关系：以最近一次包含 members 的观察为当前关系集合。

        明确的空 members（``[]``）返回空列表且 ``members_declared_empty`` 为
        True；从未读到 members 时返回空列表且该标记为 False——两者不可混淆。
        """
        resource = self._require_resource(tenant, "Groups", group_id)
        latest = self.connection.execute(
            """
            SELECT observation_id FROM scim_observations
            WHERE resource_id=? AND observation_id IN (
                SELECT observation_id FROM scim_group_members
                UNION
                SELECT observation_id FROM scim_attributes WHERE name='members'
            )
            ORDER BY observation_id DESC LIMIT 1
            """,
            (resource["resource_id"],),
        ).fetchone()
        if latest is None:
            return {"members_declared_empty": False, "members": []}
        rows = self.connection.execute(
            "SELECT * FROM scim_group_members WHERE observation_id=? ORDER BY member_row_id",
            (latest["observation_id"],),
        ).fetchall()
        members = [
            {
                "value": row["member_value"],
                "$ref": row["member_ref"],
                "ref_resource_type": row["ref_resource_type"],
                "ref_resource_id": row["ref_resource_id"],
                "state": row["state"],
                "pending_reason": row["pending_reason"],
            }
            for row in rows
        ]
        return {"members_declared_empty": len(members) == 0, "members": members}

    def pending_relationships(self, tenant=None):
        """待补成员关系：各组*当前*（最近一次成员观察中）仍 pending 的引用。

        被更新的成员观察取代的历史行保留在库内供审计，但不再出现在当前待补
        队列——目录以最新一次成员观察解释机构的当前关系集合。
        """
        query = (
            "WITH latest_members AS ("
            "  SELECT group_resource_id, MAX(observation_id) AS observation_id"
            "  FROM scim_group_members GROUP BY group_resource_id"
            ")"
            " SELECT g.tenant, g.source_id AS group_id, m.member_value, m.member_ref, "
            " m.ref_resource_type, m.ref_resource_id, m.state, m.pending_reason, m.observation_id "
            " FROM scim_group_members m"
            " JOIN latest_members lm ON lm.group_resource_id=m.group_resource_id"
            "   AND lm.observation_id=m.observation_id"
            " JOIN scim_resources g ON g.resource_id=m.group_resource_id "
            " WHERE m.state=?"
        )
        params = [MEMBER_PENDING]
        if tenant is not None:
            query += " AND g.tenant=?"
            params.append(tenant)
        query += " ORDER BY g.tenant, g.source_id, m.member_row_id"
        return [dict(row) for row in self.connection.execute(query, params).fetchall()]

    def access_denials(self, tenant=None, resource_type=None, source_id=None):
        """访问拒绝记录。拒绝只说明读取失败，目录不会据此删除任何资源。"""
        query = "SELECT tenant,resource_type,source_id,status,capture_id,denied_at FROM scim_access_denials"
        clauses, params = [], []
        for column, value in (
            ("tenant", tenant),
            ("resource_type", resource_type),
            ("source_id", source_id),
        ):
            if value is not None:
                clauses.append(column + "=?")
                params.append(value)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY denial_id"
        return [dict(row) for row in self.connection.execute(query, params).fetchall()]

    def resource_view(self, tenant, resource_type, source_id):
        """一份资源的完整覆盖范围说明：身份、版本切面、已确认/未读/待补/拒绝。"""
        resource = self._require_resource(tenant, resource_type, source_id)
        view = {
            "tenant": tenant,
            "resource_type": resource_type,
            "id": source_id,
            "facets": self.facets(tenant, resource_type, source_id),
            "confirmed_attributes": self.confirmed_attributes(tenant, resource_type, source_id),
            "unread_attributes": self.unread_attributes(tenant, resource_type, source_id),
            "observations": self.observations(tenant, resource_type, source_id),
            "access_denials": self.access_denials(tenant, resource_type, source_id),
        }
        if resource_type == "Groups":
            relationships = self.group_relationships(tenant, source_id)
            view["members_declared_empty"] = relationships["members_declared_empty"]
            view["members"] = relationships["members"]
            view["pending_relationships"] = [
                item
                for item in self.pending_relationships(tenant)
                if item["group_id"] == source_id
            ]
        return view


def fetch_scim_resource(client, archive, directory, tenant, resource_type, source_id,
                        attributes=None, source=None):
    """按需补详情：请求单个 SCIM 资源并把结果登记进目录。

    原始响应仍由 ``archive`` 完整留存；2xx 且为 SCIM 资源对象时作为一次观察
    入库，拒绝/错误状态登记为 access denial（不是删除证明）。返回
    ``(capture_id, response, outcome)``，outcome 为 ``ingested`` 或 ``denied``。
    """
    path = "/scim/v2/" + resource_type + "/" + source_id
    if attributes:
        path += "?attributes=" + ",".join(attributes)
    response = client.fetch(path)
    capture_id = archive.append(source or tenant, response.url, response.status, response.headers, response.body)
    if 200 <= response.status < 300:
        payload = json.loads(response.body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("SCIM resource response must be an object")
        envelope = {
            "tenant": tenant,
            "resource_type": resource_type,
            "id": source_id,
            "requested_attributes": list(attributes) if attributes else [],
            "resource": payload,
        }
        directory.ingest_observation(envelope, capture_id=capture_id)
        return capture_id, response, "ingested"
    directory.record_denial(tenant, resource_type, source_id, response.status, capture_id=capture_id)
    return capture_id, response, "denied"
