import json
import re
from datetime import UTC, datetime

from bs4 import BeautifulSoup
from bs4.element import Comment

_BOILERPLATE = re.compile(
    r"(?:^|[-_\s])(nav|menu|breadcrumb|footer|sidebar|share|toolbar|related|login|comment)(?:$|[-_\s])",
    re.IGNORECASE,
)
_DATE_META_KEYS = {
    "article:published_time",
    "date",
    "datepublished",
    "pubdate",
    "publishdate",
    "publish_time",
    "pub_time",
    "发布日期",
}


def extract_html_text(raw: bytes, content_type: str | None = None) -> dict:
    charset = None
    if content_type:
        match = re.search(r"charset=([\w.-]+)", content_type, re.IGNORECASE)
        if match:
            charset = match.group(1).strip("\"'")
    try:
        html = raw.decode(charset or "utf-8", errors="replace")
    except LookupError:
        html = raw.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    title = _clean(soup.title.get_text(" ", strip=True)) if soup.title else None
    published_at = _published_at(soup)

    for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()
    for tag in soup.find_all(
        ["script", "style", "noscript", "svg", "iframe", "form", "nav", "footer"]
    ):
        tag.decompose()
    # Walk from leaves to roots. Decomposing a parent first clears the
    # attributes of its already-enumerated children in BeautifulSoup.
    for tag in reversed(soup.find_all(True)):
        if tag.attrs is None:
            continue
        marker = " ".join([str(tag.get("id", "")), *[str(value) for value in tag.get("class", [])]])
        if _BOILERPLATE.search(marker):
            tag.decompose()

    root = soup.find("article") or soup.find("main") or soup.body or soup
    text = _clean(root.get_text("\n", strip=True))
    return {"title": title, "published_at": published_at, "content_text": text}


def _published_at(soup: BeautifulSoup) -> datetime | None:
    candidates: list[str] = []
    for tag in soup.find_all("meta"):
        key = str(tag.get("property") or tag.get("name") or tag.get("itemprop") or "").lower()
        if key in _DATE_META_KEYS or "publish" in key or "published" in key:
            content = tag.get("content")
            if content:
                candidates.append(str(content))
    for tag in soup.find_all("time"):
        value = tag.get("datetime") or tag.get_text(" ", strip=True)
        if value:
            candidates.append(str(value))
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or tag.get_text())
        except (TypeError, ValueError):
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict):
                value = item.get("datePublished") or item.get("dateCreated")
                if value:
                    candidates.append(str(value))
    for candidate in candidates:
        parsed = _parse_datetime(candidate)
        if parsed:
            return parsed
    return None


def _parse_datetime(value: str) -> datetime | None:
    normalized = value.strip().replace("年", "-").replace("月", "-").replace("日", "")
    normalized = re.sub(r"\s+", "T", normalized, count=1) if " " in normalized else normalized
    try:
        result = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        for pattern in ("%Y-%m-%d", "%Y%m%d", "%Y-%m", "%Y"):
            try:
                result = datetime.strptime(normalized[:10], pattern)
                break
            except ValueError:
                result = None
        if result is None:
            return None
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result


def _clean(value: str) -> str:
    value = value.replace("\xa0", " ").replace("\u200b", "")
    lines = [re.sub(r"\s+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line)
