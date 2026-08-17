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

"""Restricts which Google Ads accounts this deployment may reach.

Fork addition, see FORK.md. Upstream enforces no such limit: whoever
authenticates can query every account the configured MCC reaches, so one
mistyped `customer_id` is enough to pull another client's numbers.

The allowlist is **the Registry** (`ads_mcp.registry`) — the accounts named in
the agency's Sheet are the accounts this server will serve, and nothing else.
It used to be a separate `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS` variable copied out
of the Registry by hand, which meant connecting a Client was two operations in
two places and the pair drifted; the symptom was a refusal for an Account the
Registry already listed. One source removes the class of problem rather than
the instance.

A deployment with no Registry configured is unrestricted. That is upstream's
behaviour, and it keeps the same image runnable locally without a service
account. A deployment that has a Registry it cannot read is *not* unrestricted:
it refuses, because an unknown allowlist is not an empty one.
"""

import logging
from typing import Iterable, List, Optional

from fastmcp.exceptions import ToolError

from ads_mcp import registry, registry_cache
from ads_mcp.registry import normalize_customer_id

logger = logging.getLogger(__name__)

# What to tell the caller when the Registry is configured but unreadable. The
# caller cannot fix it by picking a different account, so the message points at
# the one person who can do something about it.
_UNAVAILABLE = (
    "The Registry could not be read, so this server cannot tell which Accounts "
    "it is allowed to query, and it will not guess. This is for whoever "
    "administers the server to fix — check that the Registry sheet is still "
    "shared with the server's service account. See the server log for the "
    "underlying error."
)


def get_allowed_customer_ids() -> Optional[frozenset]:
    """Returns the allowed account ids, or None when unrestricted.

    Raises `ToolError` when a Registry is configured but no copy of it can be
    had — not even a stale one. Reading it costs a dictionary lookup in the
    common case: `registry_cache` holds the snapshot in process memory and only
    reaches for the network when it has gone stale.
    """
    if not registry.is_configured():
        return None

    snapshot = registry_cache.get_snapshot()
    if snapshot is None:
        raise ToolError(_UNAVAILABLE)

    return snapshot.allowed_customer_ids()


def ensure_customer_id_allowed(customer_id: str) -> None:
    """Raises ToolError unless the Registry names `customer_id`.

    Call it before doing any work for an account: there is no reason to
    authenticate against Google for a request that is going to be refused.
    """
    allowed = get_allowed_customer_ids()
    if allowed is None or normalize_customer_id(customer_id) in allowed:
        return

    # A Registry that names no Google Ads account at all. Every account is
    # blocked and no id the caller could pick instead would help, so this one
    # is for the administrator rather than the Manager.
    if not allowed:
        raise ToolError(
            f"Account {customer_id} was refused: the Registry lists no Google "
            f"Ads Accounts at all, so no Account can be queried. Report this "
            f"to whoever administers the server."
        )

    raise ToolError(
        f"Account {customer_id} is not in this agency's Registry, so it "
        f"cannot be queried. Either the Client is missing from the Registry — "
        f"ask a Manager to add it — or the id is a typo. Accounts in the "
        f"Registry: {', '.join(sorted(allowed))}."
    )


def filter_allowed_customer_ids(customer_ids: Iterable[str]) -> List[str]:
    """Keeps only the allowed ids, in the order they arrived.

    Discovery filters instead of refusing: an account the caller may not query
    has no business being offered as a choice in the first place.
    """
    allowed = get_allowed_customer_ids()
    if allowed is None:
        return list(customer_ids)

    accessible = list(customer_ids)
    kept = [cid for cid in accessible if normalize_customer_id(cid) in allowed]

    hidden = len(accessible) - len(kept)
    if hidden:
        logger.info(
            "The Registry hid %d of %d accessible accounts.",
            hidden,
            len(accessible),
        )
    return kept
