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

**Only two columns are ever read: Google Ads and VK.** The Sheet is a working
document of the agency and its other columns hold credentials in plain text —
cabinet logins next to their passwords, social accounts next to theirs. Those
must not reach a snapshot, a cache or an agent's context, and the cheapest way
to guarantee that is to never parse the columns they live in. Yandex Direct is
therefore absent from the Registry by design, not by omission: the agent
reaches Direct through LidFly, which carries its own account context.

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

# Wide enough for a hand-kept sheet to grow into without an edit here, and
# bounded so a stray value in column ZZ cannot turn one read into a large one.
DEFAULT_RANGE = "A1:Z2000"

# The narrowest scope that can read a Sheet. The Registry is maintained by
# people; a token that cannot write is one fewer way to corrupt it.
_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
_VALUES_ENDPOINT = (
    "https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{range}"
)
_TIMEOUT_SECONDS = 20.0

# Provider keys, in the vocabulary the agent's repository already uses. Yandex
# Direct is deliberately not among them — see the module docstring.
PROVIDERS: Tuple[str, ...] = ("google_ads", "vk")

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
}

# A Google Ads id as people write it: `123-456-7890` in the interface,
# `1234567890` in the API. The lookarounds keep it from biting a ten-digit
# stretch out of a longer number — a phone number, a GA property id.
_CUSTOMER_ID = re.compile(r"(?<![\d-])(?:\d{3}-\d{3}-\d{4}|\d{10})(?![\d-])")

# VK cells are written as a human name followed by the id in brackets:
# `SOME-NAME (12345678)` or `SOME-NAME (ID 12345678)`. Only the bracketed
# number is taken; whatever else the cell holds stays where it is.
_VK_ID = re.compile(r"\(\s*(?:ID\s*)?(\d{4,})\s*\)", re.IGNORECASE)

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
    is what lets `activecloud.by` be found by someone who typed `activecloud`.
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


def fetch() -> RegistrySnapshot:
    """Reads the Sheet and returns it parsed.

    Raises `RegistryUnavailable` for anything that goes wrong, including a
    sheet that parses to nothing useful. Never returns an empty Registry on
    failure: an empty Registry is an empty allowlist, and that would lock every
    Manager out of every Account over a network blip.
    """
    httpx = _require("httpx", "makes the request to the Sheets API")

    sheet_id = os.environ.get(SHEET_ID_ENV_VAR, "").strip()
    if not sheet_id:
        raise RegistryUnavailable(
            f"{SHEET_ID_ENV_VAR} is not set: there is no Registry to read."
        )

    cell_range = os.environ.get(RANGE_ENV_VAR, "").strip() or DEFAULT_RANGE
    url = _VALUES_ENDPOINT.format(sheet_id=sheet_id, range=cell_range)
    token = _access_token(_credentials())

    try:
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params={"majorDimension": "ROWS"},
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
            f"No sheet with id from {SHEET_ID_ENV_VAR} (HTTP 404), or the "
            f"range {cell_range!r} names a tab that does not exist."
        )
    if response.status_code >= 400:
        raise RegistryUnavailable(
            f"The Registry sheet returned HTTP {response.status_code}."
        )

    try:
        rows = response.json().get("values") or []
    except ValueError as error:
        raise RegistryUnavailable(
            f"The Registry sheet returned a body that is not JSON: {error}"
        ) from error

    clients, problems = parse(rows)
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


_EXTRACTORS = {
    "google_ads": _extract_google_ads,
    "vk": _extract_vk,
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
    return _Columns(name=name_index, providers=providers)


def parse(
    rows: Sequence[Sequence[str]],
) -> Tuple[List[Client], List[str]]:
    """Turns sheet rows into Clients, collecting what was wrong along the way.

    The Sheet is not one table. Several blocks sit under one another on the
    same tab, each with its own header and its own set of Provider columns, and
    one Client routinely appears in more than one of them — its Google Ads
    cabinet in the block the PPC team keeps, its VK cabinet in the block the
    targeting team keeps. Blocks are therefore read in turn and Clients merged
    by project name.

    Raises `RegistryUnavailable` when the sheet cannot be understood at all —
    no header we recognise anywhere, or headers but no Client under any of
    them. Both mean the answer is unknown, and an unknown Registry must fall
    back to the last good snapshot rather than pass itself off as an empty one.
    """
    problems: List[str] = []
    accumulators: Dict[str, _Accumulator] = {}
    columns: Optional[_Columns] = None
    saw_header = False

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
                    f"row {offset}: Accounts listed with no project name — "
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
                problems.append(
                    f"row {offset}: no {provider} id could be read for "
                    f"{name!r} — the cell is filled but holds nothing shaped "
                    f"like an id"
                )
                continue
            accumulator.add(provider, found)

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
    return clients, problems


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
