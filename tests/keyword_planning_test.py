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

"""Test cases for the keyword planning tools (fork addition)."""

import unittest

from fastmcp.exceptions import ToolError

from ads_mcp.tools.keyword_planning import (
    _build_request,
    _format_idea,
    _resource_name,
)

from google.ads.googleads.v24.services.types.keyword_plan_idea_service import (
    GenerateKeywordIdeaResult,
    GenerateKeywordIdeasRequest,
)


def build(**overrides):
    """Builds a request with valid defaults, overriding named arguments.

    The version the request type is pinned to does not matter here: production
    takes its type from the live client, and `_build_request` only fills in
    whatever message it is handed. Supplying one directly is what keeps these
    tests runnable without credentials.
    """
    kwargs = {
        "customer_id": "1234567890",
        "language": "1031",
        "geo_target_constants": ["2112"],
        "keywords": ["окна пвх"],
        "page_url": "",
        "keyword_plan_network": "GOOGLE_SEARCH",
        "include_adult_keywords": False,
    }
    kwargs.update(overrides)
    return _build_request(GenerateKeywordIdeasRequest(), **kwargs)


class TestResourceName(unittest.TestCase):
    def test_expands_bare_id(self):
        self.assertEqual(
            _resource_name("2112", "geoTargetConstants"),
            "geoTargetConstants/2112",
        )

    def test_passes_through_full_resource_name(self):
        self.assertEqual(
            _resource_name("languageConstants/1031", "languageConstants"),
            "languageConstants/1031",
        )

    def test_rejects_a_name(self):
        # Callers reach for "Belarus" or "ru" — both must fail loudly rather
        # than reach the API as a malformed resource name.
        with self.assertRaises(ToolError):
            _resource_name("Belarus", "geoTargetConstants")

    def test_rejects_empty(self):
        with self.assertRaises(ToolError):
            _resource_name("  ", "geoTargetConstants")


class TestBuildRequest(unittest.TestCase):
    def test_requires_a_seed(self):
        with self.assertRaises(ToolError):
            build(keywords=[], page_url="")

    def test_rejects_more_than_ten_geo_targets(self):
        with self.assertRaises(ToolError):
            build(geo_target_constants=[str(2000 + i) for i in range(11)])

    def test_accepts_exactly_ten_geo_targets(self):
        request = build(geo_target_constants=[str(2000 + i) for i in range(10)])
        self.assertEqual(len(request.geo_target_constants), 10)

    def test_rejects_unknown_network(self):
        with self.assertRaises(ToolError):
            build(keyword_plan_network="DISPLAY")

    def test_keyword_seed(self):
        request = build(keywords=["a", "b"], page_url="")
        self.assertEqual(list(request.keyword_seed.keywords), ["a", "b"])
        self.assertFalse(request.url_seed.url)

    def test_url_seed(self):
        request = build(keywords=[], page_url="https://example.by/okna")
        self.assertEqual(request.url_seed.url, "https://example.by/okna")
        self.assertFalse(list(request.keyword_seed.keywords))

    def test_keyword_and_url_seed(self):
        request = build(keywords=["a"], page_url="https://example.by/okna")
        self.assertEqual(
            request.keyword_and_url_seed.url, "https://example.by/okna"
        )
        self.assertEqual(list(request.keyword_and_url_seed.keywords), ["a"])

    def test_normalises_constants(self):
        request = build(
            language="languageConstants/1031", geo_target_constants=["2112"]
        )
        self.assertEqual(request.language, "languageConstants/1031")
        self.assertEqual(
            list(request.geo_target_constants), ["geoTargetConstants/2112"]
        )


class TestFormatIdea(unittest.TestCase):
    def test_reports_missing_volume_as_none_not_zero(self):
        # A phrase Google has no data for must not be reported as zero demand:
        # someone downstream will treat the zero as a measurement.
        idea = GenerateKeywordIdeaResult()
        idea.text = "окна пвх"

        row = _format_idea(idea)

        self.assertEqual(row["text"], "окна пвх")
        self.assertIsNone(row["avg_monthly_searches"])
        self.assertIsNone(row["low_top_of_page_bid_micros"])
        self.assertIsNone(row["high_top_of_page_bid_micros"])

    def test_reports_reported_values(self):
        idea = GenerateKeywordIdeaResult()
        idea.text = "окна пвх минск"
        idea.keyword_idea_metrics.avg_monthly_searches = 1900
        idea.keyword_idea_metrics.low_top_of_page_bid_micros = 250000
        idea.keyword_idea_metrics.high_top_of_page_bid_micros = 1200000

        row = _format_idea(idea)

        self.assertEqual(row["avg_monthly_searches"], 1900)
        self.assertEqual(row["low_top_of_page_bid_micros"], 250000)
        self.assertEqual(row["high_top_of_page_bid_micros"], 1200000)

    def test_zero_volume_is_preserved(self):
        idea = GenerateKeywordIdeaResult()
        idea.text = "очень редкий запрос"
        idea.keyword_idea_metrics.avg_monthly_searches = 0

        self.assertEqual(_format_idea(idea)["avg_monthly_searches"], 0)


if __name__ == "__main__":
    unittest.main()
