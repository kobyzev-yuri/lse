"""
Fetch and parse earnings report / call source pages into plain text.

Hybrid ingest v0: requests + BeautifulSoup HTML extraction.
PDF is saved when downloaded but text extraction is deferred (parse_error note).
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from services.http_outbound import outbound_session

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; LSE-EarningsMaterialBot/1.0; +https://github.com/kobyzev-yuri/lse)"
)
MIN_PARSED_TEXT_CHARS = 400
LINK_KEYWORDS = (
    "transcript",
    "earnings",
    "press-release",
    "press release",
    "presentation",
    "slides",
    "webcast",
    "call",
    "results",
    "financial-results",
    "financial results",
    ".pdf",
)


@dataclass(frozen=True)
class FetchResult:
    url: str
    final_url: str
    content_type: str
    content: bytes
    status_code: int


@dataclass(frozen=True)
class ParseResult:
    text: str
    method: str
    discovered_links: tuple[str, ...]
    content_type: str
    final_url: str
    content_sha256: str
    raw_ext: str
    parse_error: str | None = None


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def fetch_url(url: str, *, timeout_sec: int = 45) -> FetchResult:
    sess = outbound_session("EARNINGS_MATERIAL_USE_SYSTEM_PROXY")
    resp = sess.get(
        url,
        timeout=timeout_sec,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
        },
        allow_redirects=True,
    )
    resp.raise_for_status()
    content_type = (resp.headers.get("Content-Type") or "application/octet-stream").split(";")[0].strip().lower()
    return FetchResult(
        url=url,
        final_url=str(resp.url),
        content_type=content_type,
        content=resp.content,
        status_code=int(resp.status_code),
    )


def _guess_ext(content_type: str, url: str) -> str:
    if "pdf" in content_type:
        return "pdf"
    if "html" in content_type or "xml" in content_type:
        return "html"
    path = urlparse(url).path.lower()
    if path.endswith(".pdf"):
        return "pdf"
    if path.endswith(".html") or path.endswith(".htm") or path.endswith(".aspx"):
        return "html"
    return "bin"


def _clean_soup(soup: BeautifulSoup) -> None:
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "header", "footer", "nav", "form"]):
        tag.decompose()


def _candidate_article_nodes(soup: BeautifulSoup) -> Iterable:
    selectors = (
        "article",
        "main",
        '[role="main"]',
        ".article-body",
        ".article-content",
        ".entry-content",
        ".post-content",
        ".transcript",
        ".earnings-transcript",
        ".module_body",
        ".event-details",
        ".financial-results",
        "#main-content",
        "#content",
    )
    for sel in selectors:
        for node in soup.select(sel):
            yield node
    yield soup.body or soup


def extract_text_from_html(content: bytes, *, base_url: str) -> tuple[str, tuple[str, ...]]:
    html = content.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    _clean_soup(soup)

    best_text = ""
    for node in _candidate_article_nodes(soup):
        chunk = normalize_text(node.get_text("\n", strip=True))
        if len(chunk) > len(best_text):
            best_text = chunk
    if not best_text:
        best_text = normalize_text(soup.get_text("\n", strip=True))

    links: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        abs_url = urljoin(base_url, href)
        label = " ".join(
            part.strip()
            for part in (
                str(a.get_text(" ", strip=True) or ""),
                href,
                str(a.get("title") or ""),
            )
            if part
        ).lower()
        if not any(k in label for k in LINK_KEYWORDS):
            continue
        if abs_url in seen:
            continue
        seen.add(abs_url)
        links.append(abs_url)

    return best_text, tuple(links)


def parse_fetched_content(fetch: FetchResult) -> ParseResult:
    digest = sha256_hex(fetch.content)
    ext = _guess_ext(fetch.content_type, fetch.final_url)

    if "pdf" in fetch.content_type or ext == "pdf":
        return ParseResult(
            text="",
            method="pdf_saved_only_v0",
            discovered_links=(),
            content_type=fetch.content_type,
            final_url=fetch.final_url,
            content_sha256=digest,
            raw_ext=ext,
            parse_error="pdf_text_extraction_not_supported_v0",
        )

    if "html" in fetch.content_type or ext == "html":
        text, links = extract_text_from_html(fetch.content, base_url=fetch.final_url)
        method = "html_bs4_lxml"
        parse_error = None
        if len(text) < MIN_PARSED_TEXT_CHARS:
            parse_error = f"short_text:{len(text)}"
        return ParseResult(
            text=text,
            method=method,
            discovered_links=links,
            content_type=fetch.content_type,
            final_url=fetch.final_url,
            content_sha256=digest,
            raw_ext=ext,
            parse_error=parse_error,
        )

    return ParseResult(
        text="",
        method="unsupported_content_type",
        discovered_links=(),
        content_type=fetch.content_type,
        final_url=fetch.final_url,
        content_sha256=digest,
        raw_ext=ext,
        parse_error=f"unsupported_content_type:{fetch.content_type}",
    )


def storage_path(base_dir: Path, *, symbol: str, material_id: int, digest: str, ext: str) -> Path:
    sym = symbol.strip().upper()
    sub = base_dir / sym / digest[:2]
    sub.mkdir(parents=True, exist_ok=True)
    return sub / f"{material_id}_{digest[:16]}.{ext}"


def fetch_and_parse(url: str, *, timeout_sec: int = 45) -> ParseResult:
    fetched = fetch_url(url, timeout_sec=timeout_sec)
    return parse_fetched_content(fetched)


def save_raw_copy(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
