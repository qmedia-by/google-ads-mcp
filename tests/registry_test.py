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

"""Test cases for reading the Account Registry (fork addition).

The rows here are shaped like the agency's real sheet — several blocks with
different columns, domains for names, ids written with hyphens and prose around
them — with invented values.
"""

import base64
import json
import time
import unittest
from unittest.mock import MagicMock, patch

from ads_mcp import registry
from ads_mcp.registry import (
    SERVICE_ACCOUNT_KEY_ENV_VAR,
    SHEET_ID_ENV_VAR,
    Client,
    RegistrySnapshot,
    RegistryUnavailable,
    is_configured,
    normalize_customer_id,
    parse,
)

# The PPC block: the one that carries Google Ads.
PPC_HEADER = ["Проект", "ТС PPC", "Яндекс Директ", "Google Ads", "VK реклама"]
# The targeting block: same projects, different Providers, no Google Ads.
TARGET_HEADER = ["Проект", "ТС Target", "Meta", "TikTok", "VK реклама"]
# The credentials block: named columns we never read.
ACCESS_HEADER = ["Проект", "ТС", "Аккаунт", "Доступы"]


def ppc(*rows):
    return [PPC_HEADER, *rows]


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


class TestCredentialColumnsAreNeverRead(unittest.TestCase):
    """The Sheet holds cabinet and social passwords in plain text.

    They are kept out by never naming those columns, so these are the tests
    that matter most in this file: if they fail, secrets are on their way into
    a snapshot, a Redis key and an agent's context.
    """

    def test_the_yandex_direct_column_does_not_reach_the_output(self):
        secret = "cabinet-login SuperSecret123"
        clients, problems = parse(
            ppc(["shop.by", "Иванов", secret, "123-456-7890", ""])
        )

        self.assertNotIn("yandex_direct", clients[0].accounts)
        rendered = repr(clients) + repr(problems)
        self.assertNotIn("SuperSecret123", rendered)
        self.assertNotIn("cabinet-login", rendered)

    def test_a_block_with_no_provider_columns_is_skipped_whole(self):
        rows = [
            *ppc(["shop.by", "Иванов", "", "123-456-7890", ""]),
            [],
            ACCESS_HEADER,
            ["shop.by", "Петров", "instagram.com/shop", "pass: hunter2"],
        ]
        clients, problems = parse(rows)

        self.assertEqual([c.name for c in clients], ["shop.by"])
        rendered = repr(clients) + repr(problems)
        self.assertNotIn("hunter2", rendered)
        self.assertNotIn("instagram", rendered)

    def test_the_credentials_header_does_not_become_a_client(self):
        # Without the block-boundary handling, "Проект" itself would be read
        # as a project name and its row as data.
        rows = [
            *ppc(["shop.by", "Иванов", "", "123-456-7890", ""]),
            ACCESS_HEADER,
            ["other.by", "Петров", "account", "pass: hunter2"],
        ]
        clients, _ = parse(rows)

        names = [c.name for c in clients]
        self.assertEqual(names, ["shop.by"])
        self.assertNotIn("Проект", names)


class TestBlocks(unittest.TestCase):
    def test_reads_a_single_block(self):
        clients, problems = parse(
            ppc(["shop.by", "Иванов", "", "123-456-7890", "SHOP (19142062)"])
        )

        self.assertEqual(problems, [])
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0].name, "shop.by")
        self.assertEqual(
            clients[0].accounts,
            {"google_ads": ("1234567890",), "vk": ("19142062",)},
        )

    def test_finds_a_header_that_is_not_the_first_row(self):
        rows = [
            ["Реестр аккаунтов — не редактировать без согласования"],
            [],
            *ppc(["shop.by", "Иванов", "", "123-456-7890", ""]),
        ]
        clients, _ = parse(rows)
        self.assertEqual([c.name for c in clients], ["shop.by"])

    def test_merges_one_client_split_across_blocks(self):
        # The PPC team and the targeting team keep separate blocks, and the
        # same Client appears in both with different Providers.
        rows = [
            *ppc(["shop.by", "Иванов", "", "123-456-7890", ""]),
            [],
            TARGET_HEADER,
            ["shop.by", "Петрова", "META (ID 555)", "TT", "SHOP (19142062)"],
        ]
        clients, _ = parse(rows)

        self.assertEqual(len(clients), 1)
        self.assertEqual(
            clients[0].accounts,
            {"google_ads": ("1234567890",), "vk": ("19142062",)},
        )

    def test_ignores_providers_that_are_not_ours(self):
        # Meta and TikTok are in the Sheet and not connected to the agent.
        rows = [
            TARGET_HEADER,
            ["shop.by", "Петрова", "META (ID 555)", "TT", ""],
        ]
        with self.assertRaises(RegistryUnavailable):
            parse(rows)

    def test_a_second_block_does_not_inherit_the_first_columns(self):
        rows = [
            *ppc(["shop.by", "Иванов", "", "123-456-7890", ""]),
            TARGET_HEADER,
            # Column 3 is TikTok here, not Google Ads. Reading it as an id
            # would invent an Account nobody has.
            ["other.by", "Петрова", "META (ID 5)", "999-888-7777", ""],
        ]
        clients, _ = parse(rows)

        by_name = {c.name: c.accounts for c in clients}
        self.assertNotIn("other.by", by_name)
        self.assertEqual(by_name["shop.by"]["google_ads"], ("1234567890",))

    def test_no_header_anywhere_is_unavailable(self):
        # Not an empty Registry: an empty Registry is an empty allowlist, and
        # that would lock every Manager out over a renamed column.
        with self.assertRaises(RegistryUnavailable):
            parse([["однажды"], ["в", "студёную"]])

    def test_the_error_shows_what_was_actually_in_the_sheet(self):
        with self.assertRaises(RegistryUnavailable) as context:
            parse([["Заказчик", "Кабинет"]])

        self.assertIn("Заказчик", str(context.exception))

    def test_headers_but_no_readable_client_is_unavailable(self):
        with self.assertRaises(RegistryUnavailable):
            parse(ppc())

    def test_an_empty_sheet_is_unavailable(self):
        with self.assertRaises(RegistryUnavailable):
            parse([])


class TestProjectNames(unittest.TestCase):
    def test_strips_scheme_www_and_trailing_slash(self):
        rows = ppc(
            ["https://shop.by/", "И", "", "111-111-1111", ""],
            ["www.other.by", "И", "", "222-222-2222", ""],
        )
        clients, _ = parse(rows)
        self.assertEqual([c.name for c in clients], ["shop.by", "other.by"])

    def test_the_same_domain_written_three_ways_is_one_client(self):
        rows = ppc(
            ["shop.by", "И", "", "111-111-1111", ""],
            ["https://shop.by/", "И", "", "222-222-2222", ""],
            ["www.shop.by", "И", "", "", "SHOP (19142062)"],
        )
        clients, _ = parse(rows)

        self.assertEqual(len(clients), 1)
        self.assertEqual(
            clients[0].accounts["google_ads"], ("1111111111", "2222222222")
        )
        self.assertEqual(clients[0].accounts["vk"], ("19142062",))

    def test_keeps_the_first_spelling_as_the_display_name(self):
        rows = ppc(
            ["shop.by", "И", "", "111-111-1111", ""],
            ["https://shop.by/", "И", "", "222-222-2222", ""],
        )
        clients, _ = parse(rows)
        self.assertEqual(clients[0].name, "shop.by")


class TestGoogleAdsCells(unittest.TestCase):
    def test_reads_a_hyphenated_id(self):
        clients, _ = parse(ppc(["shop.by", "И", "", "846-647-7739", ""]))
        self.assertEqual(clients[0].accounts["google_ads"], ("8466477739",))

    def test_reads_a_bare_ten_digit_id(self):
        clients, _ = parse(ppc(["shop.by", "И", "", "8466477739", ""]))
        self.assertEqual(clients[0].accounts["google_ads"], ("8466477739",))

    def test_takes_every_cabinet_in_a_cell_rather_than_the_first(self):
        # Written in the Sheet as prose: several cabinets split by country or
        # product line. Picking one would report on half a Client.
        cell = "разные кабинеты: 814-201-8417 666-118-9500"
        clients, problems = parse(ppc(["shop.by", "И", "", cell, ""]))

        self.assertEqual(
            clients[0].accounts["google_ads"], ("8142018417", "6661189500")
        )
        self.assertEqual(problems, [])

    def test_ignores_prose_around_an_id(self):
        cell = "byqmedia@gmail.com 700-817-1195"
        clients, _ = parse(ppc(["shop.by", "И", "", cell, ""]))
        self.assertEqual(clients[0].accounts["google_ads"], ("7008171195",))

    def test_does_not_bite_ten_digits_out_of_a_longer_number(self):
        # A phone number in the cell is not a cabinet.
        cell = "вход по телефону +375291990147"
        clients, problems = parse(
            ppc(
                ["other.by", "И", "", "111-111-1111", ""],
                ["shop.by", "И", "", cell, ""],
            )
        )

        self.assertEqual([c.name for c in clients], ["other.by"])
        self.assertTrue(any("no google_ads id" in p for p in problems))

    def test_a_repeated_id_in_one_cell_is_listed_once(self):
        cell = "846-647-7739 и он же 8466477739"
        clients, _ = parse(ppc(["shop.by", "И", "", cell, ""]))
        self.assertEqual(clients[0].accounts["google_ads"], ("8466477739",))

    def test_dashes_meaning_none_are_not_reported_as_broken(self):
        for marker in ("-", "–", "—", "\\-", ""):
            with self.subTest(marker=marker):
                rows = ppc(
                    ["shop.by", "И", "", marker, "SHOP (19142062)"],
                )
                clients, problems = parse(rows)
                self.assertEqual(problems, [])
                self.assertNotIn("google_ads", clients[0].accounts)

    def test_a_filled_cell_with_nothing_id_shaped_is_reported(self):
        clients, problems = parse(
            ppc(["shop.by", "И", "", "уточняется у клиента", "SHOP (1914)"])
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("no google_ads id", problems[0])
        self.assertIn("shop.by", problems[0])


class TestVkCells(unittest.TestCase):
    def test_reads_a_bracketed_id(self):
        clients, _ = parse(
            ppc(["shop.by", "И", "", "", "ICNT-ВКа-M-S-shop (19142062)"])
        )
        self.assertEqual(clients[0].accounts["vk"], ("19142062",))

    def test_reads_a_bracketed_id_written_with_the_word_id(self):
        clients, _ = parse(
            ppc(["shop.by", "И", "", "", "SHOP-NAME (ID 29165115)"])
        )
        self.assertEqual(clients[0].accounts["vk"], ("29165115",))

    def test_takes_only_the_bracketed_number(self):
        # These cells also carry logins and phone numbers. Only what is in
        # brackets is an account id, and only that leaves the parser.
        cell = "vkads_1095379146@vk@11083283 (ID 29165115) вход +375291990147"
        clients, problems = parse(ppc(["shop.by", "И", "", "", cell]))

        self.assertEqual(clients[0].accounts["vk"], ("29165115",))
        rendered = repr(clients) + repr(problems)
        self.assertNotIn("1095379146", rendered)
        self.assertNotIn("375291990147", rendered)


class TestProblems(unittest.TestCase):
    def test_reports_accounts_with_no_project_name(self):
        clients, problems = parse(
            ppc(
                ["shop.by", "И", "", "111-111-1111", ""],
                ["", "", "", "2" * 10, ""],
            )
        )
        self.assertEqual(len(clients), 1)
        self.assertEqual(len(problems), 1)
        self.assertIn("no project name", problems[0])

    def test_ignores_blank_spacer_rows_without_complaining(self):
        clients, problems = parse(
            ppc(
                ["shop.by", "И", "", "111-111-1111", ""],
                [],
                ["", "", "", "", ""],
            )
        )
        self.assertEqual(problems, [])
        self.assertEqual(len(clients), 1)

    def test_a_project_with_no_readable_account_is_left_out(self):
        clients, _ = parse(
            ppc(
                ["shop.by", "И", "", "111-111-1111", ""],
                ["empty.by", "И", "", "-", "-"],
            )
        )
        self.assertEqual([c.name for c in clients], ["shop.by"])

    def test_reports_one_account_claimed_by_two_clients(self):
        clients, problems = parse(
            ppc(
                ["shop.by", "И", "", "111-111-1111", ""],
                ["other.by", "И", "", "111-111-1111", ""],
            )
        )
        self.assertEqual(len(clients), 2)
        self.assertEqual(len(problems), 1)
        self.assertIn("does not say which Client", problems[0])
        self.assertIn("shop.by", problems[0])
        self.assertIn("other.by", problems[0])

    def test_several_cabinets_on_one_client_are_not_a_conflict(self):
        clients, problems = parse(
            ppc(
                ["shop.by", "И", "", "111-111-1111", ""],
                ["shop.by", "И", "", "222-222-2222", ""],
            )
        )
        self.assertEqual(len(clients), 1)
        self.assertEqual(problems, [])


class TestSnapshot(unittest.TestCase):
    def snapshot(self, *clients, age=0.0):
        return RegistrySnapshot(
            clients=tuple(clients),
            problems=(),
            fetched_at=time.time() - age,
        )

    def test_allowed_customer_ids_collects_every_cabinet(self):
        snapshot = self.snapshot(
            Client("shop.by", {"google_ads": ("1111111111", "3333333333")}),
            Client("other.by", {"vk": ("42",)}),
        )
        self.assertEqual(
            snapshot.allowed_customer_ids(),
            frozenset({"1111111111", "3333333333"}),
        )

    def test_a_contested_account_stays_in_the_allowlist(self):
        # The duplicate makes the attribution ambiguous, not the account fake.
        # Refusing it would break a working Client over a spreadsheet slip.
        snapshot = self.snapshot(
            Client("shop.by", {"google_ads": ("1111111111",)}),
            Client("other.by", {"google_ads": ("1111111111",)}),
        )
        self.assertEqual(
            snapshot.allowed_customer_ids(), frozenset({"1111111111"})
        )

    def test_find_matches_exactly_first(self):
        snapshot = self.snapshot(
            Client("shop.by", {"vk": ("1",)}),
            Client("shop.by.plus", {"vk": ("2",)}),
        )
        self.assertEqual(
            [c.name for c in snapshot.find("shop.by")], ["shop.by"]
        )

    def test_find_falls_back_to_containment(self):
        snapshot = self.snapshot(
            Client("activecloud.by", {"vk": ("1",)}),
            Client("other.by", {"vk": ("2",)}),
        )
        self.assertEqual(
            [c.name for c in snapshot.find("activecloud")], ["activecloud.by"]
        )

    def test_find_ignores_spacing_and_punctuation(self):
        snapshot = self.snapshot(Client("овертайм.бел", {"vk": ("1",)}))
        self.assertEqual(len(snapshot.find("овертайм бел")), 1)

    def test_find_returns_every_candidate_rather_than_the_best(self):
        snapshot = self.snapshot(
            Client("shop.by", {"vk": ("1",)}),
            Client("shop.ru", {"vk": ("2",)}),
        )
        self.assertEqual(len(snapshot.find("shop")), 2)

    def test_find_on_nothing_matches_nothing(self):
        snapshot = self.snapshot(Client("shop.by", {"vk": ("1",)}))
        self.assertEqual(snapshot.find("   "), [])

    def test_age_is_never_negative(self):
        snapshot = self.snapshot(Client("shop.by", {"vk": ("1",)}), age=-60)
        self.assertEqual(snapshot.age_seconds, 0.0)


class TestSnapshotSerialisation(unittest.TestCase):
    def test_round_trips(self):
        original = RegistrySnapshot(
            clients=(
                Client(
                    "shop.by",
                    {"google_ads": ("1111111111", "2222222222"), "vk": ("42",)},
                ),
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
        rows = ppc(["shop.by", "И", "", "123-456-7890", ""])
        with self.respond(payload={"values": rows}):
            snapshot = registry.fetch()

        self.assertEqual([c.name for c in snapshot.clients], ["shop.by"])
        self.assertAlmostEqual(snapshot.age_seconds, 0.0, places=1)

    def test_sends_a_bearer_token_and_never_the_key(self):
        rows = ppc(["shop.by", "И", "", "123-456-7890", ""])
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
