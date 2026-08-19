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

"""Tools for looking a Client up in the Account Registry.

Fork addition, see FORK.md. These answer the question upstream cannot:
`list_accessible_customers` returns bare numeric ids, and a Manager running two
dozen Clients cannot tell which is which.
"""

from typing import Any, Dict, List, Sequence

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

# Imported as modules, not as names: `ads_mcp.tools.registry` and
# `ads_mcp.registry` are different things and reading `account_registry` at the
# call site says which one is meant.
from ads_mcp import registry as account_registry
from ads_mcp import registry_cache
from ads_mcp.registry import (
    DIRECT_DROPPED_MARKER as _DIRECT_DROPPED_MARKER,
)
from ads_mcp.registry import Client, RegistrySnapshot
from ads_mcp.registry_cache import REFRESH_INTERVAL_SECONDS

registry_mcp = FastMCP("registry")

_NOT_CONFIGURED = (
    "This server has no Registry configured, so it cannot map Clients to "
    "Accounts. Ask the Manager for the account id directly, and report that "
    "the server is missing its Registry configuration."
)

_UNAVAILABLE = (
    "The Registry could not be read and no earlier copy is cached, so which "
    "Accounts belong to this Client is unknown. Do not guess and do not fall "
    "back to listing accessible accounts — ask the Manager to name the account "
    "id, and tell them the Registry is unreachable."
)

_DIRECT_IN_LISTING = (
    "A Client shows `yandex_direct` here only where the sheet records the "
    "Direct login on its own in the Direct column. Where it is written with a "
    "password beside it the cell is not read at all, and that Client appears "
    "without `yandex_direct` despite having a Direct Account. Its absence is "
    "therefore never evidence: a Direct Account is confirmed or ruled out "
    'through LidFly\'s `get_provider_context` with provider "yandex", not '
    "here. Meta and TikTok are not in the Registry at all, so a Client who "
    "runs only those is missing from this listing entirely — do not offer it "
    "as the agency's full list of Clients."
)

_DIRECT_FOUND = (
    "The Registry has a Direct login for this Client, under `yandex_direct` "
    "in `accounts`. It is a candidate, not a resolved scope: the Registry is "
    "a spreadsheet, and a login read out of one has not been checked against "
    "anything. Pass it to LidFly's `get_provider_context` as `client_login` "
    'with provider "yandex" and work from what that returns. If LidFly does '
    "not know the login, say so plainly — do not go hunting for a similar one."
)


def _direct_note(name: str, found: bool, dropped: bool) -> str:
    """Says where Yandex Direct stands, in the answer rather than a docstring.

    The docstring carries the same facts, but it is read before the call and
    competes with everything else in the tool list; this is read after it, as
    the answer to the question actually asked. That gap is the bug this exists
    for: an agent handed a Client with a Google Ads id and no Direct one had
    nothing in front of it saying where Direct lives, and either wandered
    around LidFly guessing arguments or told the Manager the Client has no
    Direct Account.

    Three states, not two, and conflating any pair of them gives the Manager a
    wrong answer. **Found** is a candidate login to go and resolve. **Dropped**
    means the sheet has something here that could not be published safely — the
    Client almost certainly does run Direct, and somebody should go and tidy
    the cell. **Absent** means the Registry says nothing at all, which is still
    not evidence: the Client may be missing from the Direct column entirely.

    Only the first of those puts anything in `accounts`. The other two live up
    here at the top level, because an empty list under `yandex_direct` would
    read as "this Client has no Direct Account" — the exact false statement
    being prevented.
    """
    if found:
        return _DIRECT_FOUND
    if dropped:
        return (
            "The Registry holds something for this Client under Yandex Direct "
            "that could not be published — see `problems`. Read that as "
            "unknown rather than absent: a cell is usually dropped because it "
            "has the password written into it, which means the Account exists. "
            "Find it through LidFly's `get_provider_context` with provider "
            f'"yandex" and query {name!r}, and tell the Manager the Registry '
            "cell needs cleaning up."
        )
    return (
        "The Registry has no Yandex Direct login for this Client. That is no "
        "evidence either way — never report a Direct Account as missing on "
        "the strength of it, since the Registry covers Direct only where the "
        "sheet happens to record the login cleanly. If Direct is what was "
        "asked about, call LidFly's `get_provider_context` with provider "
        f'"yandex" and query {name!r}.'
    )


def _describe_age(seconds: float) -> str:
    """Puts an age in words, at the coarseness anyone actually acts on."""
    if seconds < 120:
        return "less than two minutes"
    if seconds < 5400:
        return f"about {round(seconds / 60)} minutes"
    if seconds < 172800:
        return f"about {round(seconds / 3600)} hours"
    return f"about {round(seconds / 86400)} days"


def _staleness(snapshot: RegistrySnapshot) -> str:
    """Warns when this answer comes from a copy that could not be refreshed.

    A snapshot older than the refresh interval can only mean the last attempt
    to re-read the Sheet failed — `registry_cache` refreshes on the way out
    otherwise. That is worth telling the Manager, who is the one who can go and
    find out why.
    """
    age = snapshot.age_seconds
    if age <= REFRESH_INTERVAL_SECONDS:
        return ""
    return (
        f"The Registry could not be re-read just now; this answer comes from a "
        f"copy taken {_describe_age(age)} ago. A Client added since then would "
        f"be missing from it. Tell the Manager, and mention that someone "
        f"should check the Registry sheet is still shared with the server."
    )


def _problems_about(
    snapshot: RegistrySnapshot, clients: Sequence[Client]
) -> List[str]:
    """Registry problems that concern these Clients.

    Matched by name appearing in the message, which is crude but right for what
    it is used for: the point is that a Manager asking about one Client is told
    their row is broken, without being handed every unrelated flaw in the sheet
    on every call.
    """
    names = [client.name for client in clients]
    return [
        problem
        for problem in snapshot.problems
        if any(name in problem for name in names)
    ]


def _snapshot(*, force_refresh: bool = False) -> RegistrySnapshot:
    """Returns the Registry, or refuses in a way the agent can act on."""
    if not account_registry.is_configured():
        raise ToolError(_NOT_CONFIGURED)

    snapshot = registry_cache.get_snapshot(force_refresh=force_refresh)
    if snapshot is None:
        raise ToolError(_UNAVAILABLE)
    return snapshot


def _render(client: Client) -> Dict[str, Any]:
    return {
        "name": client.name,
        "accounts": {
            provider: list(ids) for provider, ids in client.accounts.items()
        },
    }


def _providers_with_several(clients: Sequence[Client]) -> List[str]:
    """Providers where a returned Client has more than one Account.

    Normal rather than broken: the agency splits a Client across cabinets by
    country or product line. It still has to be asked about, because picking
    one silently means reporting on half a Client.
    """
    return sorted(
        {
            provider
            for client in clients
            for provider, ids in client.accounts.items()
            if len(ids) > 1
        }
    )


@registry_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def find_client(name: str) -> Dict[str, Any]:
    """Finds a Client in the agency's Registry and returns their Accounts.

    Use this before any other tool whenever the user names a Client rather than
    an account id. It is the only correct way to turn a Client's name into a
    `customer_id`: never pick an id out of `list_accessible_customers` by how
    much it looks like the right one.

    Matching ignores case, spacing and punctuation, so a Client recorded as
    `example-shop.by` is found by "example-shop". If several Clients match, all
    of them are returned and you must ask which one is meant rather than
    choosing. If none match, the Client is not in the Registry: say so and ask
    a Manager to add them.

    Each Provider maps to a *list* of account ids, usually of one. More than
    one means the agency runs that Client through several cabinets — split by
    country or product line — and you must ask which is meant. Never query all
    of them and add the numbers up.

    Google Ads and VK ids here are ready to use. A `yandex_direct` login is
    not: it is a candidate to hand to LidFly's `get_provider_context` as
    `client_login`, and LidFly decides whether it is real. The Registry covers
    Direct only where the sheet records the login cleanly, so its absence is
    never evidence that a Client has no Direct Account — the `yandex_direct`
    field returned on every call says which of the three cases this answer is.
    Meta and TikTok are not in the Registry at all.

    Args:
        name: The Client's name, as the user said it.

    Returns:
        The matching Clients with their account ids per Provider, any problems
        the Registry has with those Clients, and a warning if the answer came
        from a copy that could not be refreshed.
    """
    snapshot = _snapshot()
    matches = snapshot.find(name)

    # A miss is the one answer that must never come out of a stale copy: the
    # Client may have been added to the sheet minutes ago. Pay for one live
    # read before saying "not in the Registry".
    if not matches:
        snapshot = _snapshot(force_refresh=True)
        matches = snapshot.find(name)

    result: Dict[str, Any] = {
        "query": name,
        "clients": [_render(client) for client in matches],
    }

    problems = _problems_about(snapshot, matches) if matches else []
    direct_dropped = any(
        _DIRECT_DROPPED_MARKER in problem for problem in problems
    )
    direct_found = any(
        client.accounts.get("yandex_direct") for client in matches
    )

    if not matches:
        result["found"] = False
        result["known_clients"] = [c.name for c in snapshot.clients]
        result["guidance"] = (
            f"No Client in the Registry matches {name!r}. The Registry covers "
            f"Google Ads, VK, and Yandex Direct where the sheet records the "
            f"login cleanly — so this means no Account was found with any of "
            f"those, not that the agency does not run the Client. Meta and "
            f"TikTok are not in the Registry at all, and a Direct login "
            f"written in the sheet next to its password is not either. Say "
            f"which Providers were actually checked rather than that the "
            f"Client is unknown, and ask for them to be added to the Registry "
            f"only if Google Ads or VK is what was wanted. Direct is still "
            f"worth trying through LidFly. Do not guess an account id."
        )
    else:
        result["found"] = True
        if problems:
            result["problems"] = problems

        several = _providers_with_several(matches)
        if len(matches) > 1:
            result["guidance"] = (
                "Several Clients match. Ask the Manager which one is meant "
                "before querying anything."
            )
        elif several:
            result["guidance"] = (
                f"This Client has more than one Account for: "
                f"{', '.join(several)}. The agency splits Clients across "
                f"cabinets by country or product line, so ask the Manager "
                f"which one they mean rather than picking one or merging the "
                f"numbers."
            )

    result["yandex_direct"] = _direct_note(
        name, found=direct_found, dropped=direct_dropped
    )

    warning = _staleness(snapshot)
    if warning:
        result["warning"] = warning
    return result


@registry_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_clients() -> Dict[str, Any]:
    """Lists every Client in the agency's Registry, with their Providers.

    Use it when a Client cannot be found by name and you need to offer the
    Manager the nearest candidates, or when they ask who is in the Registry.
    Account ids are deliberately not included — call `find_client` for the
    Client you actually need.

    This is not every Client of the agency, and `providers` is not everything a
    Client runs. Meta and TikTok are outside the Registry, and a Yandex Direct
    login the sheet records next to its password is not read, so it shows up
    here as no Direct at all. Do not present either as a complete list.

    Returns:
        Every Client's name and which Providers they have an Account with, any
        problems found in the Registry, and a warning if the answer came from a
        copy that could not be refreshed.
    """
    snapshot = _snapshot()

    result: Dict[str, Any] = {
        "clients": [
            {"name": client.name, "providers": sorted(client.accounts)}
            for client in snapshot.clients
        ],
        "count": len(snapshot.clients),
        "yandex_direct": _DIRECT_IN_LISTING,
    }

    if snapshot.problems:
        result["problems"] = list(snapshot.problems)

    warning = _staleness(snapshot)
    if warning:
        result["warning"] = warning
    return result
