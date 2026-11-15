import json
import tempfile
import threading
import unittest
from pathlib import Path

from archive_locator.archive import Archive
from archive_locator.client import Client
from archive_locator.directory import (
    AmbiguousVersion, Directory, DirectoryError, UnknownFacet,
)
from archive_locator.origin import load_scenario, serve


def record(tenant, resource_type, resource_id, observation, resource, requested=None, **extra):
    item = {
        "tenant": tenant,
        "resource_type": resource_type,
        "id": resource_id,
        "observation": observation,
        "resource": resource,
    }
    if requested is not None:
        item["requested_attributes"] = requested
    item.update(extra)
    return item


def user(resource_id="u1", user_name="name-1", version="W/\"v1\"", **extra):
    resource = {"id": resource_id, "userName": user_name}
    if version is not None:
        resource["meta"] = {"version": version}
    resource.update(extra)
    return resource


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.directory = Directory()

    def tearDown(self):
        self.directory.close()

    def test_resource_id_is_identity_and_rename_does_not_add_a_person(self):
        self.directory.ingest_record(record("a", "Users", "u1", 1, user(user_name="old-name", version="W/\"v1\""), ["id", "userName"]))
        self.directory.ingest_record(record("a", "Users", "u1", 2, user(user_name="new-name", version="W/\"v2\""), ["id", "userName"]))
        self.assertEqual(len(self.directory.identities()), 1)
        self.assertEqual(self.directory.identities()[0]["id"], "u1")
        self.assertNotEqual(
            self.directory.confirmed_attributes("a", "Users", "u1", version="W/\"v1\"")["userName"],
            self.directory.confirmed_attributes("a", "Users", "u1", version="W/\"v2\"")["userName"],
        )

    def test_same_user_name_and_external_id_stay_separate_across_tenants(self):
        for tenant in ("a", "b"):
            self.directory.ingest_record(record(
                tenant, "Users", "u1", 1, user(external_id="ext-1"), ["id", "userName", "externalId"],
            ))
        identities = self.directory.identities()
        self.assertEqual([item["tenant"] for item in identities], ["a", "b"])
        self.assertEqual(len(identities), 2)

    def test_same_name_with_different_ids_is_not_merged(self):
        self.directory.ingest_record(record("a", "Users", "u1", 1, user("u1", "shared"), ["id", "userName"]))
        self.directory.ingest_record(record("a", "Users", "u2", 1, user("u2", "shared"), ["id", "userName"]))
        self.assertEqual({item["id"] for item in self.directory.identities()}, {"u1", "u2"})


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = Directory()

    def tearDown(self):
        self.directory.close()

    def test_records_requested_present_and_version_per_query(self):
        self.directory.ingest_record(record(
            "a", "Users", "u1", 1, user(), ["id", "userName", "active"],
        ))
        query = self.directory.queries("a", "Users", "u1")[0]
        self.assertEqual(query["requested"], ["id", "userName", "active"])
        self.assertEqual(query["present"], ["id", "userName", "meta"])
        self.assertEqual(query["version"], "W/\"v1\"")
        self.assertEqual(query["scope"], "declared")

    def test_requested_but_absent_attribute_is_unread(self):
        self.directory.ingest_record(record("a", "Users", "u1", 1, user(), ["id", "userName", "emails"]))
        unread = {item["name"]: item["reason"] for item in self.directory.unread_attributes("a", "Users", "u1")}
        self.assertEqual(unread["emails"], "requested_but_absent")
        self.assertNotIn("emails", self.directory.confirmed_attributes("a", "Users", "u1"))

    def test_missing_members_is_not_an_empty_membership(self):
        self.directory.ingest_record(record(
            "a", "Groups", "g1", 1,
            {"id": "g1", "displayName": "group", "meta": {"version": "W/\"g1\""}},
            ["id", "displayName"],
        ))
        relations = self.directory.relations("a", "g1")
        self.assertEqual(relations["membership"]["state"], "members_unread")
        self.assertFalse(relations["membership"]["read"])
        self.assertFalse(relations["membership"]["empty"])
        self.assertEqual(relations["items"], [])

    def test_explicit_empty_members_is_an_observed_empty_set(self):
        self.directory.ingest_record(record(
            "a", "Groups", "g1", 1,
            {"id": "g1", "members": [], "meta": {"version": "W/\"g1\""}},
            ["id", "members"],
        ))
        relations = self.directory.relations("a", "g1")
        self.assertEqual(relations["membership"]["state"], "confirmed")
        self.assertTrue(relations["membership"]["empty"])

    def test_unknown_projection_is_not_assumed_complete(self):
        self.directory.ingest_record(record("a", "Users", "u1", 1, user()))
        coverage = self.directory.coverage("a", "Users", "u1")
        self.assertFalse(coverage["coverage"]["complete"])
        self.assertIn("request projection was not captured for every query", coverage["coverage"]["reasons"])

    def test_scope_all_marks_absent_inventory_attributes_not_returned(self):
        self.directory.ingest_response(
            "a", "Users",
            type("R", (), {"status": 200, "url": "/scim/v2/Users/u1", "headers": {"content-type": "application/scim+json"},
                           "body": json.dumps(user()).encode()})(),
        )
        coverage = self.directory.coverage("a", "Users", "u1")
        reasons = {item["name"]: item["reason"] for item in coverage["unread_attributes"]}
        self.assertEqual(reasons["emails"], "not_returned")

    def test_attributes_and_excluded_attributes_are_mutually_exclusive(self):
        with self.assertRaisesRegex(DirectoryError, "mutually exclusive"):
            self.directory.ingest_record(record(
                "a", "Users", "u1", 1, user(), ["id"], excluded_attributes=["emails"],
            ))

    def test_excluded_attributes_from_url_are_recorded(self):
        response = type("R", (), {
            "status": 200, "url": "/scim/v2/Users/u1?excludedAttributes=emails,phoneNumbers",
            "headers": {"content-type": "application/json"}, "body": json.dumps(user()).encode(),
        })()
        self.directory.ingest_response("a", "Users", response)
        query = self.directory.queries("a", "Users", "u1")[0]
        self.assertEqual(query["scope"], "all_except")
        self.assertEqual(query["excluded"], ["emails", "phoneNumbers"])


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.directory = Directory()

    def tearDown(self):
        self.directory.close()

    def ingest_group(self):
        self.directory.ingest_record(record(
            "a", "Groups", "g1", 2,
            {"id": "g1", "displayName": "group", "meta": {"version": "W/\"zeta\""}},
            ["id", "displayName"],
        ))
        self.directory.ingest_record(record(
            "a", "Groups", "g1", 3,
            {"id": "g1", "members": [], "meta": {"version": "W/\"beta\""}},
            ["id", "members"],
        ))

    def test_multiple_versions_require_an_explicit_facet(self):
        self.ingest_group()
        with self.assertRaises(AmbiguousVersion) as caught:
            self.directory.coverage("a", "Groups", "g1")
        self.assertEqual(len(caught.exception.facets), 2)

    def test_versions_are_not_string_ordered(self):
        self.directory.ingest_record(record("a", "Users", "u1", 1, user(version="9"), ["id"]))
        self.directory.ingest_record(record("a", "Users", "u1", 2, user(version="10"), ["id"]))
        versions = self.directory.versions("a", "Users", "u1")
        self.assertEqual([item["version"] for item in versions], ["9", "10"])
        self.assertNotIn("latest", json.dumps(versions))

    def test_facets_are_not_stitched_together(self):
        self.ingest_group()
        late = self.directory.coverage("a", "Groups", "g1", version="W/\"zeta\"")
        self.assertNotIn("members", late["confirmed_attributes"])
        self.assertIn("members", [item["name"] for item in late["unread_attributes"]])
        early = self.directory.coverage("a", "Groups", "g1", version="W/\"beta\"")
        self.assertNotIn("displayName", early["confirmed_attributes"])

    def test_conflicting_values_within_one_version_are_not_picked(self):
        self.directory.ingest_record(record("a", "Users", "u1", 1, user(user_name="first"), ["id", "userName"]))
        self.directory.ingest_record(record("a", "Users", "u1", 2, user(user_name="second"), ["id", "userName"]))
        coverage = self.directory.coverage("a", "Users", "u1")
        self.assertNotIn("userName", coverage["confirmed_attributes"])
        conflict = coverage["conflicted_attributes"][0]
        self.assertEqual(conflict["name"], "userName")
        self.assertEqual(len(conflict["candidates"]), 2)

    def test_missing_meta_version_keeps_each_observation_separate(self):
        self.directory.ingest_record(record("a", "Users", "u1", 1, user(version=None), ["id"]))
        self.directory.ingest_record(record("a", "Users", "u1", 2, user(version=None, user_name="renamed"), ["id"]))
        facets = self.directory.versions("a", "Users", "u1")
        self.assertEqual(len(facets), 2)
        self.assertTrue(all(item["version_state"] == "unknown" for item in facets))
        with self.assertRaises(AmbiguousVersion):
            self.directory.coverage("a", "Users", "u1")
        selected = self.directory.coverage("a", "Users", "u1", observation=2)
        self.assertEqual(selected["confirmed_attributes"]["userName"], "renamed")

    def test_unknown_version_selector_cannot_silence_a_facet(self):
        self.directory.ingest_record(record("a", "Users", "u1", 1, user(version=None), ["id"]))
        with self.assertRaises(UnknownFacet):
            self.directory.coverage("a", "Users", "u1", version="W/\"v1\"")


class AttemptTests(unittest.TestCase):
    def setUp(self):
        self.directory = Directory()

    def tearDown(self):
        self.directory.close()

    def test_permission_denied_is_not_a_deletion_or_an_empty_resource(self):
        self.directory.ingest_record(record("a", "Users", "u9", 1, user("u9", "name-9"), ["id", "userName"]))
        payload = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
            "status": "403",
        }
        response = type("R", (), {
            "status": 403, "url": "/scim/v2/Users/u9?attributes=id,userName",
            "headers": {"content-type": "application/scim+json"},
            "body": json.dumps(payload).encode(),
        })()
        result = self.directory.ingest_response("a", "Users", response, resource_id="u9")
        self.assertTrue(result.failed)
        attempts = self.directory.attempts()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["effect"], "unread_attempt")
        self.assertEqual(attempts[0]["requested"], ["id", "userName"])
        self.assertEqual(
            self.directory.confirmed_attributes("a", "Users", "u9", version="W/\"v1\"")["userName"],
            "name-9",
        )

    def test_scim_error_with_success_status_is_still_an_attempt(self):
        payload = {"schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"], "status": "200"}
        response = type("R", (), {
            "status": 200, "url": "/scim/v2/Users/u1",
            "headers": {"content-type": "application/json"}, "body": json.dumps(payload).encode(),
        })()
        result = self.directory.ingest_response("a", "Users", response, resource_id="u1")
        self.assertTrue(result.failed)
        self.assertEqual(self.directory.identities()[0]["failed_queries"], 1)
        with self.assertRaises(UnknownFacet):
            self.directory.versions("a", "Users", "u1")

    def test_successful_record_without_resource_is_rejected(self):
        with self.assertRaisesRegex(DirectoryError, "requires a resource"):
            self.directory.ingest_record({"tenant": "a", "resource_type": "Users", "id": "u1", "observation": 1})

    def test_error_status_body_is_an_attempt_not_a_resource(self):
        record = {
            "tenant": "a", "resource_type": "Users", "id": "u1", "observation": 1,
            "status": 403,
            "resource": {"schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"], "status": "403"},
        }
        result = self.directory.ingest_record(record)
        self.assertTrue(result.failed)
        self.assertEqual(self.directory.attempts()[0]["status"], 403)
        with self.assertRaises(UnknownFacet):
            self.directory.versions("a", "Users", "u1")


class RelationTests(unittest.TestCase):
    def setUp(self):
        self.directory = Directory()

    def tearDown(self):
        self.directory.close()

    def add_group(self, members, version="W/\"g1\""):
        self.directory.ingest_record(record(
            "a", "Groups", "g1", 1,
            {"id": "g1", "members": members, "meta": {"version": version}},
            ["id", "members"],
        ))

    def test_member_reference_before_detail_is_pending_then_resolves(self):
        self.add_group([{"value": "u1"}])
        relations = self.directory.relations("a", "g1")
        self.assertEqual(relations["items"][0]["state"], "pending_target")
        self.assertEqual(relations["items"][0]["target"], {"tenant": "a", "resource_type": None, "id": "u1"})
        self.assertEqual(len(self.directory.pending_relations()), 1)
        self.directory.ingest_record(record("a", "Users", "u1", 1, user(), ["id"]))
        relations = self.directory.relations("a", "g1")
        self.assertEqual(relations["items"][0]["state"], "confirmed")
        self.assertEqual(relations["items"][0]["target"], {"tenant": "a", "resource_type": "Users", "id": "u1"})
        self.assertEqual(self.directory.pending_relations(), [])

    def test_ref_and_value_conflict_is_held_not_guessed(self):
        self.add_group([{"value": "u1", "$ref": "/scim/v2/Users/u9", "display": "name-9"}])
        self.directory.ingest_record(record("a", "Users", "u9", 1, user("u9", "name-9"), ["id", "userName"]))
        item = self.directory.relations("a", "g1")["items"][0]
        self.assertEqual(item["state"], "conflict")
        self.assertIsNone(item["target"])
        self.assertEqual(
            {candidate["id"] for candidate in item["candidates"]}, {"u1", "u9"},
        )
        self.assertEqual(
            [entry["kind"] for entry in self.directory.pending_relations()], ["member"],
        )

    def test_display_name_never_creates_identity(self):
        self.add_group([{"display": "name-1"}])
        item = self.directory.relations("a", "g1")["items"][0]
        self.assertEqual(item["state"], "unresolved")
        self.assertIsNone(item["target"])

    def test_ref_target_is_pending_until_read(self):
        self.add_group([{"$ref": "/scim/v2/Users/u1"}])
        item = self.directory.relations("a", "g1")["items"][0]
        self.assertEqual(item["state"], "pending_target")
        self.assertEqual(item["target"]["resource_type"], "Users")

    def test_members_are_not_borrowed_from_another_version(self):
        self.add_group([{"value": "u1"}], version="W/\"old\"")
        self.directory.ingest_record(record(
            "a", "Groups", "g1", 2,
            {"id": "g1", "displayName": "renamed", "meta": {"version": "W/\"new\""}},
            ["id", "displayName"],
        ))
        new = self.directory.relations("a", "g1", version="W/\"new\"")
        self.assertEqual(new["membership"]["state"], "members_unread")
        self.assertEqual(new["items"], [])


class LiveDirectoryTests(unittest.TestCase):
    def test_capture_is_archived_and_detail_is_a_separate_version_facet(self):
        scenario = load_scenario()
        with serve(scenario) as origin, tempfile.TemporaryDirectory() as temporary:
            archive = Archive(Path(temporary) / "raw.sqlite3")
            directory = Directory(Path(temporary) / "directory.sqlite3")
            try:
                directory.ingest_record(scenario["source_records"][0])
                client = Client(origin.base_url)
                result = directory.fetch_detail(
                    client, "archive-a", "/scim/v2/Users/u1",
                    resource_type="Users", attributes=["id", "userName"],
                    archive=archive, source="local-source",
                )
                self.assertFalse(result.failed)
                self.assertIsNotNone(result.capture_id)
                self.assertEqual(archive.body(result.capture_id), client.fetch("/scim/v2/Users/u1").body)
                versions = directory.versions("archive-a", "Users", "u1")
                self.assertEqual([item["version"] for item in versions], ["W/\"alpha\"", "W/\"omega\""])
                omega = directory.coverage("archive-a", "Users", "u1", version="W/\"omega\"")
                self.assertEqual(omega["confirmed_attributes"]["userName"], "new-name")
                alpha = directory.coverage("archive-a", "Users", "u1", version="W/\"alpha\"")
                self.assertEqual(alpha["confirmed_attributes"]["userName"], "old-name")
            finally:
                directory.close()
                archive.close()

    def test_denied_detail_is_an_attempt_and_does_not_delete(self):
        scenario = load_scenario()
        with serve(scenario) as origin, tempfile.TemporaryDirectory() as temporary:
            archive = Archive(Path(temporary) / "raw.sqlite3")
            directory = Directory()
            try:
                directory.ingest_record(scenario["source_records"][2])
                result = directory.fetch_detail(
                    Client(origin.base_url), "archive-a", "/scim/v2/Users/u9",
                    resource_type="Users", attributes=["id", "userName"], archive=archive,
                )
                self.assertTrue(result.failed)
                self.assertEqual(result.status, 403)
                self.assertEqual(directory.attempts()[0]["status"], 403)
                self.assertEqual(directory.versions("archive-a", "Groups", "g1")[0]["version"], "W/\"beta\"")
                self.assertEqual(directory.relations("archive-a", "g1")["items"][0]["state"], "conflict")
            finally:
                directory.close()
                archive.close()


class CaptureIngestTests(unittest.TestCase):
    def test_archived_feed_can_be_ingested_without_merging_tenants(self):
        scenario = load_scenario()
        with serve(scenario) as origin, tempfile.TemporaryDirectory() as temporary:
            archive = Archive(Path(temporary) / "raw.sqlite3")
            directory = Directory()
            try:
                capture_id, _ = __import__("archive_locator.client", fromlist=["collect"]).collect(
                    Client(origin.base_url), archive, "fixture",
                )
                results = directory.ingest_capture(archive, capture_id)
                self.assertEqual(len(results), 4)
                self.assertEqual(directory.ingest_capture(archive, capture_id), [])
                self.assertEqual(len(directory.identities()), 3)
                tenants = {item["tenant"] for item in directory.identities()}
                self.assertEqual(tenants, {"archive-a", "archive-b"})
            finally:
                directory.close()
                archive.close()


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_revisit_and_detail_do_not_mix_versions(self):
        directory = Directory()
        try:
            directory.ingest_record(record(
                "a", "Users", "u1", 1,
                {"id": "u1", "userName": "list-name", "meta": {"version": "W/\"v1\""}},
                ["id", "userName"],
            ))

            def revisit():
                directory.ingest_record(record(
                    "a", "Users", "u1", 2,
                    {"id": "u1", "displayName": "list-two", "meta": {"version": "W/\"v1\""}},
                    ["id", "displayName"],
                ))

            def detail():
                directory.ingest_record(record(
                    "a", "Users", "u1", 3,
                    {"id": "u1", "userName": "detail-name", "emails": ["x@example.invalid"],
                     "meta": {"version": "W/\"v2\""}},
                    ["id", "userName", "emails"],
                ))

            threads = [
                threading.Thread(target=revisit),
                threading.Thread(target=detail),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            versions = directory.versions("a", "Users", "u1")
            self.assertEqual([item["version"] for item in versions], ["W/\"v1\"", "W/\"v2\""])
            first = directory.coverage("a", "Users", "u1", version="W/\"v1\"")
            second = directory.coverage("a", "Users", "u1", version="W/\"v2\"")
            self.assertEqual(first["confirmed_attributes"]["displayName"], "list-two")
            self.assertNotIn("emails", first["confirmed_attributes"])
            self.assertEqual(second["confirmed_attributes"]["userName"], "detail-name")
            self.assertNotIn("displayName", second["confirmed_attributes"])
        finally:
            directory.close()


class BusinessCaseTests(unittest.TestCase):
    """fixtures/business_cases.json 中的三项业务关注点现在可被解释。"""

    def test_partial_projection_distinguishes_unread_from_empty(self):
        directory = Directory()
        try:
            directory.ingest_record(load_scenario()["source_records"][1])
            unread = directory.relations("archive-a", "g1", version='W/"zeta"')
            self.assertEqual(unread["membership"]["state"], "members_unread")
            self.assertFalse(unread["membership"]["empty"])
            directory.ingest_record(record(
                "archive-a", "Groups", "g2", 9,
                {"id": "g2", "members": [], "meta": {"version": "W/\"g2\""}},
                ["id", "members"],
            ))
            empty = directory.relations("archive-a", "g2")
            self.assertEqual(empty["membership"]["state"], "confirmed")
            self.assertTrue(empty["membership"]["empty"])
        finally:
            directory.close()

    def test_version_facets_prevent_a_fake_complete_resource(self):
        directory = Directory()
        try:
            for item in load_scenario()["source_records"][:3]:
                directory.ingest_record(item)
            with self.assertRaises(AmbiguousVersion):
                directory.relations("archive-a", "g1")
            zeta = directory.coverage("archive-a", "Groups", "g1", version='W/"zeta"')
            self.assertNotIn("members", zeta["confirmed_attributes"])
            beta = directory.coverage("archive-a", "Groups", "g1", version='W/"beta"')
            self.assertNotIn("displayName", beta["confirmed_attributes"])
        finally:
            directory.close()

    def test_reference_conflict_and_403_hold_for_review(self):
        directory = Directory()
        try:
            directory.ingest_record(load_scenario()["source_records"][2])
            item = directory.relations("archive-a", "g1", version='W/"beta"')["items"][0]
            self.assertEqual(item["state"], "conflict")
            self.assertIsNone(item["target"])
            self.assertEqual(directory.pending_relations()[0]["member"]["state"], "conflict")
        finally:
            directory.close()

    def test_business_case_file_lists_the_expected_observables(self):
        document = json.loads(
            (Path(__file__).parents[1] / "fixtures" / "business_cases.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [item["name"] for item in document],
            ["partial_projection", "version_facets", "reference_conflict"],
        )


class ValidationTests(unittest.TestCase):
    def test_invalid_envelope_is_rejected(self):
        cases = [
            {"tenant": "", "resource_type": "Users", "id": "u1", "observation": 1, "resource": user()},
            {"tenant": "a", "resource_type": "Users", "id": "u1", "observation": True, "resource": user()},
            {"tenant": "a", "resource_type": "Users", "id": "", "observation": 1, "resource": user()},
        ]
        for item in cases:
            with self.subTest(item=item), self.assertRaises(DirectoryError):
                Directory().ingest_record(item)

    def test_envelope_id_must_match_resource_id(self):
        with self.assertRaisesRegex(DirectoryError, "does not match"):
            Directory().ingest_record(record("a", "Users", "u1", 1, user("u2")))

    def test_members_that_are_not_an_array_are_invalid_not_empty(self):
        directory = Directory()
        try:
            directory.ingest_record(record(
                "a", "Groups", "g1", 1,
                {"id": "g1", "members": "u1", "meta": {"version": "W/\"g1\""}}, ["id", "members"],
            ))
            relations = directory.relations("a", "g1")
            self.assertEqual(relations["membership"]["state"], "members_invalid")
            self.assertFalse(relations["membership"]["empty"])
        finally:
            directory.close()


if __name__ == "__main__":
    unittest.main()
