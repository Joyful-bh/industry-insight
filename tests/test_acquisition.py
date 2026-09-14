import pytest

from track_insight.acquisition import (
    CollectionError,
    FetchResponse,
    canonicalize_url,
    extract_html_link_records,
)


def test_extract_html_link_records_supports_scripted_government_paging() -> None:
    response = FetchResponse(
        url="https://example.gov.cn/zcwj/index.html",
        status_code=200,
        headers={"content-type": "text/html; charset=utf-8"},
        content=(
            '<a title="下一页" tagname="/zcwj/list-id-2.html" '
            'onclick="query(this,\'/zcwj/list-id-2.html\')">下一页</a>'
        ).encode(),
    )

    records = extract_html_link_records(response)

    assert [(record.href, record.title) for record in records] == [
        ("/zcwj/list-id-2.html", "下一页")
    ]


def test_canonicalize_url_removes_fragment_and_sorts_query() -> None:
    assert (
        canonicalize_url("HTTPS://Example.COM/path?b=2&a=1#section")
        == "https://example.com/path?a=1&b=2"
    )


def test_canonicalize_url_rejects_non_http_urls() -> None:
    with pytest.raises(CollectionError, match="HTTP"):
        canonicalize_url("file:///private/document.pdf")


def test_canonicalize_url_percent_encodes_unicode_path() -> None:
    assert canonicalize_url("https://example.com/产业‑动态.html") == (
        "https://example.com/%E4%BA%A7%E4%B8%9A%E2%80%91%E5%8A%A8%E6%80%81.html"
    )
