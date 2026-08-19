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

    Two mechanisms keep them out and these are the tests that matter most in
    this file: if they fail, secrets are on their way into a snapshot, a Redis
    key and an agent's context.

    The block rule is here. The cell rule, which is what lets the Direct column
    be read at all, is in `TestYandexDirectCells` below.
    """

    def test_a_login_and_password_in_one_cell_yields_nothing(self):
        secret = "cabinet-login SuperSecret123"
        clients, problems = parse(
            ppc(["shop.by", "Иванов", secret, "123-456-7890", ""])
        )

        self.assertNotIn("yandex_direct", clients[0].accounts)
        rendered = repr(clients) + repr(problems)
        self.assertNotIn("SuperSecret123", rendered)
        self.assertNotIn("cabinet-login", rendered)

    def test_a_dropped_cell_is_reported_without_repeating_any_of_it(self):
        # The problem travels further than the cell did — into the snapshot,
        # into Redis, into the Manager's context. It carries a category and a
        # row number, never a substring.
        secret = "napalm-cabinet / Tr0ub4dor&3"
        _, problems = parse(
            ppc(["shop.by", "Иванов", secret, "123-456-7890", ""])
        )

        self.assertEqual(len(problems), 1)
        self.assertIn("shop.by", problems[0])
        self.assertIn("row 2", problems[0])
        self.assertNotIn("Tr0ub4dor", problems[0])
        self.assertNotIn("napalm", problems[0])

    def test_an_access_block_naming_direct_is_still_skipped_whole(self):
        # The rule that saves us here is that Yandex Direct is a *dependent*
        # column: naming it does not make a block worth reading. Without that,
        # adding the Direct alias would have opened the credentials block.
        rows = [
            *ppc(["shop.by", "Иванов", "", "123-456-7890", ""]),
            [],
            ["Проект", "ТС", "Яндекс Директ", "Доступы"],
            ["other.by", "Петров", "somelogin", "hunter2"],
        ]
        clients, problems = parse(rows)

        self.assertEqual([c.name for c in clients], ["shop.by"])
        rendered = repr(clients) + repr(problems)
        self.assertNotIn("hunter2", rendered)
        self.assertNotIn("somelogin", rendered)

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


class TestYandexDirectCells(unittest.TestCase):
    """The one column where an identifier and a secret share a cell.

    Every other extractor mines a cell and leaves the prose. This one refuses
    the whole cell unless all of it is logins, because a password is not shaped
    less like an identifier than a login is. These tests are that rule.
    """

    def direct(self, cell):
        """Parses one PPC row with `cell` in the Direct column."""
        clients, problems = parse(
            ppc(["shop.by", "Иванов", cell, "123-456-7890", ""])
        )
        return clients[0].accounts.get("yandex_direct"), problems

    def test_a_bare_login_is_published(self):
        accounts, problems = self.direct("example-shop-by")
        self.assertEqual(accounts, ("example-shop-by",))
        self.assertEqual(problems, [])

    def test_dots_and_digits_are_part_of_a_login(self):
        accounts, _ = self.direct("alfa.radon2")
        self.assertEqual(accounts, ("alfa.radon2",))

    def test_a_yandex_mailbox_is_a_login(self):
        accounts, _ = self.direct("example-shop@yandex.by")
        self.assertEqual(accounts, ("example-shop@yandex.by",))

    def test_an_address_at_any_other_domain_is_somebodys_contact(self):
        accounts, problems = self.direct("manager@qmedia.by")
        self.assertIsNone(accounts)
        self.assertEqual(len(problems), 1)

    def test_two_cabinets_listed_with_a_comma_both_come_back(self):
        accounts, problems = self.direct("example-shop-by, example-shop-ru")
        self.assertEqual(accounts, ("example-shop-by", "example-shop-ru"))
        self.assertEqual(problems, [])

    def test_one_bad_entry_costs_the_whole_cell(self):
        # Not "take the good half": the good half is only recognisable as good
        # by the same shape test the bad half just failed.
        accounts, problems = self.direct("example-shop-by, Qwerty123!")
        self.assertIsNone(accounts)
        self.assertEqual(len(problems), 1)

    def test_a_space_between_two_words_is_a_credential_pair(self):
        for cell in (
            "example-shop-by Qwerty123",
            "example-shop-by / qwerty123",
            "example-shop-by: qwerty123",
            "example-shop-by\nqwerty123",
        ):
            with self.subTest(cell=cell):
                accounts, problems = self.direct(cell)
                self.assertIsNone(accounts)
                self.assertEqual(len(problems), 1)

    def test_a_line_break_is_not_a_list_separator(self):
        # Deliberate, and it costs a real case: two cabinets written on two
        # lines are refused. Alt-enter inside a cell is the commonest way of
        # all to write a login above its password, and both halves of that are
        # lowercase latin tokens — reading a line break as a list would publish
        # the password as a second cabinet.
        accounts, problems = self.direct("example-shop-by\nexample-shop-ru")
        self.assertIsNone(accounts)
        self.assertEqual(len(problems), 1)

    def test_a_capital_letter_is_reported_rather_than_swallowed(self):
        # Yandex does not care about case, but `Qwerty123` is the commonest
        # password shape there is, so lower case is required and a login typed
        # with a capital is sent back to be fixed rather than dropped quietly.
        accounts, problems = self.direct("Example-shop")
        self.assertIsNone(accounts)
        self.assertIn("lower case", problems[0])

    def test_a_word_naming_a_credential_drops_the_cell(self):
        for cell in (
            "логин example-shop",
            "example-shop пароль",
            "pass example-shop",
        ):
            with self.subTest(cell=cell):
                accounts, _ = self.direct(cell)
                self.assertIsNone(accounts)

    def test_a_login_that_merely_starts_with_pass_is_not_a_credential(self):
        accounts, _ = self.direct("passion-shop")
        self.assertEqual(accounts, ("passion-shop",))

    def test_dashes_meaning_none_are_not_reported(self):
        for marker in ("-", "–", "—", ""):
            with self.subTest(marker=marker):
                accounts, problems = self.direct(marker)
                self.assertIsNone(accounts)
                self.assertEqual(problems, [])

    def test_a_client_with_only_a_direct_login_reaches_the_registry(self):
        # The point of the whole change: about 28 of the sheet's 92 rows have
        # no Google Ads and no VK, and used to vanish entirely.
        clients, _ = parse(
            ppc(
                ["shop.by", "Иванов", "", "123-456-7890", ""],
                ["direct-only.by", "Иванов", "directonly", "", ""],
            )
        )

        by_name = {c.name: c.accounts for c in clients}
        self.assertEqual(
            by_name["direct-only.by"], {"yandex_direct": ("directonly",)}
        )

    def test_a_direct_login_never_reaches_the_allowlist(self):
        # The allowlist is Google Ads customer ids and nothing else. A login
        # leaking into it would authorise an account nobody granted.
        clients, _ = parse(
            ppc(["shop.by", "Иванов", "example-shop", "123-456-7890", ""])
        )
        allowed = RegistrySnapshot(
            clients=tuple(clients), problems=(), fetched_at=time.time()
        ).allowed_customer_ids()

        self.assertEqual(allowed, frozenset({"1234567890"}))


class TestSheetLevelFaults(unittest.TestCase):
    def test_a_registry_with_no_google_ads_anywhere_says_so(self):
        # Reading Direct made this state survivable where it used to be fatal:
        # a sheet whose Google Ads column was renamed now parses perfectly on
        # its Direct logins, and the only symptom would be every Google Ads
        # request refused for an allowlist that is empty rather than unknown.
        clients, problems = parse(
            [
                ["Проект", "ТС PPC", "Яндекс Директ", "Гугл реклама", "VK"],
                ["shop.by", "Иванов", "example-shop", "123-456-7890", ""],
            ]
        )

        self.assertEqual(
            clients[0].accounts, {"yandex_direct": ("example-shop",)}
        )
        self.assertTrue(any("no Google Ads Account" in p for p in problems))


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
                # The second row only keeps the sheet-level "no Google Ads
                # anywhere" alarm quiet; this test is about the first one.
                rows = ppc(
                    ["shop.by", "И", "", marker, "SHOP (19142062)"],
                    ["other.by", "И", "", "111-111-1111", ""],
                )
                clients, problems = parse(rows)
                self.assertEqual(problems, [])
                self.assertNotIn("google_ads", clients[0].accounts)

    def test_a_filled_cell_with_nothing_id_shaped_is_reported(self):
        clients, problems = parse(
            ppc(
                ["shop.by", "И", "", "уточняется у клиента", "SHOP (1914)"],
                ["other.by", "И", "", "111-111-1111", ""],
            )
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
            Client("example-shop.by", {"vk": ("1",)}),
            Client("other.by", {"vk": ("2",)}),
        )
        self.assertEqual(
            [c.name for c in snapshot.find("example-shop")], ["example-shop.by"]
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


class TestMissingDependency(unittest.TestCase):
    """A package absent from the image must not reach a Manager raw.

    It did once: the first deploy of the Registry resolved without httpx, and
    `ModuleNotFoundError: No module named 'httpx'` went straight to the person
    asking for a list of Clients, who could do nothing about it.
    """

    def test_a_missing_package_is_the_server_s_fault_and_says_so(self):
        with patch("importlib.import_module", side_effect=ImportError("no")):
            with self.assertRaises(RegistryUnavailable) as context:
                registry._require("httpx", "makes the request")

        message = str(context.exception)
        self.assertIn("httpx", message)
        self.assertIn("administers", message)

    def test_fetch_turns_it_into_an_ordinary_unavailable_registry(self):
        # Which means the cache falls back to its snapshot rather than the
        # whole call stack blowing up.
        environment = {
            SHEET_ID_ENV_VAR: "sheet-id",
            SERVICE_ACCOUNT_KEY_ENV_VAR: "key",
        }
        with patch.dict("os.environ", environment):
            with patch(
                "importlib.import_module", side_effect=ImportError("no")
            ):
                with self.assertRaises(RegistryUnavailable):
                    registry.fetch()


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


class FetchTestCase(unittest.TestCase):
    def setUp(self):
        # `GOOGLE_ADS_REGISTRY_TABS` is blanked rather than left alone: a
        # developer with it set in their shell would otherwise send the
        # single-tab tests down the multi-tab path and read the failures as a
        # bug in the code.
        self.env = patch.dict(
            "os.environ",
            {
                SHEET_ID_ENV_VAR: "sheet-id",
                SERVICE_ACCOUNT_KEY_ENV_VAR: "k",
                registry.TABS_ENV_VAR: "",
            },
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


class TestFetch(FetchTestCase):
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


class TestFetchTabs(FetchTestCase):
    """Reading several named tabs rather than whichever one is first.

    The file the agency keeps has five: two hold current Clients, one holds
    Clients who have left, one is credentials and one is empty. Reading the
    first tab alone was hiding 43 of 67 Clients; reading all of them would put
    former Clients back in the allowlist. Hence a list, and hence the reporting
    when the list and the file disagree.
    """

    def responses(self, titles, values, status_code=200):
        """Answers the tab listing, then the batched values read."""
        listing = MagicMock()
        listing.status_code = 200
        listing.json.return_value = {
            "sheets": [{"properties": {"title": t}} for t in titles]
        }
        batch = MagicMock()
        batch.status_code = status_code
        batch.json.return_value = {
            "valueRanges": [{"values": rows} for rows in values]
        }
        return patch("httpx.get", side_effect=[listing, batch])

    def configured(self, *tabs):
        return patch.dict("os.environ", {registry.TABS_ENV_VAR: ",".join(tabs)})

    def test_merges_one_client_across_two_tabs(self):
        # The agency keeps `Таргет + контекст` for Clients who run both and
        # `Контекст` for those who run only that. A Client who moves between
        # them must stay one Client, not become two.
        first = ppc(["shop.by", "И", "", "123-456-7890", ""])
        second = ppc(["shop.by", "И", "example-shop", "", ""])

        with self.configured("A", "B"):
            with self.responses(["A", "B"], [first, second]):
                snapshot = registry.fetch()

        self.assertEqual(len(snapshot.clients), 1)
        self.assertEqual(
            snapshot.clients[0].accounts,
            {"google_ads": ("1234567890",), "yandex_direct": ("example-shop",)},
        )

    def test_a_problem_says_which_tab_the_row_is_on(self):
        # "row 2" is useless across five tabs; the Manager has to find it.
        rows = ppc(["shop.by", "И", "login pass", "123-456-7890", ""])

        with self.configured("Контекст"):
            with self.responses(["Контекст"], [rows]):
                snapshot = registry.fetch()

        self.assertTrue(
            any("'Контекст'" in p for p in snapshot.problems), snapshot.problems
        )

    def test_a_tab_the_file_no_longer_has_is_reported(self):
        rows = ppc(["shop.by", "И", "", "123-456-7890", ""])

        with self.configured("Контекст", "Ушедшие"):
            with self.responses(["Контекст"], [rows]):
                snapshot = registry.fetch()

        self.assertTrue(any("'Ушедшие'" in p for p in snapshot.problems))

    def test_a_tab_nobody_configured_is_reported_too(self):
        # The one weakness of an allowlist is that a new tab is invisible.
        # Saying so out loud is what makes the loss recoverable.
        rows = ppc(["shop.by", "И", "", "123-456-7890", ""])

        with self.configured("Контекст"):
            with self.responses(["Контекст", "Новая"], [rows]):
                snapshot = registry.fetch()

        self.assertTrue(any("'Новая'" in p for p in snapshot.problems))

    def test_a_tab_of_former_clients_is_never_read(self):
        # The whole reason this is an allowlist and not a denylist.
        current = ppc(["shop.by", "И", "", "123-456-7890", ""])

        with self.configured("Контекст"):
            with self.responses(["Контекст", "покинувшие нас"], [current]):
                snapshot = registry.fetch()

        self.assertEqual([c.name for c in snapshot.clients], ["shop.by"])

    def test_every_configured_tab_gone_is_unavailable_not_empty(self):
        with self.configured("Контекст"):
            with self.responses(["Что-то другое"], []):
                with self.assertRaises(RegistryUnavailable) as context:
                    registry.fetch()

        self.assertIn(registry.TABS_ENV_VAR, str(context.exception))

    def test_the_tabs_are_read_in_one_batched_request(self):
        rows = ppc(["shop.by", "И", "", "123-456-7890", ""])

        with self.configured("A", "B"):
            with self.responses(["A", "B"], [rows, []]) as mock_get:
                registry.fetch()

        self.assertEqual(mock_get.call_count, 2)  # the listing, then the values
        ranges = mock_get.call_args.kwargs["params"]["ranges"]
        self.assertEqual(ranges, ["'A'!A1:Z2000", "'B'!A1:Z2000"])


if __name__ == "__main__":
    unittest.main()
