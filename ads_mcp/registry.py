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

Caching and the fallback to a stale copy live in `ads_mcp.registry_cache`;
this module only fetches and parses.
"""

import base64
import binascii
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Sequence, Tuple

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

# Provider keys. These are the vocabulary the agent's repository already uses
# (Провайдер / Аккаунт in its CONTEXT.md) and they travel through to the tool
# output, so they are not spelled differently here for local convenience.
PROVIDERS: Tuple[str, ...] = ("google_ads", "yandex_direct", "vk")

# SCHEMA PENDING. The real header row has not been seen yet — the Sheet is
# private and access is still being arranged. Headers are written by people and
# will not match one fixed spelling, so they are matched case-, space- and
# punctuation-insensitively against the aliases below.
#
# When the Sheet becomes readable, correcting these tuples is the whole change:
# nothing else in this module knows what a column is called. A header that
# matches nothing is reported, never guessed at — see `parse`.
_NAME_ALIASES = (
    "client",
    "clients",
    "name",
    "клиент",
    "клиенты",
    "название",
    "имя",
)
_PROVIDER_ALIASES: Mapping[str, Tuple[str, ...]] = {
    "google_ads": (
        "google ads",
        "google",
        "customer id",
        "google ads id",
        "гугл",
        "гугл адс",
    ),
    "yandex_direct": (
        "yandex direct",
        "yandex",
        "client login",
        "яндекс директ",
        "яндекс",
        "директ",
        "логин",
    ),
    "vk": (
        "vk",
        "vk ads",
        "вк",
        "вконтакте",
        "vk id",
    ),
}


class RegistryUnavailable(Exception):
    """The Registry could not be read or made sense of.

    Raised for every reason the answer might be missing — no configuration, a
    rejected key, a network failure, a sheet whose header row means nothing to
    us. Callers do not act on the difference: they fall back to the last good
    snapshot either way, and the reason belongs in the log.
    """


@dataclass(frozen=True)
class Client:
    """One Client and the Accounts the Registry gives it."""

    name: str
    accounts: Mapping[str, str]


@dataclass(frozen=True)
class RegistrySnapshot:
    """The Registry as it read at one moment.

    `problems` carries what was wrong with the sheet — a malformed id, an
    Account claimed by two Clients. They are kept rather than raised: one bad
    row must not cost the agent the other forty, but it must not be silent
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
            client.accounts["google_ads"]
            for client in self.clients
            if "google_ads" in client.accounts
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


def _fold(value: str) -> str:
    """Reduces a label to what survives being typed by a different person.

    Case, spacing and punctuation all vary between the Sheet and whatever the
    Manager says to the agent; letters and digits are what actually carry the
    name. Unicode-aware, because half these names are in Cyrillic.
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
    from google.oauth2 import service_account

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
    import google.auth.transport.requests

    try:
        credentials.refresh(google.auth.transport.requests.Request())
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
    import httpx

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
    """Which column holds what, resolved from the header row."""

    name: int
    providers: Mapping[str, int]


def _cell(row: Sequence[str], index: int) -> str:
    """Reads one cell. The Sheets API truncates trailing empty cells."""
    if index < 0 or index >= len(row):
        return ""
    return str(row[index]).strip()


def _read_header(row: Sequence[str]) -> _Columns:
    """Maps a candidate header row onto columns, or raises.

    A provider column claimed twice keeps the leftmost: rereading the same
    provider from two columns would make which Account wins depend on column
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

    if name_index < 0 or not providers:
        raise RegistryUnavailable("not a header row")
    return _Columns(name=name_index, providers=providers)


def parse(
    rows: Sequence[Sequence[str]],
) -> Tuple[List[Client], List[str]]:
    """Turns sheet rows into Clients, collecting what was wrong along the way.

    Raises `RegistryUnavailable` when the sheet cannot be understood at all —
    no header we recognise, or a header but no Client under it. Both mean the
    answer is unknown, and an unknown Registry must fall back to the last good
    snapshot rather than pass itself off as an empty one.
    """
    problems: List[str] = []
    columns = None
    header_row = 0

    # The header is not always the first row: a hand-kept sheet often opens
    # with a title or a note to whoever edits it.
    for index, row in enumerate(rows):
        try:
            columns = _read_header(row)
        except RegistryUnavailable:
            continue
        header_row = index
        break

    if columns is None:
        seen = ", ".join(
            repr(str(cell)) for row in rows[:5] for cell in row if str(cell)
        )
        raise RegistryUnavailable(
            "No header row in the Registry sheet: expected a column naming "
            "the Client and at least one Provider column. Cells seen near the "
            f"top: {seen or '(the sheet is empty)'}"
        )

    clients: List[Client] = []
    seen_names: Dict[str, str] = {}
    seen_accounts: Dict[Tuple[str, str], str] = {}

    for offset, row in enumerate(rows[header_row + 1 :], start=header_row + 2):
        where = f"row {offset}"
        name = _cell(row, columns.name)
        raw_accounts = {
            provider: _cell(row, index)
            for provider, index in columns.providers.items()
        }

        if not name:
            # Blank spacer rows are normal in a sheet people edit by hand and
            # are not worth reporting. A row with ids but no name is.
            if any(raw_accounts.values()):
                problems.append(
                    f"{where}: Accounts listed with no Client name — skipped"
                )
            continue

        accounts: Dict[str, str] = {}
        for provider, value in raw_accounts.items():
            if not value:
                continue
            if provider == "google_ads":
                candidate = normalize_customer_id(value)
                if not candidate.isdigit() or len(candidate) != 10:
                    problems.append(
                        f"{where}: google_ads {value!r} for {name!r} is not a "
                        f"10-digit customer id — ignored"
                    )
                    continue
                value = candidate
            accounts[provider] = value

        if not accounts:
            problems.append(
                f"{where}: Client {name!r} has no Accounts — skipped"
            )
            continue

        folded_name = _fold(name)
        if folded_name in seen_names:
            problems.append(
                f"{where}: {name!r} is listed twice (also as "
                f"{seen_names[folded_name]!r}); the Registry does not say "
                f"which row is current"
            )
        else:
            seen_names[folded_name] = name

        for provider, value in accounts.items():
            key = (provider, value)
            owner = seen_accounts.get(key)
            if owner is not None and owner != name:
                problems.append(
                    f"{where}: {provider} Account {value!r} is listed for both "
                    f"{owner!r} and {name!r} — the Registry does not say which "
                    f"Client it belongs to"
                )
            else:
                seen_accounts[key] = name

        clients.append(Client(name=name, accounts=accounts))

    if not clients:
        raise RegistryUnavailable(
            "The Registry sheet has a header row but no Client under it."
        )

    return clients, problems


def snapshot_to_json(snapshot: RegistrySnapshot) -> str:
    """Serialises a snapshot for the durable cache."""
    return json.dumps(
        {
            "fetched_at": snapshot.fetched_at,
            "problems": list(snapshot.problems),
            "clients": [
                {"name": client.name, "accounts": dict(client.accounts)}
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
                        str(k): str(v)
                        for k, v in dict(entry["accounts"]).items()
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
