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

"""Reports what the Registry sheet looks like, without repeating any of it.

Fork addition, see FORK.md. Run it against the real sheet to find out how much
of the Yandex Direct column this deployment can actually publish, and which
rows somebody has to go and tidy:

    GOOGLE_ADS_REGISTRY_SHEET_ID=... \\
    GOOGLE_ADS_REGISTRY_SA_KEY=... \\
    .venv/bin/python -m scripts.audit_registry_columns

**Nothing this prints comes out of a cell.** Column headings, row numbers,
project names and counts, and for a dropped Direct cell the category of what
was wrong with it — never a substring of the cell itself. That restraint is the
point: the question being answered is "how many rows hold a password", and an
answer that quoted them to find out would be self-defeating. Read the output
anywhere; paste it anywhere.

The one thing it does need is the service account key, so run it where that
already lives — the same machine that runs `setup-registry-access.sh`.
"""

import collections
import sys
from typing import Dict, List, Sequence, Set, Tuple

from ads_mcp import registry


def _fetch_tabs() -> List[Tuple[str, List[List[str]]]]:
    """Reads every tab of the file, named, through the server's own helpers.

    Deliberately not a second implementation of the API calls: an auditor that
    reached Sheets its own way could report a file the server never sees.

    It does differ from the server in one way, on purpose — it reads *every*
    tab, including the ones `GOOGLE_ADS_REGISTRY_TABS` leaves out. Knowing what
    is in the tabs nobody reads is most of the reason to run this.
    """
    httpx = registry._require("httpx", "makes the request to the Sheets API")

    sheet_id = registry._sheet_id()
    token = registry._access_token(registry._credentials())
    titles = registry._tab_titles(httpx, sheet_id, token)
    if not titles:
        return []

    payload = registry._get(
        httpx,
        registry._BATCH_VALUES_ENDPOINT.format(sheet_id=sheet_id),
        token,
        {
            "ranges": [registry._quote_tab(title) for title in titles],
            "majorDimension": "ROWS",
        },
        "every tab",
    )
    values = payload.get("valueRanges") or []
    return [
        (title, value.get("values") or [])
        for title, value in zip(titles, values)
    ]


def _report_tabs(
    tabs: Sequence[Tuple[str, Sequence[Sequence[str]]]], configured: Set[str]
) -> None:
    """Prints every tab, whether the server reads it, and what it holds.

    Headings are labels rather than data, and they are the fastest way to see
    that a column has been renamed out from under the parser — or that the
    sheet has grown one worth reading, a Metrika counter say. Which tabs are
    read is the other half: a tab nobody configured is invisible to the server
    however good its columns are.
    """
    print("Tabs\n")
    for title, rows in tabs:
        mark = "read" if title in configured else "NOT READ"
        print(f"  {title!r} — {len(rows)} non-empty rows — {mark}")
        for row in rows:
            columns = registry._read_header(row)
            if columns is None:
                continue
            headings = ", ".join(str(c) for c in row if str(c).strip())
            recognised = (
                ", ".join(sorted(columns.providers)) or "none — block skipped"
            )
            print(f"      headings  : {headings}")
            print(f"      recognised: {recognised}")
            break
        try:
            clients, _ = registry.parse(rows)
            counts = ", ".join(
                f"{provider} у {sum(1 for c in clients if provider in c.accounts)}"
                for provider in registry.PROVIDERS
            )
            print(f"      would give: {len(clients)} Clients — {counts}")
        except registry.RegistryUnavailable:
            print("      would give: no Client with a readable Account")
        print()


def _report_direct(
    tabs: Sequence[Tuple[str, Sequence[Sequence[str]]]], configured: Set[str]
) -> None:
    """Counts what the Direct column would and would not publish, per tab.

    Counted across every tab, not only the configured ones. A tab nobody reads
    still says how much work its rows would be worth if somebody did.
    """
    published: List[str] = []
    dropped: Dict[str, List[str]] = collections.defaultdict(list)
    blank = 0

    for title, rows in tabs:
        columns = None
        for offset, row in enumerate(rows, start=1):
            header = registry._read_header(row)
            if header is not None:
                columns = (
                    header if "yandex_direct" in header.providers else None
                )
                continue
            if columns is None:
                continue

            name = registry._clean_project(registry._cell(row, columns.name))
            cell = registry._cell(row, columns.providers["yandex_direct"])
            if not name or registry._is_blank(cell):
                blank += 1
                continue

            mark = "" if title in configured else " — tab NOT READ"
            if registry._extract_yandex_direct(cell):
                published.append(name)
            else:
                dropped[registry._why_direct_was_dropped(cell)].append(
                    f"row {offset} on {title!r} ({name}){mark}"
                )

    total_dropped = sum(len(rows_) for rows_ in dropped.values())
    print("Yandex Direct column, across every tab\n")
    print(f"  published as logins : {len(published)}")
    print(f"  dropped             : {total_dropped}")
    print(f"  empty               : {blank}\n")

    for reason, where in sorted(dropped.items()):
        print(f"  {len(where)} × {reason}")
        for entry in where:
            print(f"      {entry}")
        print()


def main() -> int:
    try:
        tabs = _fetch_tabs()
    except registry.RegistryUnavailable as error:
        print(f"Could not read the Registry: {error}", file=sys.stderr)
        return 1

    configured = set(registry.configured_tabs())
    if not configured and tabs:
        # Unset means the server reads whichever tab is first, and saying so
        # matters more than it looks: on this file that was hiding two thirds
        # of the Clients behind a setting nobody had noticed was a setting.
        configured = {tabs[0][0]}
        print(
            f"\n{registry.TABS_ENV_VAR} is not set, so the server reads only "
            f"the first tab, {tabs[0][0]!r}.\n"
        )

    print(f"Read {len(tabs)} tabs.\n")
    _report_tabs(tabs, configured)
    _report_direct(tabs, configured)
    print(
        "Every dropped row is one edit in the sheet: leave the login alone in\n"
        "the Direct cell, in lower case, and keep the password in the access\n"
        "block. Nothing from a dropped cell reached this output, the server's\n"
        "snapshot, Redis or any agent."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
