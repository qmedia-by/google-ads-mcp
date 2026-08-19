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

SHOP = Client("shop.by", {"google_ads": ("1111111111",), "vk": ("42",)})
OTHER = Client("other.by", {"vk": ("77",)})
SPLIT = Client("split.by", {"google_ads": ("1111111111", "2222222222")})


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
        with self.serving(snapshot(SHOP, OTHER)):
            result = find_client("shop.by")

        self.assertTrue(result["found"])
        self.assertEqual(len(result["clients"]), 1)
        self.assertEqual(
            result["clients"][0]["accounts"]["google_ads"], ["1111111111"]
        )

    def test_matches_a_domain_by_its_name_alone(self):
        with self.serving(snapshot(SHOP, OTHER)):
            result = find_client("shop")

        self.assertTrue(result["found"])

    def test_a_miss_is_confirmed_against_a_fresh_read(self):
        # A Client added to the sheet minutes ago must not be reported as
        # missing because the snapshot predates them.
        with patch(
            "ads_mcp.registry_cache.get_snapshot",
            side_effect=[snapshot(OTHER), snapshot(OTHER, SHOP)],
        ) as get_snapshot:
            result = find_client("shop.by")

        self.assertTrue(result["found"])
        self.assertEqual(get_snapshot.call_count, 2)
        self.assertEqual(get_snapshot.call_args.kwargs, {"force_refresh": True})

    def test_a_genuine_miss_says_so_and_offers_the_known_names(self):
        with self.serving(snapshot(OTHER)):
            result = find_client("shop.by")

        self.assertFalse(result["found"])
        self.assertEqual(result["clients"], [])
        self.assertEqual(result["known_clients"], ["other.by"])
        self.assertIn("Do not guess", result["guidance"])

    def test_several_matches_are_all_returned_with_a_warning(self):
        north = Client("shop.by", {"vk": ("1",)})
        south = Client("shop.ru", {"vk": ("2",)})
        with self.serving(snapshot(north, south)):
            result = find_client("shop")

        self.assertEqual(len(result["clients"]), 2)
        self.assertIn("Ask the Manager", result["guidance"])

    def test_a_client_with_several_cabinets_is_flagged_for_a_question(self):
        # Splitting a Client across cabinets by country is normal here; adding
        # their numbers together or picking one is not.
        with self.serving(snapshot(SPLIT)):
            result = find_client("split.by")

        self.assertEqual(
            result["clients"][0]["accounts"]["google_ads"],
            ["1111111111", "2222222222"],
        )
        self.assertIn("more than one Account", result["guidance"])
        self.assertIn("google_ads", result["guidance"])

    def test_a_single_cabinet_needs_no_guidance(self):
        with self.serving(snapshot(SHOP)):
            self.assertNotIn("guidance", find_client("shop.by"))

    def test_every_answer_says_where_yandex_direct_lives(self):
        # The fact is in the docstring too, but a docstring is read before the
        # call and a weak model has forgotten it by the time the answer lands.
        # This is the field that has to survive: an agent handed a Google Ads
        # id and nothing else had no route to Direct at all.
        with self.serving(snapshot(SHOP)):
            note = find_client("shop.by")["yandex_direct"]

        self.assertIn("get_provider_context", note)
        self.assertIn("yandex", note)
        self.assertIn("shop.by", note)

    def test_the_direct_note_forbids_reading_it_as_an_absence(self):
        with self.serving(snapshot(SHOP)):
            note = find_client("shop.by")["yandex_direct"]

        self.assertIn("no evidence", note)

    def test_a_miss_still_points_at_direct(self):
        # The case this was written for: about a third of the sheet's projects
        # run Direct only and have no Registry row, so a miss is where saying
        # "this Client does not exist" does the most damage.
        with self.serving(snapshot(OTHER)):
            result = find_client("shop.by")

        self.assertFalse(result["found"])
        self.assertIn("get_provider_context", result["yandex_direct"])

    def test_a_miss_does_not_claim_the_client_is_unknown(self):
        with self.serving(snapshot(OTHER)):
            guidance = find_client("shop.by")["guidance"]

        self.assertIn("Google Ads and VK only", guidance)
        self.assertIn("Yandex Direct", guidance)
        self.assertIn("Do not guess", guidance)

    def test_problems_about_this_client_come_back_with_it(self):
        problems = (
            "row 4: no google_ads id could be read for 'shop.by'",
            "row 9: something about other.by entirely",
        )
        with self.serving(snapshot(SHOP, OTHER, problems=problems)):
            result = find_client("shop.by")

        self.assertEqual(len(result["problems"]), 1)
        self.assertIn("shop.by", result["problems"][0])

    def test_a_clean_registry_adds_no_problems_key(self):
        with self.serving(snapshot(SHOP)):
            self.assertNotIn("problems", find_client("shop.by"))

    def test_a_stale_answer_carries_a_warning(self):
        with self.serving(snapshot(SHOP, age=3 * 3600)):
            result = find_client("shop.by")

        self.assertIn("could not be re-read", result["warning"])
        self.assertIn("about 3 hours", result["warning"])

    def test_a_barely_stale_answer_still_warns(self):
        with self.serving(snapshot(SHOP, age=REFRESH_INTERVAL_SECONDS + 60)):
            self.assertIn("warning", find_client("shop.by"))

    def test_a_fresh_answer_carries_no_warning(self):
        with self.serving(snapshot(SHOP)):
            self.assertNotIn("warning", find_client("shop.by"))

    def test_an_unreadable_registry_refuses_and_forbids_guessing(self):
        with patch("ads_mcp.registry_cache.get_snapshot", return_value=None):
            with self.assertRaises(ToolError) as context:
                find_client("shop.by")

        message = str(context.exception)
        self.assertIn("ask the Manager", message)
        self.assertIn("do not fall back", message.lower())

    def test_a_server_without_a_registry_says_that_instead(self):
        with patch("ads_mcp.registry.is_configured", return_value=False):
            with self.assertRaises(ToolError) as context:
                find_client("shop.by")

        self.assertIn("no Registry configured", str(context.exception))


class TestListClients(ToolTestCase):
    def test_lists_names_and_providers_but_not_ids(self):
        # Account ids belong in find_client's answer, not in every listing:
        # they are noise until the Manager has picked a Client.
        with self.serving(snapshot(SHOP, OTHER)):
            result = list_clients()

        self.assertEqual(result["count"], 2)
        self.assertEqual(
            result["clients"][0],
            {"name": "shop.by", "providers": ["google_ads", "vk"]},
        )
        self.assertNotIn("1111111111", str(result))

    def test_the_listing_says_it_is_not_every_client(self):
        # It is offered to the Manager as a list of candidates, and Clients who
        # run only Direct are not in it.
        with self.serving(snapshot(SHOP, OTHER)):
            self.assertIn("get_provider_context", list_clients()["yandex_direct"])

    def test_reports_every_problem_in_the_registry(self):
        with self.serving(snapshot(SHOP, problems=("row 4: bad",))):
            self.assertEqual(list_clients()["problems"], ["row 4: bad"])

    def test_a_stale_listing_carries_a_warning(self):
        with self.serving(snapshot(SHOP, age=REFRESH_INTERVAL_SECONDS + 60)):
            self.assertIn("warning", list_clients())

    def test_an_unreadable_registry_refuses(self):
        with patch("ads_mcp.registry_cache.get_snapshot", return_value=None):
            with self.assertRaises(ToolError):
                list_clients()


if __name__ == "__main__":
    unittest.main()
