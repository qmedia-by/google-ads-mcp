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

"""Test cases for the account allowlist (fork addition).

The allowlist is the Registry, so these tests stand a snapshot up in place of
the Sheet rather than set an environment variable.
"""

import contextlib
import time
import unittest
from unittest.mock import MagicMock, patch

from fastmcp.exceptions import ToolError

from ads_mcp.access_control import (
    ensure_customer_id_allowed,
    filter_allowed_customer_ids,
    get_allowed_customer_ids,
)
from ads_mcp.registry import Client, RegistrySnapshot


def snapshot_of(*customer_ids: str, age: float = 0.0) -> RegistrySnapshot:
    """A Registry holding one Client per id given."""
    return RegistrySnapshot(
        clients=tuple(
            Client(name=f"client-{number}.by", accounts={"google_ads": (cid,)})
            for number, cid in enumerate(customer_ids, start=1)
        ),
        problems=(),
        fetched_at=time.time() - age,
    )


@contextlib.contextmanager
def with_registry(snapshot: RegistrySnapshot):
    """Runs the block with a configured, readable Registry."""
    with (
        patch("ads_mcp.registry.is_configured", return_value=True),
        patch("ads_mcp.registry_cache.get_snapshot", return_value=snapshot),
    ):
        yield


@contextlib.contextmanager
def without_registry():
    """Runs the block with no Registry configured at all."""
    with patch("ads_mcp.registry.is_configured", return_value=False):
        yield


@contextlib.contextmanager
def with_unreadable_registry():
    """Runs the block with a Registry configured but nothing to read."""
    with (
        patch("ads_mcp.registry.is_configured", return_value=True),
        patch("ads_mcp.registry_cache.get_snapshot", return_value=None),
    ):
        yield


class TestGetAllowedCustomerIds(unittest.TestCase):
    def test_no_registry_means_unrestricted(self):
        # Upstream ships no allowlist, and the same image has to keep working
        # for anyone running it without a service account.
        with without_registry():
            self.assertIsNone(get_allowed_customer_ids())

    def test_reads_the_ids_out_of_the_registry(self):
        with with_registry(snapshot_of("1234567890", "2222222222")):
            self.assertEqual(
                get_allowed_customer_ids(),
                frozenset({"1234567890", "2222222222"}),
            )

    def test_ignores_clients_without_a_google_ads_account(self):
        snapshot = RegistrySnapshot(
            clients=(
                Client(name="ads.by", accounts={"google_ads": ("1234567890",)}),
                Client(name="vk-only.by", accounts={"vk": ("42",)}),
            ),
            problems=(),
            fetched_at=time.time(),
        )
        with with_registry(snapshot):
            self.assertEqual(
                get_allowed_customer_ids(), frozenset({"1234567890"})
            )

    def test_collects_every_cabinet_of_a_client_with_several(self):
        snapshot = RegistrySnapshot(
            clients=(
                Client(
                    name="split.by",
                    accounts={"google_ads": ("1111111111", "2222222222")},
                ),
            ),
            problems=(),
            fetched_at=time.time(),
        )
        with with_registry(snapshot):
            self.assertEqual(
                get_allowed_customer_ids(),
                frozenset({"1111111111", "2222222222"}),
            )

    def test_unreadable_registry_refuses_rather_than_permits(self):
        # The whole point of the layer: an allowlist that cannot be read is
        # unknown, and an unknown allowlist is not an empty one.
        with with_unreadable_registry():
            with self.assertRaises(ToolError):
                get_allowed_customer_ids()

    def test_registry_without_any_google_ads_accounts_allows_nothing(self):
        snapshot = RegistrySnapshot(
            clients=(Client(name="vk-only.by", accounts={"vk": ("42",)}),),
            problems=(),
            fetched_at=time.time(),
        )
        with with_registry(snapshot):
            self.assertEqual(get_allowed_customer_ids(), frozenset())


class TestEnsureCustomerIdAllowed(unittest.TestCase):
    def test_allows_anything_without_a_registry(self):
        with without_registry():
            ensure_customer_id_allowed("9999999999")

    def test_allows_a_listed_account(self):
        with with_registry(snapshot_of("1234567890", "2222222222")):
            ensure_customer_id_allowed("2222222222")

    def test_allows_a_listed_account_written_with_hyphens(self):
        # Managers copy ids out of the Google Ads UI, which hyphenates them.
        with with_registry(snapshot_of("1234567890")):
            ensure_customer_id_allowed("123-456-7890")

    def test_refuses_an_unlisted_account(self):
        with with_registry(snapshot_of("1234567890")):
            with self.assertRaises(ToolError):
                ensure_customer_id_allowed("9999999999")

    def test_refusal_names_the_account_and_the_alternatives(self):
        # The Manager reads this through the agent, so it has to say which
        # account was refused and what may be used instead.
        with with_registry(snapshot_of("1234567890", "2222222222")):
            with self.assertRaises(ToolError) as context:
                ensure_customer_id_allowed("9999999999")

        message = str(context.exception)
        self.assertIn("9999999999", message)
        self.assertIn("1234567890", message)
        self.assertIn("2222222222", message)
        self.assertIn("Registry", message)

    def test_refusal_when_the_registry_has_no_accounts_points_at_the_server(
        self,
    ):
        snapshot = RegistrySnapshot(
            clients=(Client(name="vk-only.by", accounts={"vk": ("42",)}),),
            problems=(),
            fetched_at=time.time(),
        )
        with with_registry(snapshot):
            with self.assertRaises(ToolError) as context:
                ensure_customer_id_allowed("1234567890")

        message = str(context.exception)
        self.assertIn("1234567890", message)
        self.assertIn("administers", message)

    def test_unreadable_registry_refuses_and_says_who_can_fix_it(self):
        with with_unreadable_registry():
            with self.assertRaises(ToolError) as context:
                ensure_customer_id_allowed("1234567890")

        self.assertIn("administers", str(context.exception))


class TestFilterAllowedCustomerIds(unittest.TestCase):
    def test_passes_everything_through_without_a_registry(self):
        with without_registry():
            self.assertEqual(
                filter_allowed_customer_ids(["1111111111", "2222222222"]),
                ["1111111111", "2222222222"],
            )

    def test_keeps_only_listed_accounts_in_order(self):
        with with_registry(snapshot_of("3333333333", "1111111111")):
            self.assertEqual(
                filter_allowed_customer_ids(
                    ["1111111111", "2222222222", "3333333333"]
                ),
                ["1111111111", "3333333333"],
            )

    def test_accepts_a_generator(self):
        # core.list_accessible_customers passes one rather than build a list
        # it would immediately throw away.
        with with_registry(snapshot_of("1111111111")):
            self.assertEqual(
                filter_allowed_customer_ids(
                    cid for cid in ["1111111111", "2222222222"]
                ),
                ["1111111111"],
            )

    def test_no_overlap_yields_an_empty_list(self):
        with with_registry(snapshot_of("4444444444")):
            self.assertEqual(
                filter_allowed_customer_ids(["1111111111", "2222222222"]), []
            )

    def test_unreadable_registry_refuses_rather_than_hiding_everything(self):
        # Returning [] here would read to the Manager as "you have no
        # accounts", which is a different and much more alarming statement
        # than "the server cannot reach its Registry right now".
        with with_unreadable_registry():
            with self.assertRaises(ToolError):
                filter_allowed_customer_ids(["1111111111"])


class TestToolsHonourTheAllowlist(unittest.TestCase):
    """Checks the allowlist is actually wired into every tool that needs it."""

    @patch("ads_mcp.utils.get_googleads_service")
    def test_search_refuses_before_calling_the_api(self, mock_get_service):
        from ads_mcp.tools import search

        with with_registry(snapshot_of("1234567890")):
            with self.assertRaises(ToolError) as context:
                search.search(
                    customer_id="9999999999",
                    fields=["campaign.id"],
                    resource="campaign",
                )

        self.assertIn("9999999999", str(context.exception))
        mock_get_service.assert_not_called()

    @patch("ads_mcp.utils.get_googleads_service")
    def test_search_proceeds_for_a_listed_account(self, mock_get_service):
        from ads_mcp.tools import search

        mock_service = MagicMock()
        mock_get_service.return_value = mock_service
        mock_service.search_stream.return_value = []

        with with_registry(snapshot_of("1234567890")):
            results = search.search(
                customer_id="1234567890",
                fields=["campaign.id"],
                resource="campaign",
            )

        self.assertEqual(results, [])
        mock_service.search_stream.assert_called_once()

    @patch("ads_mcp.utils.get_googleads_service")
    def test_generate_keyword_ideas_refuses_before_calling_the_api(
        self, mock_get_service
    ):
        from ads_mcp.tools import keyword_planning

        with with_registry(snapshot_of("1234567890")):
            with self.assertRaises(ToolError) as context:
                keyword_planning.generate_keyword_ideas(
                    customer_id="9999999999",
                    language="1031",
                    geo_target_constants=["2112"],
                    keywords=["окна пвх"],
                )

        self.assertIn("9999999999", str(context.exception))
        mock_get_service.assert_not_called()

    @patch("ads_mcp.utils.get_googleads_service")
    def test_generate_keyword_ideas_refuses_before_validating_input(
        self, mock_get_service
    ):
        # A refused account is refused whatever else is wrong with the call:
        # complaining about the missing seed first would send the caller off
        # fixing the wrong thing.
        from ads_mcp.tools import keyword_planning

        with with_registry(snapshot_of("1234567890")):
            with self.assertRaises(ToolError) as context:
                keyword_planning.generate_keyword_ideas(
                    customer_id="9999999999",
                    language="1031",
                    geo_target_constants=["2112"],
                    keywords=[],
                    page_url="",
                )

        self.assertIn("9999999999", str(context.exception))
        mock_get_service.assert_not_called()

    @patch("ads_mcp.utils.get_googleads_service")
    def test_list_accessible_customers_hides_unlisted_accounts(
        self, mock_get_service
    ):
        from ads_mcp.tools import core

        mock_service = MagicMock()
        mock_get_service.return_value = mock_service
        mock_service.list_accessible_customers.return_value.resource_names = [
            "customers/1111111111",
            "customers/2222222222",
            "customers/3333333333",
        ]

        with with_registry(snapshot_of("1111111111", "3333333333")):
            self.assertEqual(
                core.list_accessible_customers(),
                ["1111111111", "3333333333"],
            )

    @patch("ads_mcp.utils.get_googleads_service")
    def test_list_accessible_customers_unrestricted_by_default(
        self, mock_get_service
    ):
        from ads_mcp.tools import core

        mock_service = MagicMock()
        mock_get_service.return_value = mock_service
        mock_service.list_accessible_customers.return_value.resource_names = [
            "customers/1111111111",
            "customers/2222222222",
        ]

        with without_registry():
            self.assertEqual(
                core.list_accessible_customers(),
                ["1111111111", "2222222222"],
            )


if __name__ == "__main__":
    unittest.main()
