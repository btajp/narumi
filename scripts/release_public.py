"""Anonymous, bounded HTTPS downloads for the actual Sparkle distribution surface."""

from __future__ import annotations

import hashlib
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from release_artifacts import ReleaseError, require

# Exact hosts only. GitHub redirects public release assets to these CDN endpoints.
PUBLIC_HOSTS = frozenset(
    {
        "github.com",
        "release-assets.githubusercontent.com",
        "objects.githubusercontent.com",
        "github-releases.githubusercontent.com",
    }
)
PUBLIC_HEADERS = {
    "User-Agent": "narumi-release-verify",
    "Accept": "application/octet-stream",
    "Cache-Control": "no-cache",
}


def validate_public_url(url: str) -> None:
    require(isinstance(url, str), "公開 URL が不正です")
    require(
        not any(c.isspace() or ord(c) < 32 for c in url) and "\\" not in url,
        "公開 URL に不正な文字があります",
    )
    try:
        parts = urllib.parse.urlsplit(url)
        valid = (
            parts.scheme == "https"
            and parts.hostname in PUBLIC_HOSTS
            and parts.port in (None, 443)
            and parts.username is None
            and parts.password is None
            and not parts.fragment
        )
    except ValueError as exc:
        raise ReleaseError("公開 URL が不正です") from exc
    require(valid, "公開 URL / redirect の送信先が許可範囲外です")


class PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not carry headers, userinfo, cookies, or authentication across redirects."""

    max_redirections = 5
    max_repeats = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        require(
            req.get_method() == "GET" and code in (301, 302, 303, 307, 308),
            "公開 URL の redirect が不正です",
        )
        validate_public_url(newurl)
        return urllib.request.Request(newurl, headers=PUBLIC_HEADERS, method="GET")


def download_public(url: str, target: Path, expected: dict) -> None:
    """Never consult gh, netrc, cookies, environment proxies, or an installed global opener."""
    validate_public_url(url)
    size = expected["size"]
    require(type(size) is int and size > 0, "公開 asset の期待長が不正です")
    # A dedicated opener has no auth/cookie handlers. An empty ProxyHandler also prevents
    # ambient HTTPS_PROXY or macOS proxy credentials from entering this anonymous check.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        PublicRedirectHandler(),
    )
    request = urllib.request.Request(url, headers=PUBLIC_HEADERS, method="GET")
    digest = hashlib.sha256()
    total = 0
    deadline = time.monotonic() + 300
    try:
        with opener.open(request, timeout=30) as response, target.open("xb") as stream:
            validate_public_url(response.geturl())
            require(response.getcode() == 200, "公開 asset を匿名取得できません")
            content_length = response.headers.get("Content-Length")
            require(
                content_length in (None, str(size)), "公開 asset の Content-Length が不一致です"
            )
            require(
                response.headers.get("Content-Encoding", "identity") == "identity",
                "公開 asset の Content-Encoding が不正です",
            )
            while chunk := response.read(min(1024 * 1024, size - total + 1)):
                total += len(chunk)
                require(total <= size, "公開 asset が期待長を超えています")
                require(time.monotonic() < deadline, "公開 asset の匿名取得が時間切れです")
                digest.update(chunk)
                stream.write(chunk)
    except urllib.error.HTTPError as exc:
        raise ReleaseError(f"公開 URL を匿名取得できません（HTTP {exc.code}）") from None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ReleaseError("公開 URL の匿名取得に失敗しました") from exc
    require(
        total == size and digest.hexdigest() == expected["sha256"],
        "匿名取得した公開 asset の SHA256 / 長さが不一致です",
    )
