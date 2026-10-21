import ipaddress
import socket
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit
from urllib.request import getproxies, proxy_bypass

import httpx

from track_insight.core.errors import PageFetchError

_USER_AGENT = "TrackInsightPOC/0.1 (+public policy and industry research)"


@dataclass(frozen=True)
class HttpFetchResult:
    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    content: bytes
    headers: dict[str, str]


class PageFetcher:
    def __init__(
        self,
        *,
        timeout_seconds: int,
        max_retries: int,
        max_response_bytes: int,
        min_interval_per_host_seconds: float = 1.0,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.max_response_bytes = max_response_bytes
        self.min_interval_per_host_seconds = min_interval_per_host_seconds
        self._last_request_at: dict[str, float] = {}
        self.client = httpx.Client(
            follow_redirects=False,
            timeout=timeout_seconds,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.1",
            },
        )

    def fetch(self, url: str) -> HttpFetchResult:
        requested_url = url
        current_url = url
        for redirect_count in range(6):
            _validate_public_http_url(current_url)
            status_code, headers, content = self._request(current_url)
            if status_code in {301, 302, 303, 307, 308}:
                location = headers.get("location")
                if not location:
                    raise PageFetchError(
                        "redirect response has no Location", code="invalid_redirect"
                    )
                if redirect_count >= 5:
                    raise PageFetchError("too many redirects", code="too_many_redirects")
                current_url = urljoin(current_url, location)
                continue
            if status_code in {401, 403}:
                raise PageFetchError(
                    f"HTTP {status_code}; remote extraction may be required",
                    code="http_forbidden",
                )
            if status_code == 429 or status_code >= 500:
                raise PageFetchError(
                    f"HTTP {status_code}",
                    code=f"http_{status_code}",
                    retryable=True,
                )
            if status_code >= 400:
                raise PageFetchError(f"HTTP {status_code}", code=f"http_{status_code}")
            raw_content_type = headers.get("content-type", "")
            content_type = raw_content_type.split(";", 1)[0].strip().lower()
            if content_type not in {"text/html", "application/xhtml+xml", "application/pdf"}:
                if b"%PDF-" in content[:1024]:
                    content_type = "application/pdf"
            if content_type not in {"text/html", "application/xhtml+xml", "application/pdf"}:
                raise PageFetchError(
                    f"unsupported content type: {content_type or 'missing'}",
                    code="unsupported_content_type",
                )
            return HttpFetchResult(
                requested_url=requested_url,
                final_url=current_url,
                status_code=status_code,
                content_type=content_type,
                content=content,
                headers={
                    key.lower(): value
                    for key, value in headers.items()
                    if key.lower() in {"content-type", "last-modified", "etag", "content-language"}
                },
            )
        raise PageFetchError("redirect limit reached", code="too_many_redirects")

    def _request(self, url: str) -> tuple[int, dict[str, str], bytes]:
        hostname = urlsplit(url).hostname or ""
        for attempt in range(self.max_retries + 1):
            try:
                self._respect_host_interval(hostname)
                with self.client.stream("GET", url) as response:
                    status_code = response.status_code
                    headers = {key.lower(): value for key, value in response.headers.items()}
                    if status_code in {301, 302, 303, 307, 308, 401, 403}:
                        return status_code, headers, b""
                    if status_code == 429 or status_code >= 500:
                        if attempt < self.max_retries:
                            time.sleep(min(0.5 * (2**attempt), 2))
                            continue
                        return status_code, headers, b""
                    content_length = response.headers.get("content-length")
                    try:
                        declared_size = int(content_length) if content_length else None
                    except ValueError:
                        declared_size = None
                    if declared_size is not None and declared_size > self.max_response_bytes:
                        raise PageFetchError(
                            "response exceeds configured size limit", code="response_too_large"
                        )
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > self.max_response_bytes:
                            raise PageFetchError(
                                "response exceeds configured size limit", code="response_too_large"
                            )
                        chunks.append(chunk)
                    return status_code, headers, b"".join(chunks)
            except PageFetchError as error:
                if error.retryable and attempt < self.max_retries:
                    time.sleep(min(0.5 * (2**attempt), 2))
                    continue
                raise
            except httpx.RequestError as error:
                if attempt < self.max_retries:
                    time.sleep(min(0.5 * (2**attempt), 2))
                    continue
                raise PageFetchError(str(error), code="network_error", retryable=True) from error
        raise PageFetchError("request retry limit reached", code="network_error", retryable=True)

    def _respect_host_interval(self, hostname: str) -> None:
        now = time.monotonic()
        previous = self._last_request_at.get(hostname)
        if previous is not None:
            delay = self.min_interval_per_host_seconds - (now - previous)
            if delay > 0:
                time.sleep(delay)
        self._last_request_at[hostname] = time.monotonic()

    def close(self) -> None:
        self.client.close()


def _validate_public_http_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise PageFetchError("only absolute HTTP(S) URLs are accepted", code="invalid_url")
    if parsed.username or parsed.password:
        raise PageFetchError("URLs containing credentials are rejected", code="invalid_url")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".localhost"):
        raise PageFetchError("local hostnames are rejected", code="unsafe_url")
    try:
        addresses = {ipaddress.ip_address(hostname)}
    except ValueError:
        # When an HTTP proxy is in use, local DNS may return proxy-specific
        # synthetic addresses. Let the proxy resolve the hostname instead.
        # Hosts excluded by NO_PROXY still use the direct-resolution checks.
        proxies = getproxies()
        if (proxies.get(parsed.scheme) or proxies.get("all")) and not proxy_bypass(hostname):
            return
        try:
            addresses = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(
                    hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
                )
            }
        except OSError as error:
            raise PageFetchError(
                "host name could not be resolved", code="dns_error", retryable=True
            ) from error
    if not addresses or any(not address.is_global for address in addresses):
        raise PageFetchError("non-public network destinations are rejected", code="unsafe_url")
