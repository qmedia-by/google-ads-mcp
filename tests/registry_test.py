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

"""Test cases for reading the Account Registry (fork addition)."""

import base64
import json
import time
import unittest
from unittest.mock import MagicMock, patch

from ads_mcp import registry
from ads_mcp.registry import (
    Client,
    RegistrySnapshot,
    RegistryUnavailable,
    SERVICE_ACCOUNT_KEY_ENV_VAR,
    SHEET_ID_ENV_VAR,
    is_configured,
    normalize_customer_id,
    parse,
)

HEADER = ["Клиент", "Google Ads", "Yandex Direct", "VK"]


def sheet(*rows):
    """A sheet with the usual header and the rows given."""
    return [HEADER, *rows]


class TestNormalizeCustomerId(unittest.TestCase):
    def test_strips_hyphens_and_spaces(self):
        self.assertEqual(normalize_customer_id(" 123-456-7890 "), "1234567890")

    def test_leaves_bare_digits_alone(self):
        self.assertEqual(normalize_customer_id("1234567890"), "1234567890")


class TestIsConfigured(unittest.TestCase):
    def test_neither_variable_means_no_registry(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(is_configured())

    def test_either_variable_alone_still_counts_as_configured(self):
        # A deployment that sets one and misspells the other is misconfigured
        # and has to fail loudly. Reading it as "no Registry" would hand back
        # an unrestricted allowlist instead.
        for variable in (SHEET_ID_ENV_VAR, SERVICE_ACCOUNT_KEY_ENV_VAR):
            with self.subTest(variable=variable):
                with patch.dict("os.environ", {variable: "x"}, clear=True):
                    self.assertTrue(is_configured())

    def test_blank_values_do_not_count(self):
        with patch.dict("os.environ", {SHEET_ID_ENV_VAR: "  "}, clear=True):
            self.assertFalse(is_configured())


class TestParseHeader(unittest.TestCase):
    def test_reads_a_plain_sheet(self):
        clients, problems = parse(
            sheet(["Ромашка", "123-456-7890", "romashka", "42"])
        )

        self.assertEqual(problems, [])
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0].name, "Ромашка")
        self.assertEqual(
            clients[0].accounts,
            {
                "google_ads": "1234567890",
                "yandex_direct": "romashka",
                "vk": "42",
            },
        )

    def test_finds_a_header_that_is_not_the_first_row(self):
        # Sheets people keep by hand usually open with a title or a note.
        rows = [
            ["Реестр аккаунтов — не редактировать без согласования"],
            [],
            HEADER,
            ["Ромашка", "1234567890", "", ""],
        ]
        clients, _ = parse(rows)
        self.assertEqual([c.name for c in clients], ["Ромашка"])

    def test_header_matching_ignores_case_spacing_and_punctuation(self):
        rows = [
            ["  КЛИЕНТ ", "google_ads", "Яндекс.Директ", "VK Ads"],
            ["Ромашка", "1234567890", "romashka", "42"],
        ]
        clients, _ = parse(rows)
        self.assertEqual(
            sorted(clients[0].accounts),
            ["google_ads", "vk", "yandex_direct"],
        )

    def test_a_sheet_with_no_recognisable_header_is_unavailable(self):
        # Not an empty Registry: an empty Registry is an empty allowlist, and
        # that would lock every Manager out over a renamed column.
        with self.assertRaises(RegistryUnavailable):
            parse([["однажды"], ["в", "студёную"]])

    def test_the_error_shows_what_was_actually_in_the_sheet(self):
        with self.assertRaises(RegistryUnavailable) as context:
            parse([["Заказчик", "Кабинет"]])

        self.assertIn("Заказчик", str(context.exception))

    def test_a_header_with_no_clients_under_it_is_unavailable(self):
        with self.assertRaises(RegistryUnavailable):
            parse(sheet())

    def test_an_empty_sheet_is_unavailable(self):
        with self.assertRaises(RegistryUnavailable):
            parse([])

    def test_a_repeated_provider_column_keeps_the_leftmost(self):
        rows = [
            ["Клиент", "Google Ads", "Google"],
            ["Ромашка", "1111111111", "2222222222"],
        ]
        clients, _ = parse(rows)
        self.assertEqual(clients[0].accounts["google_ads"], "1111111111")


class TestParseRows(unittest.TestCase):
    def test_tolerates_rows_the_api_truncated(self):
        # The Sheets API drops trailing empty cells rather than padding them.
        clients, problems = parse(sheet(["Ромашка", "1234567890"]))
        self.assertEqual(problems, [])
        self.assertEqual(clients[0].accounts, {"google_ads": "1234567890"})

    def test_ignores_blank_spacer_rows_without_complaining(self):
        clients, problems = parse(
            sheet(["Ромашка", "1234567890"], [], ["", "", "", ""])
        )
        self.assertEqual(problems, [])
        self.assertEqual(len(clients), 1)

    def test_reports_accounts_with_no_client_name(self):
        clients, problems = parse(
            sheet(["Ромашка", "1111111111"], ["", "2222222222"])
        )
        self.assertEqual(len(clients), 1)
        self.assertEqual(len(problems), 1)
        self.assertIn("no Client name", problems[0])

    def test_reports_a_client_with_no_accounts_at_all(self):
        clients, problems = parse(
            sheet(["Ромашка", "1111111111"], ["Пустышка", "", "", ""])
        )
        self.assertEqual([c.name for c in clients], ["Ромашка"])
        self.assertIn("no Accounts", problems[0])

    def test_a_malformed_customer_id_is_dropped_not_guessed_at(self):
        clients, problems = parse(sheet(["Ромашка", "12345", "romashka", ""]))
        self.assertNotIn("google_ads", clients[0].accounts)
        self.assertEqual(clients[0].accounts, {"yandex_direct": "romashka"})
        self.assertIn("10-digit", problems[0])

    def test_a_malformed_id_does_not_cost_the_client_its_other_accounts(self):
        clients, _ = parse(sheet(["Ромашка", "нет", "romashka", "42"]))
        self.assertEqual(sorted(clients[0].accounts), ["vk", "yandex_direct"])

    def test_reports_a_client_listed_twice(self):
        clients, problems = parse(
            sheet(["Ромашка", "1111111111"], ["ромашка ", "2222222222"])
        )
        # Both rows are kept: which one is current is not ours to decide.
        self.assertEqual(len(clients), 2)
        self.assertTrue(any("listed twice" in p for p in problems))

    def test_reports_one_account_claimed_by_two_clients(self):
        clients, problems = parse(
            sheet(["Ромашка", "1111111111"], ["Лютик", "1111111111"])
        )
        self.assertEqual(len(clients), 2)
        self.assertTrue(any("does not say which Client" in p for p in problems))

    def test_the_same_account_on_one_client_twice_is_not_a_conflict(self):
        # Two rows for one Client is a duplicate name, reported once — it is
        # not also an ownership conflict with itself.
        _, problems = parse(
            sheet(["Ромашка", "1111111111"], ["Ромашка", "1111111111"])
        )
        self.assertFalse(
            any("does not say which Client" in p for p in problems)
        )


class TestSnapshot(unittest.TestCase):
    def snapshot(self, *clients, age=0.0):
        return RegistrySnapshot(
            clients=tuple(clients),
            problems=(),
            fetched_at=time.time() - age,
        )

    def test_allowed_customer_ids_collects_google_ads_only(self):
        snapshot = self.snapshot(
            Client("Ромашка", {"google_ads": "1111111111", "vk": "42"}),
            Client("Лютик", {"yandex_direct": "lutik"}),
        )
        self.assertEqual(
            snapshot.allowed_customer_ids(), frozenset({"1111111111"})
        )

    def test_a_contested_account_stays_in_the_allowlist(self):
        # The duplicate makes the attribution ambiguous, not the account fake.
        # Refusing it would break a working Client over a spreadsheet slip.
        snapshot = self.snapshot(
            Client("Ромашка", {"google_ads": "1111111111"}),
            Client("Лютик", {"google_ads": "1111111111"}),
        )
        self.assertEqual(
            snapshot.allowed_customer_ids(), frozenset({"1111111111"})
        )

    def test_find_matches_exactly_first(self):
        snapshot = self.snapshot(
            Client("Ромашка", {"vk": "1"}),
            Client("Ромашка Плюс", {"vk": "2"}),
        )
        self.assertEqual(
            [c.name for c in snapshot.find("ромашка")], ["Ромашка"]
        )

    def test_find_falls_back_to_containment(self):
        snapshot = self.snapshot(
            Client("Ромашка Плюс", {"vk": "1"}),
            Client("Лютик", {"vk": "2"}),
        )
        self.assertEqual(
            [c.name for c in snapshot.find("ромашка")], ["Ромашка Плюс"]
        )

    def test_find_ignores_spacing_and_punctuation(self):
        snapshot = self.snapshot(Client('ООО "Ромашка"', {"vk": "1"}))
        self.assertEqual(len(snapshot.find("ооо ромашка")), 1)

    def test_find_returns_every_candidate_rather_than_the_best(self):
        snapshot = self.snapshot(
            Client("Ромашка Север", {"vk": "1"}),
            Client("Ромашка Юг", {"vk": "2"}),
        )
        self.assertEqual(len(snapshot.find("ромашка")), 2)

    def test_find_on_nothing_matches_nothing(self):
        snapshot = self.snapshot(Client("Ромашка", {"vk": "1"}))
        self.assertEqual(snapshot.find("   "), [])

    def test_age_is_never_negative(self):
        snapshot = self.snapshot(Client("Ромашка", {"vk": "1"}), age=-60)
        self.assertEqual(snapshot.age_seconds, 0.0)


class TestSnapshotSerialisation(unittest.TestCase):
    def test_round_trips(self):
        original = RegistrySnapshot(
            clients=(
                Client("Ромашка", {"google_ads": "1111111111", "vk": "42"}),
            ),
            problems=("row 4: something",),
            fetched_at=1234567.5,
        )
        restored = registry.snapshot_from_json(
            registry.snapshot_to_json(original)
        )
        self.assertEqual(restored, original)

    def test_an_unreadable_cache_entry_is_treated_as_absent(self):
        for raw in ("", "not json", '{"clients": [{"name": "x"}]}'):
            with self.subTest(raw=raw):
                with self.assertRaises(RegistryUnavailable):
                    registry.snapshot_from_json(raw)


class TestCredentials(unittest.TestCase):
    def test_a_missing_key_names_the_variable(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RegistryUnavailable) as context:
                registry._credentials()
        self.assertIn(SERVICE_ACCOUNT_KEY_ENV_VAR, str(context.exception))

    def test_a_key_that_is_not_base64_says_so(self):
        with patch.dict(
            "os.environ", {SERVICE_ACCOUNT_KEY_ENV_VAR: "{not base64}"}
        ):
            with self.assertRaises(RegistryUnavailable) as context:
                registry._credentials()
        self.assertIn("base64", str(context.exception))

    def test_base64_that_is_not_json_says_so(self):
        encoded = base64.b64encode(b"hello").decode()
        with patch.dict("os.environ", {SERVICE_ACCOUNT_KEY_ENV_VAR: encoded}):
            with self.assertRaises(RegistryUnavailable) as context:
                registry._credentials()
        self.assertIn("JSON", str(context.exception))

    def test_the_key_itself_never_reaches_the_message(self):
        # Secrets do not go into anything a Manager or a log might see.
        secret = base64.b64encode(json.dumps({"nope": True}).encode()).decode()
        with patch.dict("os.environ", {SERVICE_ACCOUNT_KEY_ENV_VAR: secret}):
            with self.assertRaises(RegistryUnavailable) as context:
                registry._credentials()
        self.assertNotIn(secret, str(context.exception))


class TestFetch(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            "os.environ",
            {SHEET_ID_ENV_VAR: "sheet-id", SERVICE_ACCOUNT_KEY_ENV_VAR: "k"},
        )
        self.env.start()
        self.addCleanup(self.env.stop)

        self.credentials = patch(
            "ads_mcp.registry._credentials", return_value=MagicMock()
        )
        self.credentials.start()
        self.addCleanup(self.credentials.stop)

        self.token = patch(
            "ads_mcp.registry._access_token", return_value="token"
        )
        self.token.start()
        self.addCleanup(self.token.stop)

    def respond(self, status_code=200, payload=None):
        response = MagicMock()
        response.status_code = status_code
        response.json.return_value = payload or {}
        return patch("httpx.get", return_value=response)

    def test_a_missing_sheet_id_names_the_variable(self):
        with patch.dict("os.environ", {SHEET_ID_ENV_VAR: ""}):
            with self.assertRaises(RegistryUnavailable) as context:
                registry.fetch()
        self.assertIn(SHEET_ID_ENV_VAR, str(context.exception))

    def test_parses_a_successful_response(self):
        rows = sheet(["Ромашка", "1234567890", "", ""])
        with self.respond(payload={"values": rows}):
            snapshot = registry.fetch()

        self.assertEqual([c.name for c in snapshot.clients], ["Ромашка"])
        self.assertAlmostEqual(snapshot.age_seconds, 0.0, places=1)

    def test_sends_a_bearer_token_and_never_the_key(self):
        rows = sheet(["Ромашка", "1234567890"])
        with self.respond(payload={"values": rows}) as mock_get:
            registry.fetch()

        headers = mock_get.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer token")

    def test_a_refusal_points_at_the_sharing_and_the_api(self):
        for status in (401, 403):
            with self.subTest(status=status):
                with self.respond(status_code=status):
                    with self.assertRaises(RegistryUnavailable) as context:
                        registry.fetch()
                message = str(context.exception)
                self.assertIn("shared", message)
                self.assertIn("Sheets API", message)

    def test_a_missing_sheet_mentions_the_range_too(self):
        # A 404 is as often a renamed tab as a wrong file id.
        with self.respond(status_code=404):
            with self.assertRaises(RegistryUnavailable) as context:
                registry.fetch()
        self.assertIn("range", str(context.exception))

    def test_any_other_error_status_is_unavailable(self):
        with self.respond(status_code=500):
            with self.assertRaises(RegistryUnavailable):
                registry.fetch()

    def test_a_transport_failure_is_unavailable(self):
        import httpx

        with patch("httpx.get", side_effect=httpx.ConnectError("boom")):
            with self.assertRaises(RegistryUnavailable):
                registry.fetch()

    def test_an_empty_sheet_is_unavailable_not_an_empty_registry(self):
        with self.respond(payload={}):
            with self.assertRaises(RegistryUnavailable):
                registry.fetch()


if __name__ == "__main__":
    unittest.main()
