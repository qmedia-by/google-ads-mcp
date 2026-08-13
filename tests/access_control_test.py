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

"""Test cases for the customer id allowlist (fork addition)."""

import os
import unittest
from unittest.mock import MagicMock, patch

from fastmcp.exceptions import ToolError

from ads_mcp.access_control import (
    ALLOWED_CUSTOMER_IDS_ENV_VAR,
    ensure_customer_id_allowed,
    filter_allowed_customer_ids,
    get_allowed_customer_ids,
)


def with_allowlist(value: str):
    """Sets the allowlist env var for the duration of a block."""
    return patch.dict("os.environ", {ALLOWED_CUSTOMER_IDS_ENV_VAR: value})


def without_allowlist():
    """Removes the allowlist env var for the duration of a block.

    Only that one variable — clearing the whole environment would take the
    credentials and config the rest of the server reads along with it.
    """
    environment = os.environ.copy()
    environment.pop(ALLOWED_CUSTOMER_IDS_ENV_VAR, None)
    return patch.dict("os.environ", environment, clear=True)


class TestGetAllowedCustomerIds(unittest.TestCase):
    def test_unset_means_unrestricted(self):
        # Upstream ships no allowlist, and the same image must keep working
        # for anyone who never sets the variable.
        with without_allowlist():
            self.assertIsNone(get_allowed_customer_ids())

    def test_empty_means_unrestricted(self):
        with with_allowlist("   "):
            self.assertIsNone(get_allowed_customer_ids())

    def test_parses_a_list(self):
        with with_allowlist("1234567890,2222222222"):
            self.assertEqual(
                get_allowed_customer_ids(),
                frozenset({"1234567890", "2222222222"}),
            )

    def test_tolerates_spacing_and_trailing_commas(self):
        with with_allowlist(" 1234567890 , 2222222222 , "):
            self.assertEqual(
                get_allowed_customer_ids(),
                frozenset({"1234567890", "2222222222"}),
            )

    def test_stores_hyphenated_entries_as_digits(self):
        with with_allowlist("123-456-7890"):
            self.assertEqual(
                get_allowed_customer_ids(), frozenset({"1234567890"})
            )

    def test_drops_entries_that_cannot_be_an_id(self):
        with with_allowlist("1234567890,not-an-id"):
            self.assertEqual(
                get_allowed_customer_ids(), frozenset({"1234567890"})
            )

    def test_set_but_all_entries_invalid_allows_nothing(self):
        # Fails closed on purpose: a misconfigured allowlist that silently
        # lets everything through is the exact hole this module closes.
        with with_allowlist("oops"):
            self.assertEqual(get_allowed_customer_ids(), frozenset())


class TestEnsureCustomerIdAllowed(unittest.TestCase):
    def test_allows_anything_when_unset(self):
        with without_allowlist():
            ensure_customer_id_allowed("9999999999")

    def test_allows_a_listed_account(self):
        with with_allowlist("1234567890,2222222222"):
            ensure_customer_id_allowed("2222222222")

    def test_allows_a_listed_account_written_with_hyphens(self):
        with with_allowlist("1234567890"):
            ensure_customer_id_allowed("123-456-7890")

    def test_refuses_an_unlisted_account(self):
        with with_allowlist("1234567890"):
            with self.assertRaises(ToolError):
                ensure_customer_id_allowed("9999999999")

    def test_refusal_names_the_account_and_the_alternatives(self):
        # The manager reads this through the agent, so it has to say which
        # account was refused and what may be used instead.
        with with_allowlist("1234567890,2222222222"):
            with self.assertRaises(ToolError) as context:
                ensure_customer_id_allowed("9999999999")

        message = str(context.exception)
        self.assertIn("9999999999", message)
        self.assertIn("1234567890", message)
        self.assertIn("2222222222", message)
        self.assertIn(ALLOWED_CUSTOMER_IDS_ENV_VAR, message)

    def test_refusal_when_allowlist_is_unusable_points_at_the_server(self):
        with with_allowlist("oops"):
            with self.assertRaises(ToolError) as context:
                ensure_customer_id_allowed("1234567890")

        message = str(context.exception)
        self.assertIn("1234567890", message)
        self.assertIn(ALLOWED_CUSTOMER_IDS_ENV_VAR, message)


class TestFilterAllowedCustomerIds(unittest.TestCase):
    def test_passes_everything_through_when_unset(self):
        with without_allowlist():
            self.assertEqual(
                filter_allowed_customer_ids(["1111111111", "2222222222"]),
                ["1111111111", "2222222222"],
            )

    def test_keeps_only_listed_accounts_in_order(self):
        with with_allowlist("3333333333,1111111111"):
            self.assertEqual(
                filter_allowed_customer_ids(
                    ["1111111111", "2222222222", "3333333333"]
                ),
                ["1111111111", "3333333333"],
            )

    def test_accepts_a_generator(self):
        # core.list_accessible_customers passes one rather than build a list
        # it would immediately throw away.
        with with_allowlist("1111111111"):
            self.assertEqual(
                filter_allowed_customer_ids(
                    cid for cid in ["1111111111", "2222222222"]
                ),
                ["1111111111"],
            )

    def test_no_overlap_yields_an_empty_list(self):
        with with_allowlist("4444444444"):
            self.assertEqual(
                filter_allowed_customer_ids(["1111111111", "2222222222"]), []
            )


class TestToolsHonourTheAllowlist(unittest.TestCase):
    """Checks the allowlist is actually wired into every tool that needs it."""

    @patch("ads_mcp.utils.get_googleads_service")
    def test_search_refuses_before_calling_the_api(self, mock_get_service):
        from ads_mcp.tools import search

        with with_allowlist("1234567890"):
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

        with with_allowlist("1234567890"):
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

        with with_allowlist("1234567890"):
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

        with with_allowlist("1234567890"):
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

        with with_allowlist("1111111111,3333333333"):
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

        with without_allowlist():
            self.assertEqual(
                core.list_accessible_customers(),
                ["1111111111", "2222222222"],
            )


if __name__ == "__main__":
    unittest.main()
