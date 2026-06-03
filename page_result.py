"""Shared domain types used across crawler, scorer, and detector packages."""
from typing import NamedTuple


class PageResult(NamedTuple):
    """One fetched page returned by the depth crawler.

    Fields match the positional order of the legacy bare-tuple so all existing
    index-based access (page[0], page[2], etc.) and tuple-unpacking in loops
    continue to work unchanged after the upgrade.
    """

    url: str
    html: str
    http_status: int
    js_rendered: bool
