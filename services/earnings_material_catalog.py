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
    "TSM",
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
    # NBIS Q1 2026 — Fool exists; autoprep often skips Fool when 429 cooldown (use catalog).
    CatalogMaterial(
        symbol="NBIS",
        event_date=date(2026, 5, 13),
        fiscal_period="Q1 2026",
        material_type="third_party_transcript",
        source_name="The Motley Fool",
        source_url="https://www.fool.com/earnings/call-transcripts/2026/05/13/nebius-nbis-q1-2026-earnings-transcript/",
        title="Nebius Q1 2026 earnings call transcript",
        meta={"primary_source": True},
    ),
    # AMD Q4 2025 — reported 2026-02-03 (catalog bypasses Fool 429 cooldown).
    CatalogMaterial(
        symbol="AMD",
        event_date=date(2026, 2, 3),
        fiscal_period="Q4 2025",
        material_type="third_party_transcript",
        source_name="The Motley Fool",
        source_url="https://www.fool.com/earnings/call-transcripts/2026/02/03/amd-amd-q4-2025-earnings-call-transcript/",
        title="AMD Q4 2025 earnings call transcript",
        meta={"primary_source": True},
    ),
    CatalogMaterial(
        symbol="AMD",
        event_date=date(2026, 2, 3),
        fiscal_period="Q4 2025",
        material_type="press_release",
        source_name="AMD IR",
        source_url="https://ir.amd.com/news-events/press-releases/detail/1276/amd-reports-fourth-quarter-and-full-year-2025-financial-results",
        title="AMD Q4 2025 press release",
        meta={"primary_source": True},
    ),
    CatalogMaterial(
        symbol="AMD",
        event_date=date(2026, 2, 3),
        fiscal_period="Q4 2025",
        material_type="transcript",
        source_name="AMD IR",
        source_url="https://d1io3yog0oux5.cloudfront.net/_3ec85d8ec6db18d4cf4c06efc11ab71a/amd/db/841/9223/webcast_transcript/AMD+Fiscal+Fourth+Quarter+and+Full+Year+2025+Financial+Results.pdf",
        title="AMD Q4 2025 earnings call transcript PDF",
        meta={"primary_source": True},
    ),
    # NVDA Q4 FY2026 — reported 2026-02-25.
    CatalogMaterial(
        symbol="NVDA",
        event_date=date(2026, 2, 25),
        fiscal_period="Q4 FY2026",
        material_type="third_party_transcript",
        source_name="The Motley Fool",
        source_url="https://www.fool.com/earnings/call-transcripts/2026/02/25/nvidia-nvda-q4-2026-earnings-call-transcript/",
        title="NVDA Q4 FY2026 earnings call transcript",
        meta={"primary_source": True},
    ),
    CatalogMaterial(
        symbol="NVDA",
        event_date=date(2026, 2, 25),
        fiscal_period="Q4 FY2026",
        material_type="press_release",
        source_name="NVIDIA Newsroom",
        source_url="https://nvidianews.nvidia.com/news/nvidia-announces-financial-results-for-fourth-quarter-and-fiscal-2026",
        title="NVDA Q4 FY2026 press release",
        meta={"primary_source": True},
    ),
    # NBIS Q4 2025 — reported 2026-02-12.
    CatalogMaterial(
        symbol="NBIS",
        event_date=date(2026, 2, 12),
        fiscal_period="Q4 2025",
        material_type="press_release",
        source_name="Nebius IR",
        source_url="https://nebius.com/newsroom/nebius-reports-fourth-quarter-and-full-year-2025-financial-results",
        title="Nebius Q4 2025 press release",
        meta={"primary_source": True},
    ),
    # AMD Q1 2026 — report 2026-05-05; Fool published 2026-05-06 (day-window probe).
    CatalogMaterial(
        symbol="AMD",
        event_date=date(2026, 5, 5),
        fiscal_period="Q1 2026",
        material_type="third_party_transcript",
        source_name="The Motley Fool",
        source_url="https://www.fool.com/earnings/call-transcripts/2026/05/06/amd-amd-q1-2026-earnings-call-transcript/",
        title="AMD Q1 2026 earnings call transcript",
    ),
    CatalogMaterial(
        symbol="AMD",
        event_date=date(2026, 5, 5),
        fiscal_period="Q1 2026",
        material_type="press_release",
        source_name="AMD IR",
        source_url="https://ir.amd.com/news-events/press-releases/detail/1284/amd-reports-first-quarter-2026-financial-results",
        title="AMD Q1 2026 press release",
    ),
    # AMZN / GOOGL / ANET / DELL — Fool in catalog (no probe); SEC ex99 via auto after URL fix.
    CatalogMaterial(
        symbol="AMZN",
        event_date=date(2026, 4, 29),
        fiscal_period="Q1 2026",
        material_type="third_party_transcript",
        source_name="The Motley Fool",
        source_url="https://www.fool.com/earnings/call-transcripts/2026/04/29/amazon-amzn-q1-2026-earnings-call-transcript/",
        title="Amazon Q1 2026 earnings call transcript",
        meta={"primary_source": True},
    ),
    CatalogMaterial(
        symbol="GOOGL",
        event_date=date(2026, 4, 29),
        fiscal_period="Q1 2026",
        material_type="third_party_transcript",
        source_name="The Motley Fool",
        source_url="https://www.fool.com/earnings/call-transcripts/2026/04/29/alphabet-googl-q1-2026-earnings-call-transcript/",
        title="Alphabet Q1 2026 earnings call transcript",
        meta={"primary_source": True},
    ),
    CatalogMaterial(
        symbol="ANET",
        event_date=date(2026, 5, 5),
        fiscal_period="Q1 2026",
        material_type="third_party_transcript",
        source_name="The Motley Fool",
        source_url="https://www.fool.com/earnings/call-transcripts/2026/05/05/arista-anet-q1-2026-earnings-transcript/",
        title="Arista Q1 2026 earnings call transcript",
        meta={"primary_source": True},
    ),
    CatalogMaterial(
        symbol="DELL",
        event_date=date(2026, 2, 26),
        fiscal_period="FY2026 Q4",
        material_type="third_party_transcript",
        source_name="The Motley Fool",
        source_url="https://www.fool.com/earnings/call-transcripts/2026/02/26/dell-dell-q4-2026-earnings-call-transcript/",
        title="Dell Q4 FY2026 earnings call transcript",
        meta={"primary_source": True},
    ),
    # ALAB Q4 2025 — reported 2026-02-10 (Fool blocked by cooldown; catalog bypasses probe).
    CatalogMaterial(
        symbol="ALAB",
        event_date=date(2026, 2, 10),
        fiscal_period="Q4 2025",
        material_type="third_party_transcript",
        source_name="The Motley Fool",
        source_url="https://www.fool.com/earnings/call-transcripts/2026/02/10/astera-labs-alab-q4-2025-earnings-transcript/",
        title="Astera Labs Q4 2025 earnings call transcript",
        meta={"primary_source": True},
    ),
    # ARM Q3 FY2026 — reported 2026-02-04
    CatalogMaterial(
        symbol="ARM",
        event_date=date(2026, 2, 4),
        fiscal_period="Q3 FY2026",
        material_type="third_party_transcript",
        source_name="The Motley Fool",
        source_url="https://www.fool.com/earnings/call-transcripts/2026/02/04/arm-holdings-arm-q3-2026-earnings-transcript/",
        title="Arm Q3 FY2026 earnings call transcript",
        meta={"primary_source": True},
    ),
    CatalogMaterial(
        symbol="ARM",
        event_date=date(2026, 2, 4),
        fiscal_period="Q3 FY2026",
        material_type="transcript",
        source_name="Arm IR",
        source_url="https://investors.arm.com/static-files/ba30dca7-d2dd-4d44-b4c5-9fd953fedaea",
        title="Arm Q3 FY2026 earnings call transcript PDF",
    ),
    # SNDK Q2 FY2026 — reported 2026-01-29
    CatalogMaterial(
        symbol="SNDK",
        event_date=date(2026, 1, 29),
        fiscal_period="Q2 FY2026",
        material_type="third_party_transcript",
        source_name="The Motley Fool",
        source_url="https://www.fool.com/earnings/call-transcripts/2026/01/29/sandisk-sndk-q2-2026-earnings-call-transcript/",
        title="SanDisk Q2 FY2026 earnings call transcript",
        meta={"primary_source": True},
    ),
    CatalogMaterial(
        symbol="SNDK",
        event_date=date(2026, 1, 29),
        fiscal_period="Q2 FY2026",
        material_type="press_release",
        source_name="SanDisk IR",
        source_url="https://www.sandisk.com/company/newsroom/press-releases/2026/2026-01-29-sandisk-reports-fiscal-second-quarter-2026-financial-results",
        title="SanDisk Q2 FY2026 press release",
    ),
    # ASML Q4 2025 — reported 2026-01-28 (duplicate yfinance KB row also on 2026-01-27).
    CatalogMaterial(
        symbol="ASML",
        event_date=date(2026, 1, 28),
        fiscal_period="Q4 2025",
        material_type="ir_event_page",
        source_name="ASML Investor Relations",
        source_url="https://www.asml.com/investors/financial-results/q4-2025",
        title="ASML Q4 2025 financial results",
        meta={"mvp_case": "ASML Q4 2025 reference case"},
    ),
    CatalogMaterial(
        symbol="ASML",
        event_date=date(2026, 1, 28),
        fiscal_period="Q4 2025",
        material_type="press_release",
        source_name="SEC",
        source_url="https://www.sec.gov/Archives/edgar/data/937966/000162828026003701/pressreleasefinancialresul.htm",
        title="ASML Q4 2025 press release (SEC)",
    ),
    CatalogMaterial(
        symbol="ASML",
        event_date=date(2026, 1, 27),
        fiscal_period="Q4 2025",
        material_type="ir_event_page",
        source_name="ASML Investor Relations",
        source_url="https://www.asml.com/investors/financial-results/q4-2025",
        title="ASML Q4 2025 financial results",
        meta={"note": "yfinance duplicate KB date; same release as 2026-01-28"},
    ),
    # TSM Q4 2025 — reported 2026-01-15 (ADR 6-K + TSMC IR).
    CatalogMaterial(
        symbol="TSM",
        event_date=date(2026, 1, 15),
        fiscal_period="Q4 2025",
        material_type="ir_event_page",
        source_name="TSMC Investor Relations",
        source_url="https://investor.tsmc.com/english/quarterly-results/2025/q4",
        title="TSMC Q4 2025 quarterly results",
        meta={"mvp_case": "TSM semis anchor; foreign issuer 6-K"},
    ),
    # TSM Q1 2026 — reported 2026-04-15 (yfinance duplicate also on 2026-04-16).
    CatalogMaterial(
        symbol="TSM",
        event_date=date(2026, 4, 15),
        fiscal_period="Q1 2026",
        material_type="ir_event_page",
        source_name="TSMC Investor Relations",
        source_url="https://investor.tsmc.com/english/quarterly-results/2026/q1",
        title="TSMC Q1 2026 quarterly results",
        meta={"mvp_case": "TSM semis anchor; foreign issuer 6-K"},
    ),
    CatalogMaterial(
        symbol="TSM",
        event_date=date(2026, 4, 16),
        fiscal_period="Q1 2026",
        material_type="ir_event_page",
        source_name="TSMC Investor Relations",
        source_url="https://investor.tsmc.com/english/quarterly-results/2026/q1",
        title="TSMC Q1 2026 quarterly results",
        meta={"note": "yfinance duplicate KB date; same release as 2026-04-15"},
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
