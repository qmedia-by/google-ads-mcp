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

"""Tools for keyword planning.

This module is the fork's only addition to the upstream server. It exists
because `KeywordPlanIdeaService` is not reachable through GAQL: the `search`
tool queries resources, while generating keyword ideas is a separate RPC. See
FORK.md.
"""

from typing import Any, Dict, List

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

import ads_mcp.utils as utils

from google.ads.googleads.errors import GoogleAdsException
from google.ads.googleads.v24.enums.types.keyword_plan_network import (
    KeywordPlanNetworkEnum,
)
from google.ads.googleads.v24.services.types.keyword_plan_idea_service import (
    GenerateKeywordIdeasRequest,
)

planning_mcp = FastMCP("planning")

# The API rejects requests carrying more than ten geo target constants.
_MAX_GEO_TARGET_CONSTANTS = 10

_NETWORKS = {
    "GOOGLE_SEARCH": KeywordPlanNetworkEnum.KeywordPlanNetwork.GOOGLE_SEARCH,
    "GOOGLE_SEARCH_AND_PARTNERS": (
        KeywordPlanNetworkEnum.KeywordPlanNetwork.GOOGLE_SEARCH_AND_PARTNERS
    ),
}


def _resource_name(value: str, collection: str) -> str:
    """Accepts either a bare id or a full resource name, returns the latter."""
    value = str(value).strip()
    if not value:
        raise ToolError(f"Empty {collection} value")
    if value.startswith(f"{collection}/"):
        return value
    if not value.isdigit():
        raise ToolError(
            f"Expected a numeric id or a '{collection}/<id>' resource name, "
            f"got '{value}'"
        )
    return f"{collection}/{value}"


def _optional(message: Any, field: str) -> Any:
    """Returns a field's value, or None when the API did not report one.

    Absent and zero are different facts here: a phrase with no volume data is
    not a phrase with zero searches, and collapsing the two silently invents
    demand figures.
    """
    try:
        if not message._pb.HasField(field):
            return None
    except ValueError:
        # Not an optional field; fall through and read it directly.
        pass
    return getattr(message, field)


def _build_request(
    customer_id: str,
    language: str,
    geo_target_constants: List[str],
    keywords: List[str],
    page_url: str,
    keyword_plan_network: str,
    include_adult_keywords: bool,
) -> GenerateKeywordIdeasRequest:
    """Validates inputs and assembles the API request."""
    if not keywords and not page_url:
        raise ToolError(
            "Provide keywords, page_url, or both — there is nothing to seed "
            "the ideas with otherwise."
        )

    if len(geo_target_constants) > _MAX_GEO_TARGET_CONSTANTS:
        raise ToolError(
            f"At most {_MAX_GEO_TARGET_CONSTANTS} geo target constants are "
            f"allowed, got {len(geo_target_constants)}."
        )

    if keyword_plan_network not in _NETWORKS:
        raise ToolError(
            f"keyword_plan_network must be one of "
            f"{', '.join(sorted(_NETWORKS))}, got '{keyword_plan_network}'."
        )

    request = GenerateKeywordIdeasRequest()
    request.customer_id = customer_id
    request.language = _resource_name(language, "languageConstants")
    request.geo_target_constants.extend(
        _resource_name(geo, "geoTargetConstants")
        for geo in geo_target_constants
    )
    request.include_adult_keywords = include_adult_keywords
    request.keyword_plan_network = _NETWORKS[keyword_plan_network]

    if keywords and page_url:
        request.keyword_and_url_seed.url = page_url
        request.keyword_and_url_seed.keywords.extend(keywords)
    elif keywords:
        request.keyword_seed.keywords.extend(keywords)
    else:
        request.url_seed.url = page_url

    return request


def _format_idea(idea: Any) -> Dict[str, Any]:
    """Flattens one idea into the row shape the caller consumes."""
    metrics = idea.keyword_idea_metrics
    return {
        "text": idea.text,
        "avg_monthly_searches": _optional(metrics, "avg_monthly_searches"),
        "competition": metrics.competition.name,
        "competition_index": _optional(metrics, "competition_index"),
        "low_top_of_page_bid_micros": _optional(
            metrics, "low_top_of_page_bid_micros"
        ),
        "high_top_of_page_bid_micros": _optional(
            metrics, "high_top_of_page_bid_micros"
        ),
    }


@planning_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def generate_keyword_ideas(
    customer_id: str,
    language: str,
    geo_target_constants: List[str],
    keywords: List[str] = [],
    page_url: str = "",
    keyword_plan_network: str = "GOOGLE_SEARCH",
    include_adult_keywords: bool = False,
    limit: int | None = None,
) -> List[Dict[str, Any]]:
    """Generates keyword ideas with demand metrics for a market.

    Use to build or extend a keyword core. This is the only way to obtain new
    keyword ideas and search volumes: they are not queryable through GAQL, so
    the `search` tool cannot return them. Use `search` on `keyword_view` or
    `search_term_view` instead when you need what an account already runs.

    Requires a developer token with the "Researching keywords and
    recommendations" permissible use. Explorer access level cannot call this.

    Args:
        customer_id: The id of the customer, digits only, no hyphens.
        language: Language constant, e.g. '1031' or 'languageConstants/1031'.
        geo_target_constants: Up to 10 geo target constants, e.g. ['2112'].
        keywords: Seed phrases. Either this or page_url is required.
        page_url: Seed page. Combined with keywords when both are given.
        keyword_plan_network: GOOGLE_SEARCH or GOOGLE_SEARCH_AND_PARTNERS.
        include_adult_keywords: Whether adult keywords may be returned.
        limit: Maximum number of ideas to return.

    Returns:
        A list of ideas. `avg_monthly_searches` and the bid fields are null
        when the API reported no data — which is not the same as zero. Bid
        values are in micros: divide by 1,000,000 for the account currency.
    """
    request = _build_request(
        customer_id=customer_id,
        language=language,
        geo_target_constants=geo_target_constants,
        keywords=keywords,
        page_url=page_url,
        keyword_plan_network=keyword_plan_network,
        include_adult_keywords=include_adult_keywords,
    )

    utils.logger.info(
        "ads_mcp.generate_keyword_ideas customer=%s geo=%s language=%s",
        customer_id,
        list(request.geo_target_constants),
        request.language,
    )

    service = utils.get_googleads_service("KeywordPlanIdeaService")

    try:
        response = service.generate_keyword_ideas(request=request)

        ideas: List[Dict[str, Any]] = []
        for idea in response:
            ideas.append(_format_idea(idea))
            if limit and len(ideas) >= limit:
                break
        return ideas
    except GoogleAdsException as ex:
        error_msgs = [
            f"Google Ads API Error: {error.message}"
            for error in ex.failure.errors
        ]
        raise ToolError(
            f"Request ID: {ex.request_id}\n" + "\n".join(error_msgs)
        )
