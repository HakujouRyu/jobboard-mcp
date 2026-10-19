"""ATS adapters, keyed by ATS."""

from jobboard_mcp.adapters.ashby import AshbyAdapter
from jobboard_mcp.adapters.base import Adapter, BoardListing, PostingRef
from jobboard_mcp.adapters.greenhouse import GreenhouseAdapter
from jobboard_mcp.adapters.lever import LeverAdapter
from jobboard_mcp.adapters.oracle import OracleAdapter
from jobboard_mcp.adapters.personio import PersonioAdapter
from jobboard_mcp.adapters.rippling import RipplingAdapter
from jobboard_mcp.adapters.workable import WorkableAdapter
from jobboard_mcp.adapters.workday import WorkdayAdapter
from jobboard_mcp.models import Ats

ADAPTERS: dict[Ats, Adapter] = {
    Ats.GREENHOUSE: GreenhouseAdapter(),
    Ats.ASHBY: AshbyAdapter(),
    Ats.LEVER: LeverAdapter(),
    Ats.WORKDAY: WorkdayAdapter(),
    Ats.ORACLE: OracleAdapter(),
    Ats.WORKABLE: WorkableAdapter(),
    Ats.RIPPLING: RipplingAdapter(),
    Ats.PERSONIO: PersonioAdapter(),
}


def parse_posting_url(url: str) -> PostingRef | None:
    for adapter in ADAPTERS.values():
        ref = adapter.parse_url(url)
        if ref is not None:
            return ref
    return None


__all__ = ["ADAPTERS", "Adapter", "BoardListing", "PostingRef", "parse_posting_url"]
