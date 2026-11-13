import hashlib
import json
import unittest

from archive_locator.client import Client
from archive_locator.decoding import parse_page, records as decode_records
from archive_locator.origin import load_scenario, serve


class SourceContractTests(unittest.TestCase):
    def test_domain_observations_preserve_source_distinctions(self):
        scenario = load_scenario()
        records = scenario["source_records"]
        with serve(scenario) as origin:
            client = Client(origin.base_url)
            self.assertNotIn("members", records[1]["resource"])
            member = records[2]["resource"]["members"][0]
            self.assertNotEqual(member["value"], member["$ref"].rsplit("/", 1)[-1])
            self.assertEqual(records[0]["id"], records[3]["id"])
            self.assertNotEqual(records[0]["tenant"], records[3]["tenant"])
            self.assertEqual(client.fetch(member["$ref"]).status, 403)

    def test_every_record_has_the_declared_source_identity(self):
        scenario = load_scenario()
        for record in scenario["source_records"]:
            for field in scenario["identity_fields"]:
                self.assertIn(field, record)
                self.assertNotEqual(str(record[field]).strip(), "")


if __name__ == "__main__":
    unittest.main()
