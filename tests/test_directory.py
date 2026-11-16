import json
import tempfile
import unittest
from pathlib import Path

from archive_locator.archive import Archive
from archive_locator.client import Client
from archive_locator.directory import (
    MEMBER_CONFIRMED,
    MEMBER_PENDING,
    PENDING_DETAIL_MISSING,
    PENDING_REFERENCE_CONFLICT,
    Directory,
    fetch_scim_resource,
    ref_identity,
)
from archive_locator.origin import serve


def envelope(tenant, resource_type, resource_id, requested, resource, observation=None):
    data = {
        "tenant": tenant,
        "resource_type": resource_type,
        "id": resource_id,
        "requested_attributes": requested,
        "resource": resource,
    }
    if observation is not None:
        data["observation"] = observation
    return data


class DirectoryTestBase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Directory(Path(self.temporary.name) / "scim.sqlite3")

    def tearDown(self):
        self.directory.close()
        self.temporary.cleanup()


class IdentityTests(DirectoryTestBase):
    def test_resource_id_is_identity_renaming_username_keeps_one_resource(self):
        self.directory.ingest_observation(
            envelope("archive-a", "Users", "u1", ["id", "userName"],
                     {"id": "u1", "userName": "old-name", "meta": {"version": 'W/"alpha"'}}, 1)
        )
        self.directory.ingest_observation(
            envelope("archive-a", "Users", "u1", ["id", "userName"],
                     {"id": "u1", "userName": "new-name", "meta": {"version": 'W/"omega"'}}, 2)
        )
        resources = self.directory.resources()
        self.assertEqual(resources, [{"tenant": "archive-a", "resource_type": "Users", "source_id": "u1"}])
        confirmed = self.directory.confirmed_attributes("archive-a", "Users", "u1")
        # userName 只是一个带版本的属性变化，不产生第二个人
        self.assertEqual(confirmed["userName"]["value"], "new-name")
        self.assertEqual(confirmed["userName"]["version"], 'W/"omega"')

    def test_same_id_in_two_tenants_stays_distinct(self):
        self.directory.ingest_observation(
            envelope("archive-a", "Users", "u1", ["id", "userName"], {"id": "u1", "userName": "same"}, 1)
        )
        self.directory.ingest_observation(
            envelope("archive-b", "Users", "u1", ["id", "userName"], {"id": "u1", "userName": "same"}, 1)
        )
        tenants = {row["tenant"] for row in self.directory.resources()}
        self.assertEqual(tenants, {"archive-a", "archive-b"})
        with self.assertRaises(KeyError):
            self.directory.resource_view("archive-a", "Users", "unknown")
        self.assertEqual(
            self.directory.confirmed_attributes("archive-b", "Users", "u1")["userName"]["value"], "same"
        )

    def test_duplicate_external_id_is_not_merged_across_tenants(self):
        for tenant in ("archive-a", "archive-b"):
            self.directory.ingest_observation(
                envelope(tenant, "Users", "x-" + tenant, ["id", "externalId"],
                         {"id": "x-" + tenant, "externalId": "EXT-9"}, 1)
            )
        rows = self.directory.resources(resource_type="Users")
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["source_id"] for row in rows}, {"x-archive-a", "x-archive-b"})

    def test_same_display_name_does_not_merge_people(self):
        self.directory.ingest_observation(
            envelope("t", "Users", "p1", ["id", "displayName"], {"id": "p1", "displayName": "张三"})
        )
        self.directory.ingest_observation(
            envelope("t", "Users", "p2", ["id", "displayName"], {"id": "p2", "displayName": "张三"})
        )
        self.assertEqual(len(self.directory.resources()), 2)

    def test_envelope_id_must_match_resource_id(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.directory.ingest_observation(
                envelope("t", "Users", "u1", ["id"], {"id": "u2"})
            )

    def test_invalid_envelope_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing fields"):
            self.directory.ingest_observation({"tenant": "t"})
        with self.assertRaisesRegex(ValueError, "requested_attributes"):
            self.directory.ingest_observation(
                envelope("t", "Users", "u1", "not-a-list", {"id": "u1"})
            )
        with self.assertRaises(ValueError):
            self.directory.ingest_observation(
                envelope("t", "Users", "u1", ["id"], {"id": "u1"}, observation=-1)
            )


class CoverageTests(DirectoryTestBase):
    def test_observation_keeps_requested_returned_and_version(self):
        self.directory.ingest_observation(
            envelope("archive-a", "Users", "u1", ["id", "userName", "emails"],
                     {"id": "u1", "userName": "old-name", "meta": {"version": 'W/"alpha"'}}, 1)
        )
        observations = self.directory.observations("archive-a", "Users", "u1")
        self.assertEqual(observations[0]["requested_attributes"], ["id", "userName", "emails"])
        self.assertEqual(observations[0]["returned_attributes"], ["id", "userName", "meta"])
        self.assertEqual(observations[0]["version"], 'W/"alpha"')
        self.assertTrue(observations[0]["has_version"])

    def test_unread_attribute_differs_from_explicit_empty_collection(self):
        # 列表只给姓名：members 从未返回 → 未读取，而不是空机构
        self.directory.ingest_observation(
            envelope("t", "Groups", "g1", ["id", "displayName", "members"],
                     {"id": "g1", "displayName": "资料员", "meta": {"version": 'W/"zeta"'}}, 2)
        )
        self.assertEqual(self.directory.unread_attributes("t", "Groups", "g1"), ["members"])
        observation = self.directory.observations("t", "Groups", "g1")[0]
        self.assertEqual(observation["unread_attributes"], ["members"])
        relationship = self.directory.group_relationships("t", "g1")
        self.assertFalse(relationship["members_declared_empty"])
        self.assertEqual(relationship["members"], [])
        # 随后明确观察到空 members：是确认的空集合，不再算未读取
        self.directory.ingest_observation(
            envelope("t", "Groups", "g1", ["id", "members"],
                     {"id": "g1", "members": [], "meta": {"version": 'W/"eta"'}}, 4)
        )
        self.assertEqual(self.directory.unread_attributes("t", "Groups", "g1"), [])
        relationship = self.directory.group_relationships("t", "g1")
        self.assertTrue(relationship["members_declared_empty"])
        self.assertEqual(relationship["members"], [])

    def test_missing_members_in_list_is_not_treated_as_cleared(self):
        self.directory.ingest_observation(
            envelope("t", "Groups", "g1", ["id", "displayName"],
                     {"id": "g1", "displayName": "组"})
        )
        view = self.directory.resource_view("t", "Groups", "g1")
        self.assertFalse(view["members_declared_empty"])
        self.assertEqual(view["pending_relationships"], [])

    def test_denial_is_recorded_but_never_deletes_resource(self):
        self.directory.ingest_observation(
            envelope("t", "Users", "u9", ["id"], {"id": "u9", "meta": {"version": "v1"}})
        )
        denial_id = self.directory.record_denial("t", "Users", "u9", 403)
        self.assertIsInstance(denial_id, int)
        # 资源依然存在，且拒绝可追溯
        self.assertEqual(len(self.directory.resources()), 1)
        denials = self.directory.access_denials(source_id="u9")
        self.assertEqual(denials[0]["status"], 403)
        with self.assertRaises(ValueError):
            self.directory.record_denial("t", "Users", "u9", 999)


class FacetTests(DirectoryTestBase):
    def test_attributes_from_different_versions_are_never_stitched(self):
        self.directory.ingest_observation(
            envelope("t", "Groups", "g1", ["id", "displayName"],
                     {"id": "g1", "displayName": "资料员", "meta": {"version": 'W/"zeta"'}}, 2)
        )
        self.directory.ingest_observation(
            envelope("t", "Groups", "g1", ["id", "members"],
                     {"id": "g1", "members": [], "meta": {"version": 'W/"beta"'}}, 3)
        )
        facets = self.directory.facets("t", "Groups", "g1")
        self.assertEqual(len(facets), 2)
        self.assertEqual(facets[0]["version"], 'W/"zeta"')
        self.assertEqual(set(facets[0]["attributes"]), {"id", "displayName", "meta"})
        self.assertNotIn("members", facets[0]["attributes"])
        self.assertEqual(facets[1]["version"], 'W/"beta"')
        self.assertNotIn("displayName", facets[1]["attributes"])
        self.assertEqual(facets[1]["attributes"]["members"], [])

    def test_same_version_observations_merge_inside_one_facet(self):
        self.directory.ingest_observation(
            envelope("t", "Users", "u1", ["id", "userName"],
                     {"id": "u1", "userName": "a", "meta": {"version": "v7"}}, 1)
        )
        self.directory.ingest_observation(
            envelope("t", "Users", "u1", ["id", "emails"],
                     {"id": "u1", "emails": [{"value": "a@x"}], "meta": {"version": "v7"}}, 2)
        )
        facets = self.directory.facets("t", "Users", "u1")
        self.assertEqual(len(facets), 1)
        self.assertEqual(facets[0]["attributes"]["userName"], "a")
        self.assertEqual(facets[0]["attributes"]["emails"], [{"value": "a@x"}])

    def test_version_is_opaque_and_never_string_sorted(self):
        # 词序上 "v9" > "v10"，但目录不得据此判断版本新旧
        for order, version in ((1, "v9"), (2, "v10"), (3, "v1")):
            self.directory.ingest_observation(
                envelope("t", "Users", "u1", ["id", "userName"],
                         {"id": "u1", "userName": "n" + str(order), "meta": {"version": version}}, order)
            )
        facets = self.directory.facets("t", "Users", "u1")
        self.assertEqual([f["version"] for f in facets], ["v9", "v10", "v1"])
        observations = self.directory.observations("t", "Users", "u1")
        self.assertEqual([o["source_observation"] for o in observations], [1, 2, 3])

    def test_observation_without_version_forms_its_own_facet(self):
        self.directory.ingest_observation(
            envelope("t", "Users", "u1", ["id", "userName"], {"id": "u1", "userName": "old-name"}, 1)
        )
        self.directory.ingest_observation(
            envelope("t", "Users", "u1", ["id", "userName"],
                     {"id": "u1", "userName": "new-name", "meta": {"version": "v2"}}, 2)
        )
        facets = self.directory.facets("t", "Users", "u1")
        self.assertEqual(len(facets), 2)
        self.assertIsNone(facets[0]["version"])
        self.assertFalse(facets[0]["has_version"])

    def test_interleaved_incremental_and_on_demand_visits_stay_separated(self):
        # 增量重访（版本 beta 的 userName）与按需补详情（版本 gamma 的 emails）交错
        self.directory.ingest_observation(
            envelope("t", "Users", "u1", ["id", "userName"],
                     {"id": "u1", "userName": "n1", "meta": {"version": "beta"}}, 1)
        )
        self.directory.ingest_observation(
            envelope("t", "Users", "u1", ["id", "emails"],
                     {"id": "u1", "emails": [{"value": "e@x"}], "meta": {"version": "gamma"}}, 2)
        )
        self.directory.ingest_observation(
            envelope("t", "Users", "u1", ["id", "userName"],
                     {"id": "u1", "userName": "n2", "meta": {"version": "beta"}}, 3)
        )
        facets = self.directory.facets("t", "Users", "u1")
        self.assertEqual(len(facets), 2)
        beta = next(f for f in facets if f["version"] == "beta")
        self.assertEqual(beta["attributes"]["userName"], "n2")
        self.assertNotIn("emails", beta["attributes"])
        gamma = next(f for f in facets if f["version"] == "gamma")
        self.assertNotIn("userName", gamma["attributes"])
        self.assertEqual(gamma["attributes"]["emails"], [{"value": "e@x"}])


class MemberReferenceTests(DirectoryTestBase):
    def test_reference_arriving_before_detail_is_pending_then_resolved(self):
        # 组成员先抵达，用户详情尚未读取
        self.directory.ingest_observation(
            envelope("t", "Groups", "g1", ["id", "members"],
                     {"id": "g1", "members": [{"value": "u1", "$ref": "/scim/v2/Users/u1"}],
                      "meta": {"version": "g-v1"}}, 1)
        )
        pending = self.directory.pending_relationships("t")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["pending_reason"], PENDING_DETAIL_MISSING)
        relationship = self.directory.group_relationships("t", "g1")
        self.assertEqual(relationship["members"][0]["state"], MEMBER_PENDING)
        # 成员详情抵达后自动转为已确认
        self.directory.ingest_observation(
            envelope("t", "Users", "u1", ["id", "userName"],
                     {"id": "u1", "userName": "new-name", "meta": {"version": "u-v1"}}, 2)
        )
        self.assertEqual(self.directory.pending_relationships("t"), [])
        relationship = self.directory.group_relationships("t", "g1")
        self.assertEqual(relationship["members"][0]["state"], MEMBER_CONFIRMED)

    def test_ref_value_conflict_stays_pending_and_name_does_not_fill_gap(self):
        # value=u1 但 $ref 指向 u9：矛盾，即使 u1 已存在也必须待核
        self.directory.ingest_observation(
            envelope("t", "Users", "u1", ["id", "userName"], {"id": "u1", "userName": "known"})
        )
        self.directory.ingest_observation(
            envelope("t", "Groups", "g1", ["id", "members"],
                     {"id": "g1", "members": [
                         {"value": "u1", "$ref": "/scim/v2/Users/u9", "display": "某个名字"}]}, 3)
        )
        pending = self.directory.pending_relationships("t")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["pending_reason"], PENDING_REFERENCE_CONFLICT)
        self.assertEqual(pending[0]["ref_resource_id"], "u9")
        # 之后 u9 详情抵达也不能消解矛盾——来源需要给出一致的新观察
        self.directory.ingest_observation(
            envelope("t", "Users", "u9", ["id"], {"id": "u9"})
        )
        self.assertEqual(len(self.directory.pending_relationships("t")), 1)
        self.assertEqual(
            self.directory.group_relationships("t", "g1")["members"][0]["state"], MEMBER_PENDING
        )

    def test_conflict_resolved_only_by_a_consistent_new_observation(self):
        self.directory.ingest_observation(
            envelope("t", "Groups", "g1", ["id", "members"],
                     {"id": "g1", "members": [{"value": "u1", "$ref": "/scim/v2/Users/u9"}]}, 1)
        )
        self.directory.ingest_observation(
            envelope("t", "Users", "u1", ["id"], {"id": "u1"})
        )
        self.assertEqual(len(self.directory.pending_relationships("t")), 1)
        # 来源给出一致的新成员观察后，当前关系集合以最新观察为准
        self.directory.ingest_observation(
            envelope("t", "Groups", "g1", ["id", "members"],
                     {"id": "g1", "members": [{"value": "u1", "$ref": "/scim/v2/Users/u1"}],
                      "meta": {"version": "g-v2"}}, 2)
        )
        self.assertEqual(self.directory.pending_relationships("t"), [])
        self.assertEqual(
            self.directory.group_relationships("t", "g1")["members"][0]["state"], MEMBER_CONFIRMED
        )

    def test_ref_identity_parser(self):
        self.assertEqual(ref_identity("/scim/v2/Users/u9"), ("Users", "u9"))
        self.assertEqual(ref_identity("/scim/v2/Groups/g%201"), ("Groups", "g 1"))
        self.assertIsNone(ref_identity("https://other.example/Users/u1"))
        self.assertIsNone(ref_identity("/Users/u1"))
        self.assertIsNone(ref_identity(""))

    def test_member_without_ref_uses_value_for_resolution(self):
        self.directory.ingest_observation(
            envelope("t", "Groups", "g1", ["id", "members"],
                     {"id": "g1", "members": [{"value": "u7"}]})
        )
        self.assertEqual(self.directory.pending_relationships("t")[0]["pending_reason"],
                         PENDING_DETAIL_MISSING)
        self.directory.ingest_observation(envelope("t", "Users", "u7", ["id"], {"id": "u7"}))
        self.assertEqual(self.directory.pending_relationships("t"), [])


class PersistenceTests(DirectoryTestBase):
    def test_state_persists_across_reopen(self):
        path = Path(self.temporary.name) / "reopen.sqlite3"
        directory = Directory(path)
        directory.ingest_observation(
            envelope("t", "Users", "u1", ["id", "userName"],
                     {"id": "u1", "userName": "n", "meta": {"version": "v1"}}, 1)
        )
        directory.close()
        reopened = Directory(path)
        try:
            self.assertEqual(reopened.confirmed_attributes("t", "Users", "u1")["userName"]["value"], "n")
            self.assertEqual(reopened.observations("t", "Users", "u1")[0]["version"], "v1")
        finally:
            reopened.close()


class FixtureScenarioTests(unittest.TestCase):
    """端到端：合成来源 → 原始归档 → 目录解释，对应 business_cases.json 三个用例。"""

    def test_full_interleaving_scenario_against_local_origin(self):
        from archive_locator.origin import load_scenario

        scenario = load_scenario()
        with serve(scenario) as origin, tempfile.TemporaryDirectory() as temporary:
            archive = Archive(Path(temporary) / "raw.sqlite3")
            directory = Directory(Path(temporary) / "scim.sqlite3")
            client = Client(origin.base_url)
            try:
                records = scenario["source_records"]
                # 1a) 先摄入列表投影（Users u1、Groups g1 只给 displayName、另一租户 u1）
                for record in (records[0], records[1], records[3]):
                    directory.ingest_observation(
                        {key: record[key] for key in
                         ("tenant", "resource_type", "id", "observation", "requested_attributes")}
                        | {"resource": record["resource"]}
                    )

                # partial_projection：g1 列表投影未带 members → 未读取，不是清空机构
                # （列表请求本身未点名 members，所以资源级 unread 为空；覆盖范围由
                # 关系集合的“从未读到/明确空集合”区分和观察级 unread_attributes 解释）
                list_observation = next(
                    item for item in directory.observations("archive-a", "Groups", "g1")
                    if item["source_observation"] == 2
                )
                self.assertEqual(list_observation["returned_attributes"], ["id", "displayName", "meta"])
                self.assertFalse(directory.group_relationships("archive-a", "g1")["members_declared_empty"])
                self.assertEqual(directory.group_relationships("archive-a", "g1")["members"], [])
                # 两个租户的 u1 互不归并，同名也不合并
                self.assertEqual(len(directory.resources(resource_type="Users")), 2)

                # 1b) 再摄入成员观察：value=u1 与 $ref=.../u9 矛盾 → 待核
                directory.ingest_observation(
                    {key: records[2][key] for key in
                     ("tenant", "resource_type", "id", "observation", "requested_attributes")}
                    | {"resource": records[2]["resource"]}
                )
                pending = directory.pending_relationships("archive-a")
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0]["pending_reason"], PENDING_REFERENCE_CONFLICT)
                # members 现已读取，不再属于未读；但 g1 仍有两个互不拼接的版本切面
                self.assertEqual(directory.unread_attributes("archive-a", "Groups", "g1"), [])

                # 2) 按需补详情：u1 读到新版本 omega（userName 从 old 变 new，不新建人）
                _, response, outcome = fetch_scim_resource(
                    client, archive, directory, "archive-a", "Users", "u1",
                    attributes=["id", "userName"]
                )
                self.assertEqual(outcome, "ingested")
                self.assertEqual(response.status, 200)
                confirmed = directory.confirmed_attributes("archive-a", "Users", "u1")
                self.assertEqual(confirmed["userName"]["value"], "new-name")
                self.assertEqual(confirmed["userName"]["version"], 'W/"omega"')

                # version_facets：alpha 与 omega 两个切面独立，不拼完整资源
                facets = directory.facets("archive-a", "Users", "u1")
                self.assertEqual([f["version"] for f in facets], ['W/"alpha"', 'W/"omega"'])

                # 3) 按需补 u9 详情被 403 拒绝：登记拒绝，不删除、不消解待核
                _, forbidden, denial_outcome = fetch_scim_resource(
                    client, archive, directory, "archive-a", "Users", "u9"
                )
                self.assertEqual(forbidden.status, 403)
                self.assertEqual(denial_outcome, "denied")
                denials = directory.access_denials("archive-a", "Users", "u9")
                self.assertEqual(denials[0]["status"], 403)
                self.assertEqual(len(directory.pending_relationships("archive-a")), 1)

                # 原始归档完整保留每次响应（含 403 正文），且目录观察可回溯 capture
                self.assertEqual(len(archive.captures()), 2)
                observations = directory.observations("archive-a", "Users", "u1")
                self.assertIsNotNone(observations[-1]["capture_id"])
                archived = json.loads(archive.body(observations[-1]["capture_id"]))
                self.assertEqual(archived["userName"], "new-name")

                # 资源视图能解释自身覆盖范围
                view = directory.resource_view("archive-a", "Groups", "g1")
                self.assertEqual(view["unread_attributes"], [])
                self.assertEqual(len(view["facets"]), 2)
                self.assertEqual(view["members"][0]["state"], MEMBER_PENDING)
            finally:
                directory.close()
                archive.close()


if __name__ == "__main__":
    unittest.main()
