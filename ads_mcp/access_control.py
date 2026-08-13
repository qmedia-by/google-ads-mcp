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

`GOOGLE_ADS_ALLOWED_CUSTOMER_IDS` — customer ids separated by commas —
narrows that to the accounts named in it. Unset or empty means unrestricted,
which is upstream's behaviour and keeps the same image usable without the
variable.
"""

import logging
import os
from typing import Iterable, List

from fastmcp.exceptions import ToolError

logger = logging.getLogger(__name__)

ALLOWED_CUSTOMER_IDS_ENV_VAR = "GOOGLE_ADS_ALLOWED_CUSTOMER_IDS"


def _normalize(customer_id: str) -> str:
    """Reduces an id to the bare digits the API and the allowlist agree on.

    Accounts are written both ways in practice — `123-456-7890` in the Google
    Ads UI, `1234567890` in the API — and a manager pasting the hyphenated
    form must not be refused over punctuation.
    """
    return "".join(str(customer_id).split()).replace("-", "")


def _parse(raw: str) -> frozenset[str]:
    """Reads the env var, dropping entries that cannot be an account id."""
    allowed = set()
    for entry in raw.split(","):
        candidate = _normalize(entry)
        if not candidate:
            continue
        if not candidate.isdigit():
            logger.warning(
                "Ignoring '%s' in %s: a customer id is digits only.",
                entry.strip(),
                ALLOWED_CUSTOMER_IDS_ENV_VAR,
            )
            continue
        allowed.add(candidate)
    return frozenset(allowed)


def get_allowed_customer_ids() -> frozenset[str] | None:
    """Returns the configured allowlist, or None when unrestricted.

    Read on every call rather than cached at import: it costs a dictionary
    lookup next to a network round trip, and it keeps the variable
    straightforward to patch in tests.
    """
    raw = os.environ.get(ALLOWED_CUSTOMER_IDS_ENV_VAR, "")
    if not raw.strip():
        return None
    return _parse(raw)


def ensure_customer_id_allowed(customer_id: str) -> None:
    """Raises ToolError unless the allowlist permits `customer_id`.

    Call it before doing any work for an account: there is no reason to
    authenticate against Google for a request that is going to be refused.
    """
    allowed = get_allowed_customer_ids()
    if allowed is None or _normalize(customer_id) in allowed:
        return

    # Set but unusable. Every account is blocked, and no id the caller could
    # pick instead would help — this one is for the administrator to fix.
    if not allowed:
        raise ToolError(
            f"Account {customer_id} was refused: this server's "
            f"{ALLOWED_CUSTOMER_IDS_ENV_VAR} is set but names no valid "
            f"customer id, so no account can be queried at all. Report this "
            f"to whoever administers the server."
        )

    raise ToolError(
        f"Account {customer_id} is not in this server's list of allowed "
        f"accounts, so it cannot be queried. Allowed accounts: "
        f"{', '.join(sorted(allowed))}. Use one of those, or ask an "
        f"administrator to add {customer_id} to "
        f"{ALLOWED_CUSTOMER_IDS_ENV_VAR}."
    )


def filter_allowed_customer_ids(customer_ids: Iterable[str]) -> List[str]:
    """Keeps only the allowed ids, in the order they arrived.

    Discovery filters instead of refusing: an account the caller may not
    query has no business being offered as a choice in the first place.
    """
    allowed = get_allowed_customer_ids()
    if allowed is None:
        return list(customer_ids)

    accessible = list(customer_ids)
    kept = [cid for cid in accessible if _normalize(cid) in allowed]

    hidden = len(accessible) - len(kept)
    if hidden:
        logger.info(
            "%s hid %d of %d accessible accounts.",
            ALLOWED_CUSTOMER_IDS_ENV_VAR,
            hidden,
            len(accessible),
        )
    return kept
