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
    PeerGraphEdge("SNDK", "LITE", "ai_infra_supply", 0.60),
    PeerGraphEdge("SNDK", "NBIS", "ai_infra_peer", 0.55),
    # MSFT hyperscaler capex / cloud AI
    PeerGraphEdge("MSFT", "NVDA", "ai_infra_customer", 0.85),
    PeerGraphEdge("MSFT", "AMD", "ai_infra_peer", 0.70),
    PeerGraphEdge("MSFT", "MU", "ai_infra_supply", 0.75),
    PeerGraphEdge("MSFT", "LITE", "ai_infra_supply", 0.60),
    PeerGraphEdge("MSFT", "ORCL", "enterprise_peer", 0.55),
    PeerGraphEdge("MSFT", "META", "megacap_peer", 0.65),
    PeerGraphEdge("MSFT", "AMZN", "cloud_peer", 0.70),
    # AMZN AWS / capex
    PeerGraphEdge("AMZN", "NVDA", "ai_infra_customer", 0.80),
    PeerGraphEdge("AMZN", "AMD", "ai_infra_peer", 0.65),
    PeerGraphEdge("AMZN", "MU", "ai_infra_supply", 0.70),
    PeerGraphEdge("AMZN", "MSFT", "cloud_peer", 0.70),
    PeerGraphEdge("AMZN", "ORCL", "enterprise_peer", 0.50),
    # AMD compute vs NVDA
    PeerGraphEdge("AMD", "NVDA", "ai_infra_peer", 0.90),
    PeerGraphEdge("AMD", "MU", "ai_infra_supply", 0.80),
    PeerGraphEdge("AMD", "ASML", "ai_infra_supply", 0.65),
    PeerGraphEdge("AMD", "LITE", "ai_infra_supply", 0.55),
    PeerGraphEdge("AMD", "TER", "semi_equipment", 0.50),
    PeerGraphEdge("AMD", "INTC", "cpu_peer", 0.75),
    # MU memory cycle
    PeerGraphEdge("MU", "NVDA", "ai_infra_customer", 0.85),
    PeerGraphEdge("MU", "SNDK", "memory_peer", 0.80),
    PeerGraphEdge("MU", "AMD", "ai_infra_customer", 0.70),
    PeerGraphEdge("MU", "LITE", "ai_infra_supply", 0.55),
    # TER semi test / equipment
    PeerGraphEdge("TER", "NVDA", "semi_equipment", 0.60),
    PeerGraphEdge("TER", "AMD", "semi_equipment", 0.55),
    PeerGraphEdge("TER", "INTC", "semi_equipment", 0.50),
    PeerGraphEdge("TER", "ASML", "semi_equipment", 0.65),
    # ALAB connectivity / AI networking
    PeerGraphEdge("ALAB", "NVDA", "ai_infra_supply", 0.70),
    PeerGraphEdge("ALAB", "AMD", "ai_infra_supply", 0.65),
    PeerGraphEdge("ALAB", "META", "ai_infra_customer", 0.55),
    # LITE / CIEN networking
    PeerGraphEdge("LITE", "NVDA", "ai_infra_supply", 0.60),
    PeerGraphEdge("LITE", "META", "ai_infra_customer", 0.55),
    PeerGraphEdge("CIEN", "LITE", "networking_peer", 0.65),
    PeerGraphEdge("CIEN", "META", "ai_infra_customer", 0.50),
    # INTC turnaround / foundry
    PeerGraphEdge("INTC", "AMD", "cpu_peer", 0.75),
    PeerGraphEdge("INTC", "NVDA", "ai_infra_peer", 0.55),
    PeerGraphEdge("INTC", "TER", "semi_equipment", 0.50),
    # ORCL enterprise cloud
    PeerGraphEdge("ORCL", "MSFT", "enterprise_peer", 0.55),
    PeerGraphEdge("ORCL", "NVDA", "ai_infra_customer", 0.60),
    # NBIS AI infra
    PeerGraphEdge("NBIS", "NVDA", "ai_infra_peer", 0.70),
    PeerGraphEdge("NBIS", "SNDK", "ai_infra_peer", 0.60),
)


def edges_for_source(source_ticker: str) -> tuple[PeerGraphEdge, ...]:
    sym = source_ticker.strip().upper()
    return tuple(e for e in PEER_GRAPH_EDGES if e.source_ticker == sym)
