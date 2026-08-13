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
from unittest.mock import MagicMock, patch

from fastmcp.exceptions import ToolError

from ads_mcp.tools.keyword_planning import (
    _build_request,
    _format_idea,
    _generate_ideas,
    _is_retryable,
    _resource_name,
)

from google.ads.googleads.errors import GoogleAdsException
from google.ads.googleads.v24.errors.types.authorization_error import (
    AuthorizationErrorEnum,
)
from google.ads.googleads.v24.errors.types.errors import (
    ErrorCode,
    GoogleAdsError,
    GoogleAdsFailure,
)
from google.ads.googleads.v24.errors.types.internal_error import (
    InternalErrorEnum,
)
from google.ads.googleads.v24.services.types.keyword_plan_idea_service import (
    GenerateKeywordIdeaResult,
    GenerateKeywordIdeasRequest,
)


def failure(*error_codes: ErrorCode) -> GoogleAdsFailure:
    """Builds a failure carrying the given error codes."""
    return GoogleAdsFailure(
        errors=[
            GoogleAdsError(error_code=code, message="boom")
            for code in error_codes
        ]
    )


DEADLINE_EXCEEDED = ErrorCode(
    internal_error=InternalErrorEnum.InternalError.DEADLINE_EXCEEDED
)
TRANSIENT_ERROR = ErrorCode(
    internal_error=InternalErrorEnum.InternalError.TRANSIENT_ERROR
)
INTERNAL_ERROR = ErrorCode(
    internal_error=InternalErrorEnum.InternalError.INTERNAL_ERROR
)
NOT_PERMITTED = ErrorCode(
    authorization_error=(
        AuthorizationErrorEnum.AuthorizationError.USER_PERMISSION_DENIED
    )
)


def exception(fail: GoogleAdsFailure) -> GoogleAdsException:
    """Wraps a failure the way the client library delivers it."""
    ex = GoogleAdsException(
        MagicMock(), MagicMock(), fail, "Ip29YJPUXt5MpJG13iLUSw"
    )
    return ex


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


class TestIsRetryable(unittest.TestCase):
    def test_deadline_exceeded(self):
        # The failure seen in production: Google accepted the request and ran
        # out of its own deadline. Repeating it is what the API asks for.
        self.assertTrue(_is_retryable(failure(DEADLINE_EXCEEDED)))

    def test_transient_error(self):
        self.assertTrue(_is_retryable(failure(TRANSIENT_ERROR)))

    def test_other_internal_error(self):
        self.assertFalse(_is_retryable(failure(INTERNAL_ERROR)))

    def test_error_of_another_kind(self):
        # An unset oneof reads as UNSPECIFIED rather than raising, so a failure
        # of a different kind must not be mistaken for an internal one.
        self.assertFalse(_is_retryable(failure(NOT_PERMITTED)))

    def test_mixed_failure_is_not_retried(self):
        # The permanent half would be rejected again; retrying only postpones
        # the refusal.
        self.assertFalse(
            _is_retryable(failure(DEADLINE_EXCEEDED, NOT_PERMITTED))
        )

    def test_empty_failure(self):
        self.assertFalse(_is_retryable(failure()))


def service_raising(*outcomes):
    """Builds a service whose calls yield the given outcomes in order.

    Exceptions are raised, everything else is returned as the response the
    caller iterates over.
    """
    service = MagicMock()
    service.generate_keyword_ideas.side_effect = list(outcomes)
    return service


def ideas(*texts):
    """Builds the idea results a successful call would stream back."""
    results = []
    for text in texts:
        idea = GenerateKeywordIdeaResult()
        idea.text = text
        results.append(idea)
    return results


class TestGenerateIdeas(unittest.TestCase):
    def setUp(self):
        sleep = patch("ads_mcp.tools.keyword_planning.time.sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def test_returns_ideas_without_retrying(self):
        service = service_raising(ideas("окна пвх"))

        rows = _generate_ideas(service, GenerateKeywordIdeasRequest(), None)

        self.assertEqual([row["text"] for row in rows], ["окна пвх"])
        self.assertEqual(service.generate_keyword_ideas.call_count, 1)
        self.sleep.assert_not_called()

    def test_retries_a_transient_failure_and_succeeds(self):
        service = service_raising(
            exception(failure(DEADLINE_EXCEEDED)), ideas("окна пвх минск")
        )

        rows = _generate_ideas(service, GenerateKeywordIdeasRequest(), None)

        self.assertEqual([row["text"] for row in rows], ["окна пвх минск"])
        self.assertEqual(service.generate_keyword_ideas.call_count, 2)
        self.sleep.assert_called_once_with(2.0)

    def test_gives_up_after_the_last_attempt(self):
        service = service_raising(*[exception(failure(DEADLINE_EXCEEDED))] * 3)

        with self.assertRaises(ToolError) as caught:
            _generate_ideas(service, GenerateKeywordIdeasRequest(), None)

        # The wording the caller sees must not change just because we tried
        # more than once.
        self.assertIn(
            "Request ID: Ip29YJPUXt5MpJG13iLUSw", str(caught.exception)
        )
        self.assertIn("Google Ads API Error: boom", str(caught.exception))
        self.assertEqual(service.generate_keyword_ideas.call_count, 3)
        self.assertEqual(
            [call.args[0] for call in self.sleep.call_args_list], [2.0, 4.0]
        )

    def test_does_not_retry_a_permanent_failure(self):
        service = service_raising(exception(failure(NOT_PERMITTED)))

        with self.assertRaises(ToolError):
            _generate_ideas(service, GenerateKeywordIdeasRequest(), None)

        self.assertEqual(service.generate_keyword_ideas.call_count, 1)
        self.sleep.assert_not_called()

    def test_a_retry_does_not_append_to_a_half_read_result(self):
        # The response is a lazy pager, so a failure can arrive after some
        # ideas were already read. Those must not survive into the retry.
        def half_read():
            yield ideas("первая")[0]
            raise exception(failure(DEADLINE_EXCEEDED))

        service = service_raising(half_read(), ideas("первая", "вторая"))

        rows = _generate_ideas(service, GenerateKeywordIdeasRequest(), None)

        self.assertEqual([row["text"] for row in rows], ["первая", "вторая"])

    def test_limit_truncates_the_result(self):
        service = service_raising(ideas("одна", "две", "три"))

        rows = _generate_ideas(service, GenerateKeywordIdeasRequest(), 2)

        self.assertEqual([row["text"] for row in rows], ["одна", "две"])


if __name__ == "__main__":
    unittest.main()
