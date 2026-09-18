import httpx

from track_insight.content.contracts import QualityStatus
from track_insight.content.fetcher import PageFetcher, _validate_public_http_url
from track_insight.content.html_text import extract_html_text
from track_insight.content.quality import assess_text_quality
from track_insight.core.errors import PageFetchError


def test_html_extraction_removes_common_shell_and_reads_publish_date() -> None:
    html = b"""<!doctype html>
    <html><head><title>Policy title</title>
    <meta property="article:published_time" content="2025-03-04T09:30:00+08:00">
    </head><body><nav>Navigation noise</nav>
    <article><h1>Policy title</h1><p>Manufacturing policy facts and support measures.</p></article>
    <footer>Footer noise</footer></body></html>"""

    parsed = extract_html_text(html, "text/html; charset=utf-8")

    assert parsed["title"] == "Policy title"
    assert parsed["published_at"].isoformat() == "2025-03-04T09:30:00+08:00"
    assert "Manufacturing policy facts" in parsed["content_text"]
    assert "Navigation noise" not in parsed["content_text"]
    assert "Footer noise" not in parsed["content_text"]


def test_html_extraction_handles_nested_boilerplate_nodes() -> None:
    html = b"""<html><body><div class="footer"><span>nested</span></div>
    <article><h1>Useful</h1><p>This substantive paragraph must survive extraction.</p></article>
    </body></html>"""

    parsed = extract_html_text(html, "text/html; charset=utf-8")

    assert "substantive paragraph" in parsed["content_text"]
    assert "nested" not in parsed["content_text"]


def test_quality_gate_routes_empty_html_and_pdf_to_the_right_fallback() -> None:
    html_status, html_reasons = assess_text_quality("", min_chars=500)
    pdf_status, pdf_reasons = assess_text_quality("", min_chars=500, pdf=True)

    assert html_status == QualityStatus.REMOTE_READER_REQUIRED
    assert pdf_status == QualityStatus.OCR_REQUIRED
    assert html_reasons == pdf_reasons == ["empty_body"]


def test_fetcher_rejects_non_public_destinations() -> None:
    for url in ("file:///etc/passwd", "http://127.0.0.1/private", "http://localhost/"):
        try:
            _validate_public_http_url(url)
        except PageFetchError as error:
            assert error.code in {"invalid_url", "unsafe_url"}
        else:
            raise AssertionError(f"unsafe URL was accepted: {url}")


def test_fetcher_uses_proxy_dns_for_proxy_routed_host(monkeypatch) -> None:
    monkeypatch.setattr(
        "track_insight.content.fetcher.getproxies",
        lambda: {"https": "http://127.0.0.1:7890"},
    )
    monkeypatch.setattr("track_insight.content.fetcher.proxy_bypass", lambda _host: False)

    def fail_local_dns(*_args, **_kwargs):
        raise AssertionError("proxy-routed hostname should not use local DNS validation")

    monkeypatch.setattr("track_insight.content.fetcher.socket.getaddrinfo", fail_local_dns)
    _validate_public_http_url("https://public.example/policy")


def test_fetcher_keeps_dns_guard_for_no_proxy_hosts(monkeypatch) -> None:
    monkeypatch.setattr(
        "track_insight.content.fetcher.getproxies",
        lambda: {"https": "http://127.0.0.1:7890"},
    )
    monkeypatch.setattr("track_insight.content.fetcher.proxy_bypass", lambda _host: True)
    monkeypatch.setattr(
        "track_insight.content.fetcher.socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("127.0.0.1", 443)),
        ],
    )

    try:
        _validate_public_http_url("https://direct.example/policy")
    except PageFetchError as error:
        assert error.code == "unsafe_url"
    else:
        raise AssertionError("NO_PROXY host resolving locally should be rejected")


def test_fetcher_recognizes_pdf_when_server_uses_generic_content_type(monkeypatch) -> None:
    monkeypatch.setattr(
        "track_insight.content.fetcher._validate_public_http_url", lambda _url: None
    )
    fetcher = PageFetcher(timeout_seconds=2, max_retries=0, max_response_bytes=1024)
    fetcher.client.close()
    fetcher.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "application/octet-stream"},
                content=b"%PDF-1.7\nminimal pdf body",
            )
        )
    )
    try:
        result = fetcher.fetch("https://public.example/policy")
    finally:
        fetcher.close()

    assert result.content_type == "application/pdf"
    assert result.content.startswith(b"%PDF-")
