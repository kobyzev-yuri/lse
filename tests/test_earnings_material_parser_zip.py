import io
import zipfile

from services.earnings_material_parser import FetchResult, parse_fetched_content


def test_parse_zip_with_embedded_html():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "results.html",
            "<html><body><p>" + ("Earnings results paragraph. " * 40) + "</p></body></html>",
        )
    content = buf.getvalue()
    fetch = FetchResult(
        url="https://example.com/package.zip",
        final_url="https://example.com/package.zip",
        content_type="application/zip",
        content=content,
        status_code=200,
    )
    parsed = parse_fetched_content(fetch)
    assert parsed.method == "zip_unpack"
    assert len(parsed.text) >= 400
    assert parsed.parse_error is None
