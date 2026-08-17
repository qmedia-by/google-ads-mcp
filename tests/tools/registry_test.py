# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test cases for the Registry lookup tools (fork addition)."""

import time
import unittest
from unittest.mock import patch

from fastmcp.exceptions import ToolError

from ads_mcp.registry import Client, RegistrySnapshot
from ads_mcp.registry_cache import REFRESH_INTERVAL_SECONDS
from ads_mcp.tools.registry import find_client, list_clients

ROMASHKA = Client(
    "Ромашка", {"google_ads": "1111111111", "yandex_direct": "romashka"}
)
LUTIK = Client("Лютик", {"vk": "42"})


def snapshot(*clients, problems=(), age=0.0):
    return RegistrySnapshot(
        clients=tuple(clients),
        problems=tuple(problems),
        fetched_at=time.time() - age,
    )


class ToolTestCase(unittest.TestCase):
    def setUp(self):
        configured = patch("ads_mcp.registry.is_configured", return_value=True)
        configured.start()
        self.addCleanup(configured.stop)

    def serving(self, *snapshots):
        """Patches the cache to hand back these snapshots, in order."""
        return patch(
            "ads_mcp.registry_cache.get_snapshot",
            side_effect=list(snapshots) * 10,
        )


class TestFindClient(ToolTestCase):
    def test_returns_the_accounts_of_a_matching_client(self):
        with self.serving(snapshot(ROMASHKA, LUTIK)):
            result = find_client("ромашка")

        self.assertTrue(result["found"])
        self.assertEqual(len(result["clients"]), 1)
        self.assertEqual(
            result["clients"][0]["accounts"]["google_ads"], "1111111111"
        )

    def test_a_miss_is_confirmed_against_a_fresh_read(self):
        # A Client added to the sheet minutes ago must not be reported as
        # missing because the snapshot predates them.
        with patch(
            "ads_mcp.registry_cache.get_snapshot",
            side_effect=[snapshot(LUTIK), snapshot(LUTIK, ROMASHKA)],
        ) as get_snapshot:
            result = find_client("ромашка")

        self.assertTrue(result["found"])
        self.assertEqual(get_snapshot.call_count, 2)
        self.assertEqual(get_snapshot.call_args.kwargs, {"force_refresh": True})

    def test_a_genuine_miss_says_so_and_offers_the_known_names(self):
        with self.serving(snapshot(LUTIK)):
            result = find_client("Ромашка")

        self.assertFalse(result["found"])
        self.assertEqual(result["clients"], [])
        self.assertEqual(result["known_clients"], ["Лютик"])
        self.assertIn("Do not guess", result["guidance"])

    def test_several_matches_are_all_returned_with_a_warning(self):
        north = Client("Ромашка Север", {"vk": "1"})
        south = Client("Ромашка Юг", {"vk": "2"})
        with self.serving(snapshot(north, south)):
            result = find_client("ромашка")

        self.assertEqual(len(result["clients"]), 2)
        self.assertIn("Ask the Manager", result["guidance"])

    def test_problems_about_this_client_come_back_with_it(self):
        problems = (
            "row 4: google_ads 'nope' for 'Ромашка' is not a 10-digit id",
            "row 9: something about Лютик entirely",
        )
        with self.serving(snapshot(ROMASHKA, LUTIK, problems=problems)):
            result = find_client("ромашка")

        self.assertEqual(len(result["problems"]), 1)
        self.assertIn("Ромашка", result["problems"][0])

    def test_a_clean_registry_adds_no_problems_key(self):
        with self.serving(snapshot(ROMASHKA)):
            result = find_client("ромашка")

        self.assertNotIn("problems", result)

    def test_a_stale_answer_carries_a_warning(self):
        with self.serving(snapshot(ROMASHKA, age=3 * 3600)):
            result = find_client("ромашка")

        self.assertIn("could not be re-read", result["warning"])
        self.assertIn("about 3 hours", result["warning"])

    def test_a_barely_stale_answer_still_warns(self):
        with self.serving(
            snapshot(ROMASHKA, age=REFRESH_INTERVAL_SECONDS + 60)
        ):
            self.assertIn("warning", find_client("ромашка"))

    def test_a_fresh_answer_carries_no_warning(self):
        with self.serving(snapshot(ROMASHKA)):
            result = find_client("ромашка")

        self.assertNotIn("warning", result)

    def test_an_unreadable_registry_refuses_and_forbids_guessing(self):
        with patch("ads_mcp.registry_cache.get_snapshot", return_value=None):
            with self.assertRaises(ToolError) as context:
                find_client("ромашка")

        message = str(context.exception)
        self.assertIn("ask the Manager", message)
        self.assertIn("do not fall back", message.lower())

    def test_a_server_without_a_registry_says_that_instead(self):
        with patch("ads_mcp.registry.is_configured", return_value=False):
            with self.assertRaises(ToolError) as context:
                find_client("ромашка")

        self.assertIn("no Registry configured", str(context.exception))


class TestListClients(ToolTestCase):
    def test_lists_names_and_providers_but_not_ids(self):
        # Account ids belong in find_client's answer, not in every listing:
        # they are noise until the Manager has picked a Client.
        with self.serving(snapshot(ROMASHKA, LUTIK)):
            result = list_clients()

        self.assertEqual(result["count"], 2)
        self.assertEqual(
            result["clients"][0],
            {"name": "Ромашка", "providers": ["google_ads", "yandex_direct"]},
        )
        self.assertNotIn("1111111111", str(result))

    def test_reports_every_problem_in_the_registry(self):
        with self.serving(snapshot(ROMASHKA, problems=("row 4: bad",))):
            result = list_clients()

        self.assertEqual(result["problems"], ["row 4: bad"])

    def test_a_stale_listing_carries_a_warning(self):
        with self.serving(
            snapshot(ROMASHKA, age=REFRESH_INTERVAL_SECONDS + 60)
        ):
            self.assertIn("warning", list_clients())

    def test_an_unreadable_registry_refuses(self):
        with patch("ads_mcp.registry_cache.get_snapshot", return_value=None):
            with self.assertRaises(ToolError):
                list_clients()


if __name__ == "__main__":
    unittest.main()
