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

"""Reads the Account Registry — which Accounts belong to which Client.

Fork addition, see FORK.md. Upstream has no notion of a client: it knows
customer ids, and a manager looking at `list_accessible_customers` sees bare
numbers with no way to tell whose they are.

The mapping lives in a Google Sheet the agency keeps by hand, not in the
agent's repository: that repository is public, and a list of clients with their
account ids has no business being in it.

The Sheet is private and is read by a service account rather than a shared
human login, so the access is granted to this deployment alone and is revoked
by removing one viewer from the file. Nothing here writes back — the Sheet is
edited by people.

**The Sheet keeps credentials in plain text**, and none of them may reach a
snapshot, a Redis key or an agent's context. Two different rules hold them
back, and the two are not interchangeable.

*Primary* columns — Google Ads and VK — are read wherever they appear, and a
block naming neither is skipped whole. That is what keeps the access block,
where every cell is a social password, from being parsed at all.

*Dependent* columns — Yandex Direct and Meta — are read solely inside a block
that already named a primary Provider. Both of them live in the targeting
block, which qualifies because VK is there too, and neither may be promoted to
primary: the credentials block is kept unparsed by naming no primary column at
all, and where that block sits inside a working tab rather than on a tab of its
own, being unparsed is the only protection it has.

The two are then read in opposite ways, and the difference is not stylistic.
The Direct column is the one place the Sheet writes an identifier and a secret
into the same cell: for some projects it holds the cabinet login with its
password beside it. A cell that is not one bare login is therefore dropped
whole rather than mined for the login inside it, under the all-or-nothing rule
in `_extract_yandex_direct`. Mining it would publish the password as readily as
the login, because neither is shaped more like an identifier than the other.

Meta has no such problem and gets the opposite treatment. `_extract_meta` takes
the number the cell marks as an id, at whatever length, and falls back to a
long unmarked number where the cell marks nothing — leaving the prose alone
either way, as the Google Ads and VK extractors do. It has no notion of a list
separator: two cabinets in one cell are two ids however somebody separated
them, and the Direct rule, which refuses a line break as a separator because
that is how a password gets written under a login, would drop exactly those
cells.

Caching and the fallback to a stale copy live in `ads_mcp.registry_cache`;
this module only fetches and parses.
"""

import base64
import binascii
import importlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

SHEET_ID_ENV_VAR = "GOOGLE_ADS_REGISTRY_SHEET_ID"
SERVICE_ACCOUNT_KEY_ENV_VAR = "GOOGLE_ADS_REGISTRY_SA_KEY"
RANGE_ENV_VAR = "GOOGLE_ADS_REGISTRY_RANGE"

# Which tabs of the file hold the Registry, comma-separated and named exactly
# as the tabs are named. An allowlist rather than a denylist, and deliberately:
# the file also carries a tab of Clients who have left, and reading that one
# would put their Accounts back in the allowlist and their names back in
# `find_client`. A denylist would let a tab renamed tomorrow do exactly that.
#
# The cost of an allowlist is that a genuinely new tab is invisible, which
# would be the same silent loss in the other direction — so `fetch` compares
# this list against the tabs the file actually has and reports both
# directions as problems. Unset means the first tab, which is upstream's
# behaviour and keeps the image runnable against any sheet.
TABS_ENV_VAR = "GOOGLE_ADS_REGISTRY_TABS"

# Wide enough for a hand-kept sheet to grow into without an edit here, and
# bounded so a stray value in column ZZ cannot turn one read into a large one.
DEFAULT_RANGE = "A1:Z2000"

# The narrowest scope that can read a Sheet. The Registry is maintained by
# people; a token that cannot write is one fewer way to corrupt it.
_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
_VALUES_ENDPOINT = (
    "https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{range}"
)
_BATCH_VALUES_ENDPOINT = (
    "https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values:batchGet"
)
_METADATA_ENDPOINT = "https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
_TIMEOUT_SECONDS = 20.0

# Provider keys, in the vocabulary the agent's repository already uses.
PROVIDERS: Tuple[str, ...] = ("google_ads", "vk", "yandex_direct", "meta")

# The Providers whose presence makes a block worth reading at all. A block that
# names only dependent columns is not a Provider block — in this Sheet that
# shape is the access block — and `_read_header` drops its columns whole.
PRIMARY_PROVIDERS: Tuple[str, ...] = ("google_ads", "vk")

# Which column means what. Matched case-, space- and punctuation-insensitively,
# so "VK реклама", "vk_реклама" and "ВК Реклама" are one thing. The first entry
# in each tuple is the heading the Sheet actually uses today; the rest are room
# for it to be renamed slightly without breaking the read.
#
# A heading that matches nothing here is ignored, and a *block* with no
# recognised provider column is skipped whole. That is how the credentials
# columns stay unread: they are never named here, so they are never parsed.
_NAME_ALIASES = (
    "проект",
    "клиент",
    "client",
    "project",
    "название",
)
_PROVIDER_ALIASES: Mapping[str, Tuple[str, ...]] = {
    "google_ads": (
        "google ads",
        "googleads",
        "google ads id",
        "гугл адс",
    ),
    "vk": (
        "vk реклама",
        "вк реклама",
        "vk ads",
        "vk",
        "вк",
    ),
    # The heading the Sheet uses today is the dirty one: for some rows that
    # same column holds a password. The clean names are listed beside it so the
    # agency can move logins into a column of their own without a code change
    # here. The extractor is equally strict either way, so which column a value
    # arrived in never decides whether it is published.
    "yandex_direct": (
        "яндекс директ",
        "яндексдирект",
        "yandex direct",
        "yandexdirect",
        "директ",
        "direct",
        "логин директ",
        "логин яндекс директ",
        "direct login",
    ),
    # Meta sits in the targeting block beside TikTok and VK. `Facebook` and
    # `Instagram` are deliberately absent: those are the words the credentials
    # block heads its columns with. A dependent column cannot open that block
    # on its own, so this is a second layer rather than the load-bearing one —
    # but it costs nothing, and a column renamed out of this list fails loudly
    # in `scripts/audit_registry_columns.py`, which prints every heading it
    # meets.
    "meta": (
        "meta",
        "мета",
        "meta ads",
        "мета реклама",
        "meta реклама",
    ),
}

# A Google Ads id as people write it: `123-456-7890` in the interface,
# `1234567890` in the API. The lookarounds keep it from biting a ten-digit
# stretch out of a longer number — a phone number, a GA property id.
_CUSTOMER_ID = re.compile(r"(?<![\d-])(?:\d{3}-\d{3}-\d{4}|\d{10})(?![\d-])")

# A Yandex login as Yandex itself allows one: latin letters and digits, with
# hyphens and dots between them, three to thirty characters, never two
# separators running and never one at either end. Anchored, because this is
# asked of a whole cell rather than searched for inside one.
#
# Lower case is required rather than folded away, and that is the load-bearing
# part. Logins are case-insensitive at Yandex and are written in lower case;
# passwords overwhelmingly are not, and `Qwerty123` is the shape this rules
# out. A login typed with a capital is not silently dropped — it is reported,
# and fixing it is one edit in the Sheet.
_YANDEX_LOGIN = re.compile(r"^[a-z0-9](?:[.-]?[a-z0-9]){2,29}$")

# The same login written as the mailbox it is. Yandex's own domains only: an
# address at any other is somebody's contact, not a Direct account.
_YANDEX_MAIL = re.compile(
    r"^[a-z0-9](?:[.-]?[a-z0-9]){2,29}"
    r"@(?:ya\.ru|yandex\.(?:ru|by|com|kz|ua|com\.tr))$"
)

# What separates two *entries* in one cell, as against what separates a login
# from the password written after it. A comma or a semicolon is how people list
# two cabinets; a space, a slash or a colon is how they write a credential
# pair. Splitting on the first and refusing the second is the whole of the
# rule.
#
# A line break is deliberately *not* a list separator, though it looks like the
# obvious third one. In a sheet people edit by hand, alt-enter inside a cell is
# the commonest way of all to write a login above its password, and both halves
# of that are lowercase latin tokens — so treating it as a list would publish
# the password as a second cabinet. Two cabinets written on two lines are
# refused and reported instead; that costs one edit, and the other reading
# costs a secret.
_DIRECT_SEPARATORS = re.compile(r"[,;]+")

# Words that say a cell is about getting *into* a cabinet rather than naming
# one. Word-bounded, so a login like `passion-shop` is not mistaken for one.
_CREDENTIAL_MARKERS = re.compile(
    r"\b(?:пароль|пароля|пароли|парол|пасс|пассворд|доступ|доступы|логин|"
    r"pass|password|passwd|pwd|login)\b",
    re.IGNORECASE,
)

# VK cells are written as a human name followed by the id in brackets:
# `SOME-NAME (12345678)` or `SOME-NAME (ID 12345678)`. Only the bracketed
# number is taken; whatever else the cell holds stays where it is.
_VK_ID = re.compile(r"\(\s*(?:ID\s*)?(\d{4,})\s*\)", re.IGNORECASE)

# Meta ad account ids come in two situations in this column, and they need
# different rules — one rule for both is what got this wrong the first time.
#
# **Marked.** All but one cell says which number is the id, in one of three
# ways: `NAME (ID 1234…)`, `NAME` over `ID 1234…` or `ID: 1234…`, and `NAME
# (1234…)` with brackets alone. Where the sheet says "this is an id", believe
# it whatever its length. Meta documents no length, its ids have grown over the
# years, and the shortest one here is nine digits — a rule that only trusted
# long numbers dropped that cabinet and called the row broken.
#
# **Unmarked.** Exactly one cell writes the id with nothing to mark it, as
# `NAME 1234…`. Here length is all there is, and it has to carry the whole
# decision, so the floor is set high rather than tight: the unmarked numbers in
# this column that are *not* ids run to four digits (`P7` and the like in
# cabinet names), and the one that is runs to fifteen. Thirteen sits in that
# gap with room on both sides. It is deliberately nowhere near the marked
# floor: an unmarked nine- to twelve-digit number is far more likely to be a
# phone number — the neighbouring columns are full of them — than an ad
# account.
#
# Watch both floors with `scripts/audit_registry_columns.py`. Each is sound
# only while it sits in a gap, and the histogram there is split the same way.
_META_MIN_MARKED_DIGITS = 6
_META_MIN_BARE_DIGITS = 13

# The three markings, with no length in the pattern. The floor is applied in
# code instead, and deliberately: a rejection has to be able to say "you marked
# this and the floor overruled it", which a regex that already excluded the
# number cannot. `scripts/audit_registry_columns.py` splits its histogram on
# this for the same reason.
_META_MARKER = re.compile(
    r"(?:\bact_|\bID\b\s*:?\s*|\(\s*)(\d+)", re.IGNORECASE
)
_META_BARE = re.compile(rf"(?<!\d)\d{{{_META_MIN_BARE_DIGITS},}}(?!\d)")

# Every run of digits, whatever its length. Used only to say how long the
# longest run in a rejected cell was, which is a number about the cell rather
# than anything out of it.
_ANY_DIGITS = re.compile(r"(?<!\d)\d+(?!\d)")

# The words a dropped-Meta-cell problem is recognised by. Same job as
# `DIRECT_DROPPED_MARKER`: `ads_mcp.tools.registry` keys its "unknown, not
# absent" answer off it, so it lives here beside the message that carries it.
META_DROPPED_MARKER = "Meta cell"

# The words a dropped-Direct-cell problem is recognised by. `ads_mcp.tools.
# registry` keys its "unknown, not absent" answer off this, so it lives here
# next to the message that carries it rather than being retyped over there.
DIRECT_DROPPED_MARKER = "Yandex Direct cell"

# What people write in a cell to mean "none". Reporting these as malformed
# would bury the real problems under dozens of deliberate blanks.
_BLANK_MARKERS = frozenset({"", "-", "–", "—", "?", "нет", "н/д", "n/a"})


class RegistryUnavailable(Exception):
    """The Registry could not be read or made sense of.

    Raised for every reason the answer might be missing — no configuration, a
    rejected key, a network failure, a sheet whose header rows mean nothing to
    us. Callers do not act on the difference: they fall back to the last good
    snapshot either way, and the reason belongs in the log.
    """


@dataclass(frozen=True)
class Client:
    """One Client and the Accounts the Registry gives it.

    A Provider maps to *several* ids, not one: the Sheet legitimately splits a
    Client across cabinets — by country, by product line — and collapsing that
    to one id would silently pick a cabinet on the Manager's behalf.
    """

    name: str
    accounts: Mapping[str, Tuple[str, ...]]


@dataclass(frozen=True)
class RegistrySnapshot:
    """The Registry as it read at one moment.

    `problems` carries what was wrong with the sheet — an unreadable id, an
    Account claimed by two Clients. They are kept rather than raised: one bad
    row must not cost the agent the other ninety, but it must not be silent
    either, so the tools hand them to the Manager who can go and fix the Sheet.
    """

    clients: Tuple[Client, ...]
    problems: Tuple[str, ...]
    fetched_at: float

    @property
    def age_seconds(self) -> float:
        """How long ago this was read. Clocks move; a negative age does not."""
        return max(0.0, time.time() - self.fetched_at)

    def allowed_customer_ids(self) -> frozenset:
        """Google Ads ids in the Registry — the server's allowlist.

        An Account listed for two Clients still appears here. The duplicate
        makes the *attribution* ambiguous, which `problems` reports; the id
        itself is a real account of this agency, and refusing it would break a
        working Client over a spreadsheet slip.
        """
        return frozenset(
            customer_id
            for client in self.clients
            for customer_id in client.accounts.get("google_ads", ())
        )

    def find(self, name: str) -> List[Client]:
        """Clients matching `name`, exactly first, then by containment.

        Returns every match rather than the best one. Choosing between two
        similarly named Clients by score is how an agent quietly reports on the
        wrong one; the caller is expected to ask instead.
        """
        wanted = _fold(name)
        if not wanted:
            return []

        exact = [c for c in self.clients if _fold(c.name) == wanted]
        if exact:
            return exact
        return [c for c in self.clients if wanted in _fold(c.name)]


def _require(module: str, purpose: str) -> Any:
    """Imports a module the Registry needs, or says plainly what is missing.

    These are declared dependencies, so a failure here means the build is
    wrong, not the configuration. It happened once: the image resolved without
    httpx and the tool died with a bare `ModuleNotFoundError` in front of a
    Manager, who could do nothing with it. Whoever sees this should be told
    which module and that it is the server's problem, not theirs.
    """
    try:
        return importlib.import_module(module)
    except ImportError as error:
        raise RegistryUnavailable(
            f"The Registry cannot be read: this deployment is missing the "
            f"{module} package, which {purpose}. That is a broken build rather "
            f"than a misconfiguration — report it to whoever administers the "
            f"server."
        ) from error


def _fold(value: str) -> str:
    """Reduces a label to what survives being typed by a different person.

    Case, spacing and punctuation all vary between the Sheet and whatever the
    Manager says to the agent; letters and digits are what actually carry the
    name. Unicode-aware, because most of these names are in Cyrillic — and it
    is what lets `example-shop.by` be found by someone who typed `example-shop`.
    """
    return "".join(ch for ch in value.lower() if ch.isalnum())


_FOLDED_NAME_ALIASES = frozenset(_fold(alias) for alias in _NAME_ALIASES)
_FOLDED_PROVIDER_ALIASES: Dict[str, str] = {
    _fold(alias): provider
    for provider, aliases in _PROVIDER_ALIASES.items()
    for alias in aliases
}


def normalize_customer_id(customer_id: str) -> str:
    """Reduces a Google Ads id to the bare digits the API expects.

    Accounts are written both ways in practice — `123-456-7890` in the Google
    Ads UI, `1234567890` in the API — and whoever keeps the Sheet should not
    have to care which.
    """
    return "".join(str(customer_id).split()).replace("-", "")


def is_configured() -> bool:
    """True when this deployment is meant to have a Registry at all.

    Satisfied by *either* variable on purpose. A deployment that sets the sheet
    id and misspells the key name is misconfigured, and it has to fail loudly;
    treating it as "no Registry configured" would quietly hand back an
    unrestricted allowlist, which is the exact hole this arrangement closes.

    Neither variable set is a legitimate state: it is upstream's behaviour, and
    it keeps the same image runnable locally without a service account.
    """
    return any(
        os.environ.get(var, "").strip()
        for var in (SHEET_ID_ENV_VAR, SERVICE_ACCOUNT_KEY_ENV_VAR)
    )


def _credentials() -> Any:
    """Builds service account credentials from the base64 key in the env.

    Nothing here reaches the logs. Every failure names the variable and the
    shape expected of it, never the value.
    """
    service_account = _require(
        "google.oauth2.service_account", "signs the service account's token"
    )

    raw = os.environ.get(SERVICE_ACCOUNT_KEY_ENV_VAR, "").strip()
    if not raw:
        raise RegistryUnavailable(
            f"{SERVICE_ACCOUNT_KEY_ENV_VAR} is not set: the Registry cannot be "
            f"read without a service account key."
        )

    try:
        decoded = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as error:
        raise RegistryUnavailable(
            f"{SERVICE_ACCOUNT_KEY_ENV_VAR} is not valid base64. Expected the "
            f"service account JSON key encoded with `base64 -w0`."
        ) from error

    try:
        info = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RegistryUnavailable(
            f"{SERVICE_ACCOUNT_KEY_ENV_VAR} does not decode to JSON. Expected "
            f"the service account key file, base64-encoded whole."
        ) from error

    try:
        return service_account.Credentials.from_service_account_info(
            info, scopes=[_SHEETS_SCOPE]
        )
    except ValueError as error:
        raise RegistryUnavailable(
            f"{SERVICE_ACCOUNT_KEY_ENV_VAR} decoded, but is not a usable "
            f"service account key: {error}"
        ) from error


def _access_token(credentials: Any) -> str:
    """Exchanges the key for an access token.

    `google.auth.transport.requests` arrives with google-ads, so this costs no
    dependency of its own.
    """
    transport = _require(
        "google.auth.transport.requests",
        "exchanges the service account key for an access token",
    )

    try:
        credentials.refresh(transport.Request())
    except Exception as error:  # google.auth raises a family of these
        raise RegistryUnavailable(
            f"The service account could not obtain a token: {error}"
        ) from error

    if not credentials.token:
        raise RegistryUnavailable(
            "The service account returned no access token."
        )
    return credentials.token


def configured_tabs() -> Tuple[str, ...]:
    """The tabs this deployment is meant to read, in the order given."""
    raw = os.environ.get(TABS_ENV_VAR, "").strip()
    return tuple(name.strip() for name in raw.split(",") if name.strip())


def _sheet_id() -> str:
    sheet_id = os.environ.get(SHEET_ID_ENV_VAR, "").strip()
    if not sheet_id:
        raise RegistryUnavailable(
            f"{SHEET_ID_ENV_VAR} is not set: there is no Registry to read."
        )
    return sheet_id


def _cell_range() -> str:
    return os.environ.get(RANGE_ENV_VAR, "").strip() or DEFAULT_RANGE


def _get(
    httpx: Any,
    url: str,
    token: str,
    params: Any,
    what: str,
    not_found_hint: str = "",
) -> Any:
    """One GET against the Sheets API, with the failures named as causes.

    The status codes matter more than usual here because nobody is watching:
    a 403 means somebody un-shared the file, and the Manager who sees the
    refusal is the one who can go and ask for it back.
    """
    try:
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as error:
        raise RegistryUnavailable(
            f"The Registry sheet could not be reached: {error}"
        ) from error

    if response.status_code in (401, 403):
        raise RegistryUnavailable(
            f"The Registry sheet refused this service account "
            f"(HTTP {response.status_code}). Check that the file is shared "
            f"with it as a viewer and that the Google Sheets API is enabled."
        )
    if response.status_code == 404:
        raise RegistryUnavailable(
            f"No sheet with id from {SHEET_ID_ENV_VAR} (HTTP 404)"
            f"{not_found_hint}."
        )
    if response.status_code >= 400:
        raise RegistryUnavailable(
            f"The Registry sheet returned HTTP {response.status_code} "
            f"for {what}."
        )

    try:
        return response.json()
    except ValueError as error:
        raise RegistryUnavailable(
            f"The Registry sheet returned a body that is not JSON: {error}"
        ) from error


def _tab_titles(httpx: Any, sheet_id: str, token: str) -> List[str]:
    """The tabs the file actually has, in file order.

    Asked for separately rather than inferred from a failed read, because the
    interesting answers are the two mismatches: a configured tab the file no
    longer has, and a tab the file has that nobody configured. Both are silent
    losses of Clients otherwise, in opposite directions.
    """
    payload = _get(
        httpx,
        _METADATA_ENDPOINT.format(sheet_id=sheet_id),
        token,
        {"fields": "sheets.properties.title"},
        "the list of tabs",
    )
    return [
        str(sheet.get("properties", {}).get("title", ""))
        for sheet in payload.get("sheets") or []
    ]


def _quote_tab(tab: str) -> str:
    """Names a tab in A1 notation. Apostrophes in a tab name double up."""
    escaped = tab.replace("'", "''")
    return f"'{escaped}'!{_cell_range()}"


def fetch() -> RegistrySnapshot:
    """Reads the Sheet and returns it parsed.

    Raises `RegistryUnavailable` for anything that goes wrong, including a
    sheet that parses to nothing useful. Never returns an empty Registry on
    failure: an empty Registry is an empty allowlist, and that would lock every
    Manager out of every Account over a network blip.

    With no tabs configured this reads the first one, which is upstream's
    behaviour and what the fork did before the file grew tabs. With tabs
    configured it reads exactly those, in one batched request, and reports the
    tabs that do not line up either way.
    """
    httpx = _require("httpx", "makes the request to the Sheets API")

    sheet_id = _sheet_id()
    token = _access_token(_credentials())
    wanted = configured_tabs()

    if not wanted:
        payload = _get(
            httpx,
            _VALUES_ENDPOINT.format(sheet_id=sheet_id, range=_cell_range()),
            token,
            {"majorDimension": "ROWS"},
            "the first tab",
            f", or the range {_cell_range()!r} names a tab that does not exist",
        )
        clients, problems = parse(payload.get("values") or [])
        return RegistrySnapshot(
            clients=tuple(clients),
            problems=tuple(problems),
            fetched_at=time.time(),
        )

    titles = _tab_titles(httpx, sheet_id, token)
    present = [tab for tab in wanted if tab in titles]
    missing = [tab for tab in wanted if tab not in titles]
    unlisted = [tab for tab in titles if tab not in wanted]

    if not present:
        # Not "an empty Registry": every configured tab being gone means the
        # file was restructured under us, and the last good snapshot is a far
        # better answer than locking the agency out of every Account.
        raise RegistryUnavailable(
            f"None of the tabs named in {TABS_ENV_VAR} exist in the Registry "
            f"file. Configured: {', '.join(repr(t) for t in wanted)}. Present: "
            f"{', '.join(repr(t) for t in titles) or '(none)'}."
        )

    payload = _get(
        httpx,
        _BATCH_VALUES_ENDPOINT.format(sheet_id=sheet_id),
        token,
        {
            "ranges": [_quote_tab(tab) for tab in present],
            "majorDimension": "ROWS",
        },
        "the configured tabs",
    )

    values = payload.get("valueRanges") or []
    if len(values) != len(present):
        raise RegistryUnavailable(
            f"The Registry sheet returned {len(values)} ranges for "
            f"{len(present)} tabs; the answer cannot be matched to the tabs "
            f"it came from."
        )

    clients, problems = parse_tabs(
        [
            (tab, value.get("values") or [])
            for tab, value in zip(present, values)
        ]
    )

    for tab in missing:
        problems.append(
            f"{TABS_ENV_VAR} names a tab {tab!r} that the file does not have — "
            f"it was renamed or deleted, and any Client on it is missing from "
            f"the Registry entirely."
        )
    for tab in unlisted:
        problems.append(
            f"the file has a tab {tab!r} that {TABS_ENV_VAR} does not name, so "
            f"nothing on it is read. If it holds current Clients, it has to be "
            f"added there; if it holds former Clients or credentials, leaving "
            f"it out is correct."
        )

    return RegistrySnapshot(
        clients=tuple(clients),
        problems=tuple(problems),
        fetched_at=time.time(),
    )


@dataclass(frozen=True)
class _Columns:
    """Which column holds what, resolved from one header row."""

    name: int
    providers: Mapping[str, int]


@dataclass
class _Accumulator:
    """A Client under construction, gathering rows from every block."""

    name: str
    accounts: Dict[str, List[str]] = field(default_factory=dict)

    def add(self, provider: str, ids: Sequence[str]) -> None:
        known = self.accounts.setdefault(provider, [])
        for value in ids:
            if value not in known:
                known.append(value)


def _cell(row: Sequence[str], index: int) -> str:
    """Reads one cell. The Sheets API truncates trailing empty cells."""
    if index < 0 or index >= len(row):
        return ""
    return str(row[index]).strip()


def _is_blank(value: str) -> bool:
    """True for an empty cell and for the dashes people write instead."""
    return value.strip().strip("\\").strip().lower() in _BLANK_MARKERS


def _clean_project(value: str) -> str:
    """Normalises what the Sheet calls a project into a name to show back.

    Entries are domains, and the same domain appears as `kofta.by`,
    `https://kofta.by/` and `www.pamiar.by` in different blocks. Stripping the
    decoration is what lets those merge into one Client instead of three.
    """
    name = value.strip()
    name = re.sub(r"^https?://", "", name, flags=re.IGNORECASE)
    name = re.sub(r"^www\.", "", name, flags=re.IGNORECASE)
    return name.rstrip("/").strip()


def _extract_google_ads(cell: str) -> List[str]:
    """Every customer id in the cell, in order, without repeats.

    Cells hold more than an id: a note about which country or product line a
    cabinet serves, an email, sometimes two cabinets side by side. The prose is
    left alone deliberately — deciding from it which cabinet is "the right one"
    is exactly the guess the Registry exists to prevent. All of them come back,
    and the Manager is asked.
    """
    found: List[str] = []
    for match in _CUSTOMER_ID.findall(cell):
        normalized = normalize_customer_id(match)
        if normalized not in found:
            found.append(normalized)
    return found


def _extract_vk(cell: str) -> List[str]:
    """Every bracketed VK id in the cell, in order, without repeats."""
    found: List[str] = []
    for match in _VK_ID.findall(cell):
        if match not in found:
            found.append(match)
    return found


def _extract_meta(cell: str) -> List[str]:
    """Every Meta ad account id in the cell, in order, without repeats.

    A number the cell marks as an id is taken whatever its length; a number
    with nothing marking it has to be long enough to be unmistakable on its
    own. Trusting the sheet's own label first is what lets a short id through —
    ad account ids here run from nine digits to seventeen, and no floor low
    enough to catch the short one is safe for unmarked numbers, because the
    phone numbers these cells sit among are nine to twelve digits themselves.

    Everything else stays in the cell: cabinet names, the `P7` in a naming
    convention, whatever somebody wrote in the margin.

    Deliberately without any notion of a list separator. Two cabinets in one
    cell are two ids whether somebody put a comma, a line break or nothing
    between them, and `_extract_yandex_direct`'s rule — which refuses a line
    break as a separator, correctly, because that is how a password gets
    written under a login — would drop those cells whole.

    The credential check is kept as a second layer. It is not what makes this
    safe, but the column sits next to the social accounts and it costs a line.
    """
    if _CREDENTIAL_MARKERS.search(cell):
        return []

    # Kept in the order they appear, so two cabinets in one cell come back the
    # way somebody wrote them rather than marked-first.
    hits = [
        (match.start(1), match.group(1))
        for match in _META_MARKER.finditer(cell)
        if len(match.group(1)) >= _META_MIN_MARKED_DIGITS
    ]
    marked = {value for _, value in hits}
    hits.extend(
        (match.start(), match.group())
        for match in _META_BARE.finditer(cell)
        if match.group() not in marked
    )

    found: List[str] = []
    for _, value in sorted(hits):
        if value not in found:
            found.append(value)
    return found


def _why_meta_was_dropped(cell: str) -> str:
    """Says what was wrong with a Meta cell without repeating any of it.

    Same restraint as `_why_direct_was_dropped`, and for the same reason: a
    problem message travels into the snapshot, into Redis and into an agent's
    context. The longest run of digits is given as a *length* because that is
    the one number that tells somebody whether the floor is wrong or the cell
    is — and a length names no digits.
    """
    if _CREDENTIAL_MARKERS.search(cell):
        return "it names a credential rather than an account"

    runs = _ANY_DIGITS.findall(cell)
    if not runs:
        return "it holds no digits at all — a name or a link, but no id"

    longest = max(len(run) for run in runs)
    if _META_MARKER.search(cell):
        return (
            f"it marks a number as an id, but its longest run of digits is "
            f"only {longest} long — under the {_META_MIN_MARKED_DIGITS} an ad "
            f"account id has"
        )
    return (
        f"nothing in it is marked as an id, and its longest run of digits is "
        f"{longest} long — an unmarked number needs {_META_MIN_BARE_DIGITS} to "
        f"be unmistakable. Writing `ID` in front of it fixes the row"
    )


def _direct_parts(cell: str) -> List[str]:
    """Splits a Direct cell into the entries someone meant to list in it."""
    return [
        part.strip()
        for part in _DIRECT_SEPARATORS.split(cell)
        if part.strip() and not _is_blank(part)
    ]


def _extract_yandex_direct(cell: str) -> List[str]:
    """The logins in a Direct cell, or nothing at all if it holds anything else.

    Every other extractor here mines a cell for the ids inside it and leaves
    the prose where it lies. This one must not: the Direct column is where the
    Sheet writes a login and its password side by side, and a password is not
    shaped less like an identifier than a login is. Mining would publish both.

    So this one is all-or-nothing per cell. The cell is split on the characters
    people list *entries* with — comma, semicolon, line break — and every part
    must then be a bare Yandex login on its own. One part that is not, and the
    whole cell is dropped. A login and a password in one cell are separated by
    a space, a slash or a colon, and none of those survive the rule.

    What that leaves is a cell holding one lowercase latin token that is a
    password with no login anywhere near it. Someone would have to have written
    the password where the login belongs and the login nowhere, which leaves
    the cell useless to the people who keep the Sheet as well. LidFly refuses
    such a string against its live directory rather than acting on it. That is
    the accepted residue, and it is the reason a login from here is a candidate
    to be resolved rather than a scope to be used.
    """
    if _CREDENTIAL_MARKERS.search(cell):
        return []

    parts = _direct_parts(cell)
    if not parts:
        return []

    found: List[str] = []
    for part in parts:
        if not (_YANDEX_LOGIN.match(part) or _YANDEX_MAIL.match(part)):
            return []
        if part not in found:
            found.append(part)
    return found


def _why_direct_was_dropped(cell: str) -> str:
    """Says what was wrong with a Direct cell without repeating any of it.

    The whole point of dropping the cell is that its contents must not travel,
    and a problem message travels further than most things here — into the
    snapshot, into Redis, into the Manager's context. So this returns a
    category and never a substring.
    """
    if _CREDENTIAL_MARKERS.search(cell):
        return "it names a credential rather than an account"

    # Order matters. A cell holding `somelogin Qwerty123!` fails both of the
    # first two tests, and "more than one word" is the diagnosis someone can
    # act on — it says split the cell. "Not in lower case" would send them to
    # lowercase a password.
    parts = _direct_parts(cell)
    if any(re.search(r"\s", part) for part in parts):
        return (
            "it holds more than one word — a login with a password beside it "
            "reads like this"
        )
    if any(part != part.lower() for part in parts):
        return "the login is not written in lower case"
    return "it is not shaped like a bare Yandex login"


_EXTRACTORS = {
    "google_ads": _extract_google_ads,
    "vk": _extract_vk,
    "yandex_direct": _extract_yandex_direct,
    "meta": _extract_meta,
}


def _read_header(row: Sequence[str]) -> Optional[_Columns]:
    """Maps a header row onto columns.

    Returns `None` for a row that is not a header at all. A row that names the
    project column but no Provider column is a header of a block we do not
    read — that is how the credentials blocks are recognised and skipped — and
    it comes back as `_Columns` with no providers rather than as `None`, so the
    caller can end the previous block instead of eating the row as data.

    A Provider column claimed twice keeps the leftmost: rereading the same
    Provider from two columns would make which Account wins depend on column
    order, and that is not a thing anyone should have to know.
    """
    name_index = -1
    providers: Dict[str, int] = {}

    for index, raw in enumerate(row):
        folded = _fold(str(raw))
        if not folded:
            continue
        if name_index < 0 and folded in _FOLDED_NAME_ALIASES:
            name_index = index
            continue
        provider = _FOLDED_PROVIDER_ALIASES.get(folded)
        if provider is not None and provider not in providers:
            providers[provider] = index

    if name_index < 0:
        return None

    if not any(provider in providers for provider in PRIMARY_PROVIDERS):
        # A block naming a dependent column and no primary one is not a
        # Provider block. In this Sheet that shape belongs to the access block,
        # where every cell is a credential, and reading a "Директ" column there
        # would mean reading passwords out of it. Emptying `providers` rather
        # than returning None keeps the block *boundary* — the caller still
        # ends the previous block instead of eating this header as data.
        providers = {}

    return _Columns(name=name_index, providers=providers)


def _fold_rows(
    rows: Sequence[Sequence[str]],
    accumulators: Dict[str, _Accumulator],
    problems: List[str],
    tab: str,
) -> bool:
    """Folds one tab's rows into `accumulators`. Says whether it had a header.

    Nothing here raises. Whether the Registry as a whole makes sense is a
    question about every tab together, and it is asked once, by the caller.

    The Sheet is not one table even within a tab. Blocks can sit under one
    another, each with its own header and its own set of Provider columns, and
    one Client routinely appears in more than one place — its Google Ads
    cabinet on the tab the PPC team keeps, its VK cabinet on the one the
    targeting team keeps. Blocks and tabs alike are read in turn and Clients
    merged by project name.
    """
    columns: Optional[_Columns] = None
    saw_header = False

    def where(offset: int) -> str:
        """Names a row the way somebody looking at the file would find it."""
        return f"row {offset} on {tab!r}" if tab else f"row {offset}"

    for offset, row in enumerate(rows, start=1):
        header = _read_header(row)
        if header is not None:
            saw_header = True
            # A block with no Provider column among its headings is one we do
            # not read — in this Sheet those are the ones holding cabinet and
            # social credentials. Its rows are skipped until the next header.
            columns = header if header.providers else None
            continue

        if columns is None:
            continue

        name = _clean_project(_cell(row, columns.name))
        cells = {
            provider: _cell(row, index)
            for provider, index in columns.providers.items()
        }

        if not name:
            # Blank spacer rows are normal in a sheet people edit by hand and
            # are not worth reporting. A row with ids but no project is.
            if any(not _is_blank(value) for value in cells.values()):
                problems.append(
                    f"{where(offset)}: Accounts listed with no project name — "
                    f"skipped"
                )
            continue

        key = _fold(name)
        if not key:
            continue

        accumulator = accumulators.get(key)
        if accumulator is None:
            accumulator = _Accumulator(name=name)
            accumulators[key] = accumulator

        for provider, value in cells.items():
            if _is_blank(value):
                continue
            found = _EXTRACTORS[provider](value)
            if not found:
                if provider == "yandex_direct":
                    problems.append(
                        f"{where(offset)}: the {DIRECT_DROPPED_MARKER} for "
                        f"{name!r} was not read — "
                        f"{_why_direct_was_dropped(value)}. "
                        f"Nothing from it was stored anywhere. Leave the login "
                        f"alone in that cell, in lower case, and keep the "
                        f"password in the access block."
                    )
                elif provider == "meta":
                    problems.append(
                        f"{where(offset)}: the {META_DROPPED_MARKER} for "
                        f"{name!r} was not read — "
                        f"{_why_meta_was_dropped(value)}. "
                        f"Nothing from it was stored anywhere. This is not "
                        f"evidence the Client has no Meta cabinet: for Meta "
                        f"the Registry is navigation and not an allowlist, so "
                        f"ask Meta which ad accounts it can see. Writing the "
                        f"ad account id into that cell fixes the row."
                    )
                else:
                    problems.append(
                        f"{where(offset)}: no {provider} id could be read for "
                        f"{name!r} — the cell is filled but holds nothing "
                        f"shaped like an id"
                    )
                continue
            accumulator.add(provider, found)

    return saw_header


def parse(
    rows: Sequence[Sequence[str]],
) -> Tuple[List[Client], List[str]]:
    """One tab's worth of rows, turned into Clients. See `parse_tabs`."""
    return parse_tabs((("", rows),))


def parse_tabs(
    tabs: Sequence[Tuple[str, Sequence[Sequence[str]]]],
) -> Tuple[List[Client], List[str]]:
    """Turns several tabs into one Registry, collecting what was wrong.

    Clients merge across tabs by project name, exactly as they merge across
    blocks within one. That is not a nicety: the agency keeps `Таргет +
    контекст` for Clients who run both and `Контекст` for those who run only
    that, and a Client who moves between them would otherwise become two.

    Raises `RegistryUnavailable` when the whole thing cannot be understood — no
    header we recognise on any tab, or headers but no Client with a readable
    Account under any of them. Both mean the answer is unknown, and an unknown
    Registry must fall back to the last good snapshot rather than pass itself
    off as an empty one. One unreadable tab among several is *not* that: it is
    a problem, reported, while the rest of the Registry stands.
    """
    problems: List[str] = []
    accumulators: Dict[str, _Accumulator] = {}
    saw_header = False
    rows: Sequence[Sequence[str]] = ()

    for tab, tab_rows in tabs:
        rows = rows or tab_rows
        if _fold_rows(tab_rows, accumulators, problems, tab):
            saw_header = True
        elif tab:
            problems.append(
                f"the tab {tab!r} has no header row naming a project, so "
                f"nothing on it was read. Either it is not a Registry tab, or "
                f"its columns were renamed."
            )

    if not saw_header:
        seen = ", ".join(
            repr(str(cell)) for row in rows[:5] for cell in row if str(cell)
        )
        raise RegistryUnavailable(
            "No header row in the Registry sheet: expected a column naming "
            "the project. Cells seen near the top: "
            f"{seen or '(the sheet is empty)'}"
        )

    clients = [
        Client(
            name=accumulator.name,
            accounts={
                provider: tuple(ids)
                for provider, ids in accumulator.accounts.items()
                if ids
            },
        )
        for accumulator in accumulators.values()
    ]
    clients = [client for client in clients if client.accounts]

    if not clients:
        raise RegistryUnavailable(
            "The Registry sheet has header rows but no Client with a readable "
            "Account under any of them."
        )

    problems.extend(_conflicting_owners(clients))
    problems.extend(_missing_google_ads_entirely(clients))
    return clients, problems


def _missing_google_ads_entirely(clients: Sequence[Client]) -> List[str]:
    """Reports a Registry that parsed but yielded no Google Ads Account at all.

    Before Yandex Direct was read, this state could not arise quietly: Google
    Ads and VK were the only Providers, so a renamed Google Ads column usually
    left no Client standing and the whole read failed loudly as unavailable.
    Now a sheet full of Direct logins parses perfectly well with the Google Ads
    column renamed out from under it, and the only visible symptom would be
    every Google Ads request being refused for an allowlist that is empty
    rather than unknown.

    Fail-closed either way — `access_control` refuses on an empty allowlist —
    but a refusal nobody can explain is the expensive kind. This says what
    happened while there is still someone reading.
    """
    if any("google_ads" in client.accounts for client in clients):
        return []
    return [
        "no Google Ads Account was read anywhere in the Registry, so the "
        "allowlist it produces is empty and every Google Ads request will be "
        "refused. This is a fault of the sheet rather than of any one row — "
        "the usual cause is the Google Ads column having been renamed."
    ]


def _conflicting_owners(clients: Sequence[Client]) -> List[str]:
    """Reports one Account that two different Clients both claim.

    Not a rounding error: whichever way an agent broke the tie, it would be
    reporting one Client's numbers under another's name. Nobody here is in a
    position to decide which row is right, so both are kept and the question
    goes back to the Manager.
    """
    owners: Dict[Tuple[str, str], List[str]] = {}
    for client in clients:
        for provider, ids in client.accounts.items():
            for value in ids:
                owners.setdefault((provider, value), []).append(client.name)

    problems = []
    for (provider, value), names in owners.items():
        if len(names) > 1:
            listed = ", ".join(repr(name) for name in sorted(names))
            problems.append(
                f"{provider} Account {value!r} is listed for {listed} — the "
                f"Registry does not say which Client it belongs to"
            )
    return problems


def snapshot_to_json(snapshot: RegistrySnapshot) -> str:
    """Serialises a snapshot for the durable cache."""
    return json.dumps(
        {
            "fetched_at": snapshot.fetched_at,
            "problems": list(snapshot.problems),
            "clients": [
                {
                    "name": client.name,
                    "accounts": {
                        provider: list(ids)
                        for provider, ids in client.accounts.items()
                    },
                }
                for client in snapshot.clients
            ],
        },
        ensure_ascii=False,
    )


def snapshot_from_json(raw: str) -> RegistrySnapshot:
    """Reads back what `snapshot_to_json` wrote.

    Raises `RegistryUnavailable` on anything unexpected: a cache written by an
    older build is not worth crashing over, and an unreadable one is simply an
    absent one.
    """
    try:
        payload = json.loads(raw)
        return RegistrySnapshot(
            clients=tuple(
                Client(
                    name=str(entry["name"]),
                    accounts={
                        str(provider): tuple(str(v) for v in ids)
                        for provider, ids in dict(entry["accounts"]).items()
                    },
                )
                for entry in payload["clients"]
            ),
            problems=tuple(str(p) for p in payload.get("problems", ())),
            fetched_at=float(payload["fetched_at"]),
        )
    except (
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        raise RegistryUnavailable(
            f"The cached Registry snapshot could not be read: {error}"
        ) from error
