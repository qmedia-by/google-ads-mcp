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
    return {"name": client.name, "accounts": dict(client.accounts)}


@registry_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def find_client(name: str) -> Dict[str, Any]:
    """Finds a Client in the agency's Registry and returns their Accounts.

    Use this before any other tool whenever the user names a Client rather than
    an account id. It is the only correct way to turn a Client's name into a
    `customer_id`: never pick an id out of `list_accessible_customers` by how
    much it looks like the right one.

    Matching ignores case, spacing and punctuation. If several Clients match,
    all of them are returned and you must ask which one is meant rather than
    choosing. If none match, the Client is not in the Registry: say so and ask
    a Manager to add them.

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

    if not matches:
        result["found"] = False
        result["known_clients"] = [c.name for c in snapshot.clients]
        result["guidance"] = (
            f"No Client in the Registry matches {name!r}. Tell the Manager "
            f"they are not in the Registry and should be added to it. Do not "
            f"guess an account id."
        )
    else:
        result["found"] = True
        problems = _problems_about(snapshot, matches)
        if problems:
            result["problems"] = problems
        if len(matches) > 1:
            result["guidance"] = (
                "Several Clients match. Ask the Manager which one is meant "
                "before querying anything."
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
    }

    if snapshot.problems:
        result["problems"] = list(snapshot.problems)

    warning = _staleness(snapshot)
    if warning:
        result["warning"] = warning
    return result
