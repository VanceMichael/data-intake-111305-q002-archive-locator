import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from archive_locator.origin import serve


class DirectoryCommandTests(unittest.TestCase):
    def run_command(self, *arguments):
        completed = subprocess.run(
            [sys.executable, "-m", "archive_locator", *arguments],
            check=True, capture_output=True, text=True,
        )
        return json.loads(completed.stdout)

    def test_directory_cli_lifecycle(self):
        with serve() as origin, tempfile.TemporaryDirectory() as temporary:
            archive = str(Path(temporary) / "raw.sqlite3")
            directory = str(Path(temporary) / "scim.sqlite3")

            ingested = self.run_command(
                "directory", "ingest", "--base-url", origin.base_url,
                "--archive", archive, "--directory", directory,
                "--source", "local-source",
            )
            self.assertEqual(ingested["ingested_observations"], 4)
            self.assertEqual(len(ingested["known_resources"]), 3)

            # 成员引用 $ref 与 value 矛盾 → 待补
            pending = self.run_command("directory", "pending", "--directory", directory)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["pending_reason"], "ref_value_conflict")

            # 按需补 u1 详情：成功，且不新建第二个人
            fetched = self.run_command(
                "directory", "fetch", "--base-url", origin.base_url,
                "--archive", archive, "--directory", directory,
                "--tenant", "archive-a", "--resource-type", "Users", "--id", "u1",
                "--attributes", "id,userName",
            )
            self.assertEqual((fetched["status"], fetched["outcome"]), (200, "ingested"))

            # u9 详情 403：denied，不构成删除
            forbidden = self.run_command(
                "directory", "fetch", "--base-url", origin.base_url,
                "--archive", archive, "--directory", directory,
                "--tenant", "archive-a", "--resource-type", "Users", "--id", "u9",
            )
            self.assertEqual((forbidden["status"], forbidden["outcome"]), (403, "denied"))

            # 资源视图：版本切面互不拼接，拒绝可追溯
            view = self.run_command(
                "directory", "show", "--directory", directory,
                "--tenant", "archive-a", "--resource-type", "Users", "--id", "u1",
            )
            self.assertEqual([f["version"] for f in view["facets"]], ['W/"alpha"', 'W/"omega"'])
            self.assertEqual(view["confirmed_attributes"]["userName"]["value"], "new-name")
            self.assertEqual(view["access_denials"], [])

            group_view = self.run_command(
                "directory", "show", "--directory", directory,
                "--tenant", "archive-a", "--resource-type", "Groups", "--id", "g1",
            )
            self.assertEqual(group_view["members"][0]["state"], "pending")
            self.assertFalse(group_view["members_declared_empty"])

            # 两个租户的 u1 各自独立
            other_tenant = self.run_command(
                "directory", "show", "--directory", directory,
                "--tenant", "archive-b", "--resource-type", "Users", "--id", "u1",
            )
            self.assertEqual(other_tenant["confirmed_attributes"]["userName"]["value"], "old-name")


if __name__ == "__main__":
    unittest.main()
