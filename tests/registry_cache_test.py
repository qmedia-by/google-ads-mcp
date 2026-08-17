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

"""Test cases for the Registry snapshot cache (fork addition)."""

import time
import unittest
from unittest.mock import MagicMock, patch

from ads_mcp import registry, registry_cache
from ads_mcp.registry import Client, RegistrySnapshot, RegistryUnavailable
from ads_mcp.registry_cache import REFRESH_INTERVAL_SECONDS


def snapshot(name="Ромашка", customer_id="1111111111", age=0.0):
    return RegistrySnapshot(
        clients=(Client(name, {"google_ads": customer_id}),),
        problems=(),
        fetched_at=time.time() - age,
    )


class CacheTestCase(unittest.TestCase):
    """Isolates the module-level snapshot and keeps Redis out of the way."""

    def setUp(self):
        registry_cache.reset()
        self.addCleanup(registry_cache.reset)

        self.redis = patch(
            "ads_mcp.registry_cache._redis_client", return_value=None
        )
        self.redis.start()
        self.addCleanup(self.redis.stop)


class TestGetSnapshot(CacheTestCase):
    def test_returns_none_when_there_is_nothing_anywhere(self):
        with patch.object(
            registry, "fetch", side_effect=RegistryUnavailable("no")
        ):
            self.assertIsNone(registry_cache.get_snapshot())

    def test_fetches_on_the_first_call(self):
        fresh = snapshot()
        with patch.object(registry, "fetch", return_value=fresh) as fetch:
            self.assertIs(registry_cache.get_snapshot(), fresh)
        fetch.assert_called_once()

    def test_serves_a_fresh_snapshot_without_going_back_to_the_sheet(self):
        fresh = snapshot()
        with patch.object(registry, "fetch", return_value=fresh) as fetch:
            registry_cache.get_snapshot()
            registry_cache.get_snapshot()
            registry_cache.get_snapshot()
        fetch.assert_called_once()

    def test_refreshes_once_the_snapshot_has_gone_stale(self):
        stale = snapshot(age=REFRESH_INTERVAL_SECONDS + 1)
        fresh = snapshot(name="Лютик")
        with patch.object(
            registry, "fetch", side_effect=[stale, fresh]
        ) as fetch:
            registry_cache.get_snapshot()
            self.assertIs(registry_cache.get_snapshot(), fresh)
        self.assertEqual(fetch.call_count, 2)

    def test_force_refresh_ignores_freshness(self):
        first = snapshot()
        second = snapshot(name="Лютик")
        with patch.object(registry, "fetch", side_effect=[first, second]):
            registry_cache.get_snapshot()
            self.assertIs(
                registry_cache.get_snapshot(force_refresh=True), second
            )

    def test_serves_the_stale_copy_when_the_sheet_cannot_be_reached(self):
        # The whole reason a snapshot is kept: Google being unreachable must
        # not take the agency's Registry with it.
        stale = snapshot(age=REFRESH_INTERVAL_SECONDS + 1)
        with patch.object(
            registry, "fetch", side_effect=[stale, RegistryUnavailable("no")]
        ):
            registry_cache.get_snapshot()
            self.assertIs(registry_cache.get_snapshot(), stale)

    def test_a_failed_forced_refresh_still_returns_what_it_has(self):
        first = snapshot()
        with patch.object(
            registry, "fetch", side_effect=[first, RegistryUnavailable("no")]
        ):
            registry_cache.get_snapshot()
            self.assertIs(
                registry_cache.get_snapshot(force_refresh=True), first
            )


class TestDurableCache(CacheTestCase):
    def test_loads_a_snapshot_left_by_an_earlier_process(self):
        stored = snapshot(name="Ромашка")
        client = MagicMock()
        client.get.return_value = registry.snapshot_to_json(stored).encode()

        with patch("ads_mcp.registry_cache._redis_client", return_value=client):
            with patch.object(registry, "fetch") as fetch:
                loaded = registry_cache.get_snapshot()

        self.assertEqual(loaded, stored)
        fetch.assert_not_called()

    def test_a_cached_snapshot_survives_the_sheet_being_unreachable(self):
        # Cold start during an outage — the case an in-memory-only cache
        # cannot cover, and the reason Redis is involved at all.
        stored = snapshot(age=REFRESH_INTERVAL_SECONDS + 1)
        client = MagicMock()
        client.get.return_value = registry.snapshot_to_json(stored).encode()

        with patch("ads_mcp.registry_cache._redis_client", return_value=client):
            with patch.object(
                registry, "fetch", side_effect=RegistryUnavailable("no")
            ):
                self.assertEqual(registry_cache.get_snapshot(), stored)

    def test_stores_every_successful_read(self):
        client = MagicMock()
        client.get.return_value = None
        fresh = snapshot()

        with patch("ads_mcp.registry_cache._redis_client", return_value=client):
            with patch.object(registry, "fetch", return_value=fresh):
                registry_cache.get_snapshot()

        client.set.assert_called_once()
        key, payload = client.set.call_args.args
        self.assertEqual(key, registry_cache.REDIS_KEY)
        self.assertEqual(registry.snapshot_from_json(payload), fresh)

    def test_an_unreadable_cache_entry_is_discarded_not_fatal(self):
        client = MagicMock()
        client.get.return_value = b"{ this is not a snapshot"
        fresh = snapshot()

        with patch("ads_mcp.registry_cache._redis_client", return_value=client):
            with patch.object(registry, "fetch", return_value=fresh):
                self.assertIs(registry_cache.get_snapshot(), fresh)

    def test_a_broken_redis_does_not_break_the_registry(self):
        client = MagicMock()
        client.get.side_effect = RuntimeError("connection refused")
        client.set.side_effect = RuntimeError("connection refused")
        fresh = snapshot()

        with patch("ads_mcp.registry_cache._redis_client", return_value=client):
            with patch.object(registry, "fetch", return_value=fresh):
                self.assertIs(registry_cache.get_snapshot(), fresh)


class TestRedisClient(unittest.TestCase):
    """The durable half is optional; running without it is a normal state."""

    def test_no_url_configured_means_no_durable_cache(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(registry_cache._redis_client())

    def test_a_url_that_cannot_be_parsed_is_survivable(self):
        with patch.dict(
            "os.environ",
            {registry_cache.REDIS_URL_ENV_VAR: "nonsense://"},
        ):
            self.assertIsNone(registry_cache._redis_client())


if __name__ == "__main__":
    unittest.main()
