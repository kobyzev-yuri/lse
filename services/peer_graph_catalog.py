"""Canonical peer / spillover edges for earnings intelligence MVP."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class PeerGraphEdge:
    source_ticker: str
    target_ticker: str
    relation_type: str
    weight: float
    valid_from: date | None = None
    meta: dict | None = None


# MVP: AI infra / chips spillover from hyperscaler and GPU leaders.
PEER_GRAPH_EDGES: tuple[PeerGraphEdge, ...] = (
    # META capex → infra / memory / networking
    PeerGraphEdge("META", "MU", "ai_infra_supply", 0.85, meta={"mvp_case": "META capex -> memory"}),
    PeerGraphEdge("META", "SNDK", "ai_infra_supply", 0.75, meta={"mvp_case": "META capex -> storage"}),
    PeerGraphEdge("META", "AMD", "ai_infra_peer", 0.70, meta={"mvp_case": "META capex -> compute peers"}),
    PeerGraphEdge("META", "LITE", "ai_infra_supply", 0.65, meta={"mvp_case": "META capex -> networking"}),
    PeerGraphEdge("META", "INTC", "ai_infra_peer", 0.50),
    PeerGraphEdge("META", "NVDA", "ai_infra_peer", 0.80),
    PeerGraphEdge("META", "ASML", "ai_infra_supply", 0.60),
    PeerGraphEdge("META", "ARM", "ai_infra_peer", 0.55),
    # NVDA earnings → AI basket
    PeerGraphEdge("NVDA", "MU", "ai_infra_supply", 0.90, meta={"mvp_case": "NVDA demand -> HBM/memory"}),
    PeerGraphEdge("NVDA", "AMD", "ai_infra_peer", 0.85),
    PeerGraphEdge("NVDA", "SNDK", "ai_infra_supply", 0.70),
    PeerGraphEdge("NVDA", "LITE", "ai_infra_supply", 0.65),
    PeerGraphEdge("NVDA", "INTC", "ai_infra_peer", 0.55),
    PeerGraphEdge("NVDA", "ASML", "ai_infra_supply", 0.75),
    PeerGraphEdge("NVDA", "ARM", "ai_infra_peer", 0.70),
    PeerGraphEdge("NVDA", "META", "ai_infra_customer", 0.80),
    PeerGraphEdge("NVDA", "MSFT", "ai_infra_customer", 0.75),
    PeerGraphEdge("NVDA", "SMH", "sector_etf", 0.85),
    PeerGraphEdge("NVDA", "SOXX", "sector_etf", 0.85),
    # ASML equipment cycle
    PeerGraphEdge("ASML", "NVDA", "ai_infra_customer", 0.65),
    PeerGraphEdge("ASML", "AMD", "ai_infra_customer", 0.60),
    PeerGraphEdge("ASML", "INTC", "ai_infra_customer", 0.55),
    # ARM IP → chip ecosystem
    PeerGraphEdge("ARM", "NVDA", "ai_infra_peer", 0.60),
    PeerGraphEdge("ARM", "AMD", "ai_infra_peer", 0.65),
    PeerGraphEdge("ARM", "QCOM", "ai_infra_peer", 0.55),
    # SNDK memory demand
    PeerGraphEdge("SNDK", "MU", "ai_infra_peer", 0.80),
    PeerGraphEdge("SNDK", "NVDA", "ai_infra_customer", 0.65),
)


def edges_for_source(source_ticker: str) -> tuple[PeerGraphEdge, ...]:
    sym = source_ticker.strip().upper()
    return tuple(e for e in PEER_GRAPH_EDGES if e.source_ticker == sym)
