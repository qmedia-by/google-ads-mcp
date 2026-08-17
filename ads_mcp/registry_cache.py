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

"""Holds the last known Registry snapshot in memory, with a copy in Redis.

Fork addition, see FORK.md.

Two callers with different needs meet here. `ads_mcp.access_control` runs
inside synchronous upstream tools and asks for the allowlist on every single
call, so its answer has to cost a dictionary lookup rather than a network round
trip — that constraint is why this module is synchronous and why upstream's
tool signatures stay untouched. The `registry` tools want the freshest Client
list they can get. One snapshot, refreshed no more often than the interval
below, serves both.

Redis is durability, nothing more. Without it the server still works; it just
begins each process with an empty cache, and a first read that lands while
Sheets is unreachable then has nothing to fall back on. With it, the snapshot
outlives deploys — the same Redis already keeps Managers' OAuth tokens, runs
with AOF and has no eviction policy, so nothing quietly drops out of it.
"""

import logging
import os
import threading
from typing import Any, Optional

from ads_mcp import registry
from ads_mcp.registry import RegistrySnapshot, RegistryUnavailable

logger = logging.getLogger(__name__)

# How long a snapshot counts as fresh. The Client → Account mapping changes
# when a Client is signed, which is a monthly event, so this is not about
# tracking edits — it is about not making every tool call wait on Google. A
# Client added minutes ago is found anyway: a lookup that misses forces a read.
REFRESH_INTERVAL_SECONDS = 300.0

# Its own key, deliberately outside whatever prefix FastMCP's client storage
# uses: this is our cache, not part of the OAuth state, and clearing one should
# never mean clearing the other.
REDIS_KEY = "ads_mcp:registry:snapshot"
REDIS_URL_ENV_VAR = "GOOGLE_ADS_MCP_STORAGE_REDIS_URL"

_lock = threading.Lock()
_snapshot: Optional[RegistrySnapshot] = None
_durable_cache_read = False


def _redis_client() -> Optional[Any]:
    """Connects to the same Redis the OAuth store uses, if there is one.

    Every failure here is survivable — the cache degrades to memory-only — so
    none of them propagate. `redis` itself arrives with the
    `py-key-value-aio[redis]` extra the fork already depends on.
    """
    url = os.environ.get(REDIS_URL_ENV_VAR, "").strip()
    if not url:
        return None

    try:
        import redis
    except ImportError:
        logger.warning(
            "redis is not installed; the Registry snapshot will not survive a "
            "restart."
        )
        return None

    try:
        return redis.Redis.from_url(url, socket_timeout=5)
    except Exception as error:
        logger.warning("Could not open the Registry snapshot cache: %s", error)
        return None


def _read_durable_cache() -> Optional[RegistrySnapshot]:
    """Loads the snapshot left behind by an earlier process, if any."""
    client = _redis_client()
    if client is None:
        return None

    try:
        raw = client.get(REDIS_KEY)
    except Exception as error:
        logger.warning("Could not read the Registry snapshot cache: %s", error)
        return None

    if not raw:
        return None

    try:
        snapshot = registry.snapshot_from_json(
            raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        )
    except (RegistryUnavailable, UnicodeDecodeError) as error:
        logger.warning("Discarding an unreadable cached snapshot: %s", error)
        return None

    logger.info(
        "Loaded a Registry snapshot %.0f s old from the cache: %d Clients.",
        snapshot.age_seconds,
        len(snapshot.clients),
    )
    return snapshot


def _write_durable_cache(snapshot: RegistrySnapshot) -> None:
    """Stores the snapshot for the next process. No expiry, by design.

    A snapshot that expired on its own would leave a restarting server with
    nothing at the moment it can least afford it. It is replaced on every
    successful read instead.
    """
    client = _redis_client()
    if client is None:
        return

    try:
        client.set(REDIS_KEY, registry.snapshot_to_json(snapshot))
    except Exception as error:
        logger.warning("Could not store the Registry snapshot: %s", error)


def get_snapshot(*, force_refresh: bool = False) -> Optional[RegistrySnapshot]:
    """Returns the Registry, refreshing it if it has gone stale.

    Returns `None` only when there is genuinely nothing to return: the Registry
    could not be read and no earlier snapshot exists anywhere. Callers decide
    what that means for them — `access_control` refuses, because an unknown
    allowlist is not an empty one.

    `force_refresh` skips the freshness check for the one case that needs it: a
    Client the caller expected to find is missing, and the answer "no such
    Client" should never come out of a five-minute-old copy.
    """
    global _snapshot, _durable_cache_read

    # The whole read is serialised, network call included. For a department of
    # this size that costs a queued request now and then, and it buys the
    # certainty that ten simultaneous calls produce one fetch rather than ten.
    with _lock:
        if not _durable_cache_read:
            _durable_cache_read = True
            _snapshot = _read_durable_cache()

        fresh_enough = (
            _snapshot is not None
            and _snapshot.age_seconds < REFRESH_INTERVAL_SECONDS
        )
        if fresh_enough and not force_refresh:
            return _snapshot

        try:
            snapshot = registry.fetch()
        except RegistryUnavailable as error:
            if _snapshot is None:
                logger.error(
                    "The Registry could not be read and nothing is cached: %s",
                    error,
                )
                return None
            logger.warning(
                "The Registry could not be refreshed; serving a snapshot "
                "%.0f s old: %s",
                _snapshot.age_seconds,
                error,
            )
            return _snapshot

        _snapshot = snapshot
        logger.info(
            "Registry read: %d Clients, %d problems.",
            len(snapshot.clients),
            len(snapshot.problems),
        )
        _write_durable_cache(snapshot)
        return snapshot


def reset() -> None:
    """Forgets everything held in this process. For tests."""
    global _snapshot, _durable_cache_read
    with _lock:
        _snapshot = None
        _durable_cache_read = False
