"""Canonical earnings material URLs for MVP / priority tickers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class CatalogMaterial:
    symbol: str
    material_type: str
    source_url: str
    source_name: str
    title: str
    event_date: date | None = None
    fiscal_period: str | None = None
    meta: dict | None = None


# Priority tickers for manual catalog URLs (auto SEC/Fool covers the rest of universe).
PRIORITY_SYMBOLS: tuple[str, ...] = (
    "META",
    "NVDA",
    "ASML",
    "ARM",
    "SNDK",
    "MSFT",
    "MU",
    "AMD",
    "AMZN",
    "LITE",
    "CIEN",
    "TER",
    "INTC",
    "ORCL",
    "ALAB",
    "NBIS",
)

CATALOG_MATERIALS: tuple[CatalogMaterial, ...] = (
    # META Q1 2026 — IR pages are JS shells; use PR/transcript mirrors.
    CatalogMaterial(
        symbol="META",
        event_date=date(2026, 4, 29),
        fiscal_period="Q1 2026",
        material_type="ir_event_page",
        source_name="Meta Investor Relations",
        source_url="https://investor.atmeta.com/investor-events/event-details/2026/Q1-2026-Earnings-Call/default.aspx",
        title="Meta Q1 2026 earnings call event page",
        meta={"mvp_case": "META capex -> AI infrastructure peers", "note": "JS shell; use linked PDFs / mirrors"},
    ),
    CatalogMaterial(
        symbol="META",
        event_date=date(2026, 4, 29),
        fiscal_period="Q1 2026",
        material_type="press_release",
        source_name="PR Newswire",
        source_url="https://www.prnewswire.com/news-releases/meta-reports-first-quarter-2026-results-302757852.html",
        title="Meta Q1 2026 press release",
        meta={"mvp_case": "META capex -> AI infrastructure peers", "mirror_of": "meta_ir_press_release"},
    ),
    CatalogMaterial(
        symbol="META",
        event_date=date(2026, 4, 29),
        fiscal_period="Q1 2026",
        material_type="third_party_transcript",
        source_name="The Motley Fool",
        source_url="https://www.fool.com/earnings/call-transcripts/2026/04/29/meta-meta-q1-2026-earnings-call-transcript/",
        title="Meta Q1 2026 earnings call transcript",
        meta={"mvp_case": "META capex -> AI infrastructure peers"},
    ),
    CatalogMaterial(
        symbol="META",
        event_date=date(2026, 4, 29),
        fiscal_period="Q1 2026",
        material_type="transcript",
        source_name="Meta IR CDN",
        source_url="https://s21.q4cdn.com/399680738/files/doc_financials/2026/q1/META-Q1-2026-Earnings-Call-Transcript.pdf",
        title="Meta Q1 2026 earnings call transcript PDF",
        meta={"mvp_case": "META capex -> AI infrastructure peers", "primary_source": True},
    ),
    CatalogMaterial(
        symbol="META",
        event_date=date(2026, 4, 29),
        fiscal_period="Q1 2026",
        material_type="presentation",
        source_name="Meta IR CDN",
        source_url="https://s21.q4cdn.com/399680738/files/doc_financials/2026/q1/Earnings-Presentation-Q1-2026.pdf",
        title="Meta Q1 2026 earnings presentation PDF",
        meta={"mvp_case": "META capex -> AI infrastructure peers", "primary_source": True},
    ),
    # NVDA Q1 FY2027 — reported 2026-05-20
    CatalogMaterial(
        symbol="NVDA",
        event_date=date(2026, 5, 20),
        fiscal_period="Q1 FY2027",
        material_type="press_release",
        source_name="NVIDIA Newsroom",
        source_url="https://nvidianews.nvidia.com/news/nvidia-announces-financial-results-for-first-quarter-fiscal-2027",
        title="NVIDIA Q1 FY2027 press release",
        meta={"mvp_case": "NVDA earnings -> AI basket", "primary_source": True},
    ),
    CatalogMaterial(
        symbol="NVDA",
        event_date=date(2026, 5, 20),
        fiscal_period="Q1 FY2027",
        material_type="press_release",
        source_name="NVIDIA Investor Relations",
        source_url="https://investor.nvidia.com/news/press-release-details/2026/NVIDIA-Announces-Financial-Results-for-First-Quarter-Fiscal-2027/default.aspx",
        title="NVIDIA Q1 FY2027 press release (IR page)",
        meta={"mvp_case": "NVDA earnings -> AI basket", "mirror_of": "nvidia_newsroom"},
    ),
    CatalogMaterial(
        symbol="NVDA",
        event_date=date(2026, 5, 20),
        fiscal_period="Q1 FY2027",
        material_type="third_party_transcript",
        source_name="The Motley Fool",
        source_url="https://www.fool.com/earnings/call-transcripts/2026/05/20/nvidia-nvda-q1-2027-earnings-transcript/",
        title="NVIDIA Q1 FY2027 earnings call transcript",
        meta={"mvp_case": "NVDA earnings -> AI basket"},
    ),
    CatalogMaterial(
        symbol="NVDA",
        event_date=date(2026, 5, 20),
        fiscal_period="Q1 FY2027",
        material_type="transcript",
        source_name="NVIDIA IR CDN",
        source_url="https://s201.q4cdn.com/141608511/files/doc_financials/2027/q1/NVDA-Q1-2027-Earnings-Call-20-May-2026-5_00-PM-ET.pdf",
        title="NVIDIA Q1 FY2027 earnings call transcript PDF",
        meta={"mvp_case": "NVDA earnings -> AI basket", "primary_source": True},
    ),
    CatalogMaterial(
        symbol="NVDA",
        event_date=date(2026, 5, 20),
        fiscal_period="Q1 FY2027",
        material_type="presentation",
        source_name="NVIDIA IR CDN",
        source_url="https://s201.q4cdn.com/141608511/files/doc_financials/2027/Q127/NVDA-F1Q27-Quarterly-Presentation-FINAL.pdf",
        title="NVIDIA Q1 FY2027 investor presentation PDF",
        meta={"mvp_case": "NVDA earnings -> AI basket", "primary_source": True},
    ),
    CatalogMaterial(
        symbol="NVDA",
        event_date=date(2026, 5, 20),
        fiscal_period="Q1 FY2027",
        material_type="other",
        source_name="NVIDIA IR CDN",
        source_url="https://s201.q4cdn.com/141608511/files/doc_financials/2027/Q127/Q1FY27-CFO-Commentary.pdf",
        title="NVIDIA Q1 FY2027 CFO commentary PDF",
        meta={"mvp_case": "NVDA earnings -> AI basket", "subtype": "cfo_commentary", "primary_source": True},
    ),
    CatalogMaterial(
        symbol="NVDA",
        event_date=date(2026, 5, 20),
        fiscal_period="Q1 FY2027",
        material_type="sec_filing",
        source_name="SEC EDGAR",
        source_url="https://www.sec.gov/Archives/edgar/data/1045810/000104581026000051/q1fy27pr.htm",
        title="NVIDIA Q1 FY2027 earnings press release (SEC 8-K)",
        meta={"mvp_case": "NVDA earnings -> AI basket", "primary_source": True},
    ),
    CatalogMaterial(
        symbol="NVDA",
        event_date=date(2026, 5, 20),
        fiscal_period="Q1 FY2027",
        material_type="ir_event_page",
        source_name="NVIDIA Investor Relations",
        source_url="https://investor.nvidia.com/news/press-releases",
        title="NVIDIA IR press releases hub",
        meta={"mvp_case": "NVDA earnings -> AI basket", "note": "discovery hub"},
    ),
    # Existing MVP cases
    CatalogMaterial(
        symbol="ASML",
        event_date=date(2026, 4, 15),
        fiscal_period="Q1 2026",
        material_type="ir_event_page",
        source_name="ASML Investor Relations",
        source_url="https://www.asml.com/en/investors/financial-results/q1-2026",
        title="ASML Q1 2026 financial results",
        meta={"mvp_case": "ASML pullback/rebound reference case"},
    ),
    CatalogMaterial(
        symbol="ARM",
        event_date=date(2026, 5, 6),
        fiscal_period=None,
        material_type="ir_event_page",
        source_name="Arm Investor Relations",
        source_url="https://investors.arm.com/financials/quarterly-annual-results",
        title="Arm quarterly and annual results page",
        meta={"mvp_case": "ARM cross-earnings into AI/chip peers"},
    ),
    CatalogMaterial(
        symbol="SNDK",
        event_date=date(2026, 4, 30),
        fiscal_period="Q3 2026",
        material_type="third_party_transcript",
        source_name="The Motley Fool",
        source_url="https://www.fool.com/earnings/call-transcripts/2026/04/30/sandisk-sndk-q3-2026-earnings-transcript/",
        title="SanDisk Q3 2026 earnings transcript",
        meta={"mvp_case": "SNDK demand-dominant follow-through reference case"},
    ),
    # ALAB Q1 2026 — reported 2026-05-05
    CatalogMaterial(
        symbol="ALAB",
        event_date=date(2026, 5, 5),
        fiscal_period="Q1 2026",
        material_type="press_release",
        source_name="GlobeNewswire",
        source_url="https://www.globenewswire.com/news-release/2026/05/05/3288259/0/en/Astera-Labs-Reports-First-Quarter-2026-Financial-Results.html",
        title="Astera Labs Q1 2026 press release",
        meta={"primary_source": True},
    ),
    CatalogMaterial(
        symbol="ALAB",
        event_date=date(2026, 5, 5),
        fiscal_period="Q1 2026",
        material_type="third_party_transcript",
        source_name="The Motley Fool",
        source_url="https://www.fool.com/earnings/call-transcripts/2026/05/05/astera-labs-alab-q1-2026-earnings-transcript/",
        title="Astera Labs Q1 2026 earnings call transcript",
    ),
)


def catalog_for_event(symbol: str, event_date: date | None) -> tuple[CatalogMaterial, ...]:
    sym = symbol.strip().upper()
    if event_date is None:
        return tuple(m for m in CATALOG_MATERIALS if m.symbol == sym and m.event_date is None)
    return tuple(m for m in CATALOG_MATERIALS if m.symbol == sym and m.event_date == event_date)


def catalog_for_symbol(symbol: str) -> tuple[CatalogMaterial, ...]:
    sym = symbol.strip().upper()
    return tuple(m for m in CATALOG_MATERIALS if m.symbol == sym)


def priority_catalog() -> tuple[CatalogMaterial, ...]:
    return tuple(m for m in CATALOG_MATERIALS if m.symbol in PRIORITY_SYMBOLS)
