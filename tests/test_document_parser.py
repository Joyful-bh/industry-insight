from datetime import UTC, datetime

from track_insight.document_parser import normalize_text, parse_html, parse_pdf


def test_html_parser_extracts_main_content_and_metadata() -> None:
    html = """
    <html><head><title>页面标题</title></head><body>
      <nav>栏目导航</nav>
      <article>
        <h1>关于支持中小企业数字化改造的通知</h1>
        <p>发布时间：2025年6月3日</p>
        <p>京经信发〔2025〕12号</p>
        <p>支持中小企业开展数字化改造，提升经营效率。</p>
      </article>
      <footer>网站页脚</footer>
    </body></html>
    """.encode()

    result = parse_html(html)

    assert result.title == "关于支持中小企业数字化改造的通知"
    assert result.published_at == datetime(2025, 6, 3, tzinfo=UTC)
    assert result.document_number == "京经信发〔2025〕12号"
    assert "支持中小企业开展数字化改造" in result.text
    assert "栏目导航" not in result.text
    assert "网站页脚" not in result.text


def test_normalize_text_removes_empty_lines_and_extra_spaces() -> None:
    assert normalize_text(" 第一行  \n\n 第 二 行 \xa0 ") == "第一行\n第 二 行"


def test_normalize_text_removes_database_unsupported_control_characters() -> None:
    assert normalize_text("\x00第一行\x07\n第二行\x1f") == "第一行\n第二行"


def test_html_parser_removes_common_navigation_and_trailing_boilerplate() -> None:
    html = """
    <html><head><title>产业项目动态</title></head><body>
      <div class="breadcrumb">首页 &gt; 产业动态</div>
      <article>
        <h1>产业项目动态</h1>
        <div class="toolbar"><a>打印</a><a>收藏</a><a>分享</a></div>
        <p>某企业新建数字化生产线，项目已经正式投产，并将扩大生产规模。</p>
        <div class="related-news">相关推荐内容</div>
        <p>版权所有：某网站</p>
      </article>
      <div class="footer-menu">联系我们 网站地图</div>
    </body></html>
    """.encode()

    result = parse_html(html)

    assert result.text.startswith("产业项目动态")
    assert "正式投产" in result.text
    assert "打印" not in result.text
    assert "相关推荐" not in result.text
    assert "版权所有" not in result.text
    assert "联系我们" not in result.text


def test_html_parser_removes_inline_controls_but_keeps_normal_share_word() -> None:
    body = "企业代表分享了数字化改造经验，并介绍了新生产线的建设进展。"
    html = (
        "<html><head><title>产业动态</title></head><body><article>"
        f"<h1>产业动态</h1><p>来源：某单位 打印 【字体：大 中 小】 分享：X</p><p>{body}</p>"
        "</article></body></html>"
    ).encode()

    result = parse_html(html)

    assert "字体：" not in result.text
    assert "分享：X" not in result.text
    assert "分享了数字化改造经验" in result.text


def test_html_parser_treats_bodyless_fragment_as_partial_content() -> None:
    result = parse_html(b"<title>fragment</title><p>short text</p>")

    assert result.text == "fragment short text"
    assert result.warnings == ["insufficient_text"]


def test_pdf_parser_treats_invalid_file_as_partial_content() -> None:
    result = parse_pdf(b"not a PDF")

    assert result.text == ""
    assert "invalid_pdf" in result.warnings


def test_html_parser_falls_back_to_body_when_declared_main_is_empty() -> None:
    html = """
    <html><head><title>制造业项目动态</title></head><body>
      <main></main>
      <div class="page-detail">
        <h1>制造业项目动态</h1>
        <p>某制造企业建设智能生产线，形成新的工业软件、边缘计算和设备运维需求。</p>
      </div>
    </body></html>
    """.encode()

    result = parse_html(html)

    assert "建设智能生产线" in result.text
