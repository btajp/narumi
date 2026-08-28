"""Anonymous release downloads are exercised without networking or real credentials."""

import hashlib
import importlib.util
import io
import sys
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_helper(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


with patch.dict(sys.modules, {"release_artifacts": _load_helper("release_artifacts")}):
    public = _load_helper("release_public")

URL = "https://github.com/btajp/narumi/releases/download/v1.2.3/narumi-1.2.3.zip"
CDN = "https://release-assets.githubusercontent.com/github-production-release-asset/fixture.zip"
BODY = b"synthetic narumi release asset"


def _expected(content: bytes = BODY) -> dict:
    return {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def _forbidden(*args, **kwargs):
    pytest.fail("A global opener, proxy discovery, or real network operation was attempted")


def _assert_anonymous(request: urllib.request.Request) -> None:
    headers = {name.lower(): value for name, value in request.header_items()}
    assert headers == {name.lower(): value for name, value in public.PUBLIC_HEADERS.items()}
    assert not {"authorization", "cookie", "proxy-authorization"} & headers.keys()
    assert request.unredirected_hdrs == {}
    assert request.get_method() == "GET"
    assert request.data is None


class FakeResponse:
    def __init__(self, body: bytes = BODY):
        self.content = io.BytesIO(body)
        self.url = CDN
        self.status = 200
        self.headers = {"Content-Length": str(len(body))}
        self.read_sizes: list[int] = []
        self.read_error: Exception | None = None
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.closed = True
        self.content.close()

    def geturl(self):
        return self.url

    def getcode(self):
        return self.status

    def read(self, size: int):
        self.read_sizes.append(size)
        if self.read_error is not None:
            raise self.read_error
        return self.content.read(size)


class FakeTransport:
    def __init__(self):
        self.response = FakeResponse()
        self.redirects = [CDN]
        self.handlers: tuple = ()
        self.requests: list[urllib.request.Request] = []
        self.timeout: float | None = None
        self.open_error: Exception | None = None
        self.builds = 0
        self.tls_context = object()

    def build_opener(self, *handlers):
        self.handlers = handlers
        self.builds += 1
        return self

    def open(self, request, timeout):
        self.requests.append(request)
        self.timeout = timeout
        if self.open_error is not None:
            raise self.open_error
        handler = next(h for h in self.handlers if isinstance(h, public.PublicRedirectHandler))
        for url in self.redirects:
            request = handler.redirect_request(request, None, 302, "Found", {}, url)
            self.requests.append(request)
        return self.response


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", _forbidden)
    monkeypatch.setattr(urllib.request, "build_opener", _forbidden)
    monkeypatch.setattr(urllib.request, "install_opener", _forbidden)
    monkeypatch.setattr(urllib.request, "getproxies", _forbidden)
    monkeypatch.setattr(urllib.request, "getproxies_environment", _forbidden)
    monkeypatch.setattr(
        urllib.request, "_opener", type("ForbiddenOpener", (), {"open": _forbidden})()
    )


@pytest.fixture
def transport(no_external_io: None, monkeypatch: pytest.MonkeyPatch) -> FakeTransport:
    fake = FakeTransport()
    monkeypatch.setattr(urllib.request, "build_opener", fake.build_opener)
    monkeypatch.setattr(public.ssl, "create_default_context", lambda: fake.tls_context)
    return fake


def test_public_github_to_cdn_download_is_anonymous_and_exact(
    transport: FakeTransport, tmp_path: Path
) -> None:
    target = tmp_path / "download.zip"
    public.download_public(URL, target, _expected())
    assert target.read_bytes() == BODY
    assert transport.builds == 1
    assert transport.timeout == 30
    assert [request.full_url for request in transport.requests] == [URL, CDN]
    for request in transport.requests:
        _assert_anonymous(request)
    assert transport.response.closed
    assert [type(handler) for handler in transport.handlers] == [
        urllib.request.ProxyHandler,
        urllib.request.HTTPSHandler,
        public.PublicRedirectHandler,
    ]
    assert transport.handlers[0].proxies == {}
    assert transport.handlers[1]._context is transport.tls_context


def test_ambient_tokens_and_proxy_environment_do_not_enter_requests(
    transport: FakeTransport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
        monkeypatch.setenv(name, "fixture-token-never-send")
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.setenv(name, "http://fixture-user:fixture-password@127.0.0.1:8999")
    public.download_public(URL, tmp_path / "download.zip", _expected())
    assert transport.handlers[0].proxies == {}
    for request in transport.requests:
        _assert_anonymous(request)
        assert "fixture-" not in str(request.header_items())
        assert "fixture-" not in request.full_url


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirect_discards_all_inherited_credentials(code: int) -> None:
    request = urllib.request.Request(
        URL,
        headers={
            "Authorization": "Bearer fixture-token",
            "Cookie": "session=fixture-cookie",
            "Proxy-Authorization": "Basic fixture-proxy",
            "X-Private": "fixture-private",
        },
        method="GET",
    )
    request.add_unredirected_header("Authorization", "Bearer fixture-unredirected")
    request.add_unredirected_header("Cookie", "fixture-unredirected-cookie")
    redirected = public.PublicRedirectHandler().redirect_request(
        request, None, code, "redirect", {}, CDN + "?signed=fixture"
    )
    _assert_anonymous(redirected)
    assert redirected.full_url == CDN + "?signed=fixture"
    assert redirected is not request


@pytest.mark.parametrize(
    "url",
    [
        URL,
        CDN,
        "https://objects.githubusercontent.com/fixture.zip",
        "https://github-releases.githubusercontent.com/fixture.zip",
        "https://github.com:443/file.zip",
    ],
)
def test_only_known_https_distribution_hosts_are_accepted(url: str) -> None:
    public.validate_public_url(url)


UNSAFE_URLS = [
    "http://github.com/asset.zip",
    "file:///tmp/asset.zip",
    "ftp://github.com/asset.zip",
    "https://127.0.0.1/asset.zip",
    "https://localhost/asset.zip",
    "https://[::1]/asset.zip",
    "https://192.168.1.1/asset.zip",
    "https://169.254.169.254/asset.zip",
    "https://api.github.com/asset.zip",
    "https://github.com.example.com/asset.zip",
    "https://example.com/asset.zip",
    "https://user@github.com/asset.zip",
    "https://user:password@github.com/asset.zip",
    "https://github.com@127.0.0.1/asset.zip",
    "https://github.com:80/asset.zip",
    "https://github.com:8443/asset.zip",
    "https://github.com:invalid/asset.zip",
    "https://github.com:65536/asset.zip",
    "https://github.com/asset.zip#fragment",
    "https://github.com/asset zip",
    "https://github.com/asset\nzip",
    "https://github.com/asset\x00zip",
    "https://github.com\\@example.com/asset.zip",
    "//github.com/asset.zip",
    "",
]


@pytest.mark.parametrize("url", UNSAFE_URLS)
def test_unsafe_initial_url_is_rejected_before_opening(
    transport: FakeTransport, tmp_path: Path, url: str
) -> None:
    target = tmp_path / "download.zip"
    with pytest.raises(public.ReleaseError):
        public.download_public(url, target, _expected())
    assert transport.builds == 0
    assert transport.requests == []
    assert not target.exists()


@pytest.mark.parametrize("url", UNSAFE_URLS)
def test_unsafe_redirect_is_rejected(url: str) -> None:
    request = urllib.request.Request(URL, method="GET")
    with pytest.raises(public.ReleaseError):
        public.PublicRedirectHandler().redirect_request(request, None, 302, "Found", {}, url)


@pytest.mark.parametrize("method", ["POST", "PUT", "HEAD", "DELETE"])
def test_redirect_rejects_non_get_methods(method: str) -> None:
    request = urllib.request.Request(URL, method=method)
    with pytest.raises(public.ReleaseError):
        public.PublicRedirectHandler().redirect_request(request, None, 302, "Found", {}, CDN)


@pytest.mark.parametrize("code", [200, 300, 304, 305, 401, 404])
def test_redirect_rejects_unsupported_status_codes(code: int) -> None:
    request = urllib.request.Request(URL, method="GET")
    with pytest.raises(public.ReleaseError):
        public.PublicRedirectHandler().redirect_request(request, None, code, "status", {}, CDN)


def test_redirect_count_is_bounded() -> None:
    handler = public.PublicRedirectHandler()
    assert handler.max_redirections == 5
    assert handler.max_repeats == 2


@pytest.mark.parametrize("repeated", [False, True])
def test_redirect_limit_stops_before_opening_another_request(repeated: bool) -> None:
    handler = public.PublicRedirectHandler()
    handler.parent = type("ForbiddenOpener", (), {"open": _forbidden})()
    request = urllib.request.Request(URL, method="GET")
    request.redirect_dict = (
        {CDN: 2} if repeated else {f"{CDN}?attempt={index}": 1 for index in range(5)}
    )
    with pytest.raises(urllib.error.HTTPError, match="redirect"):
        handler.http_error_302(request, FakeResponse(), 302, "Found", {"location": CDN})


@pytest.mark.parametrize("code", [401, 404, 500])
def test_http_failures_do_not_create_a_target(
    transport: FakeTransport, tmp_path: Path, code: int
) -> None:
    target = tmp_path / "download.zip"
    transport.open_error = urllib.error.HTTPError(URL, code, "fixture failure", {}, None)
    with pytest.raises(public.ReleaseError, match=f"HTTP {code}"):
        public.download_public(URL, target, _expected())
    assert not target.exists()


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("fixture timeout"),
        urllib.error.URLError("fixture connection failure"),
        OSError("fixture I/O failure"),
    ],
)
def test_open_errors_are_reported_without_fallback(
    transport: FakeTransport, tmp_path: Path, error: Exception
) -> None:
    target = tmp_path / "download.zip"
    transport.open_error = error
    with pytest.raises(public.ReleaseError, match="匿名取得に失敗"):
        public.download_public(URL, target, _expected())
    assert transport.builds == 1
    assert not target.exists()


@pytest.mark.parametrize("code", [201, 204, 301, 401, 404, 500])
def test_only_a_final_200_response_is_accepted(
    transport: FakeTransport, tmp_path: Path, code: int
) -> None:
    transport.response.status = code
    with pytest.raises(public.ReleaseError, match="匿名取得できません"):
        public.download_public(URL, tmp_path / "download.zip", _expected())
    assert transport.response.read_sizes == []
    assert transport.response.closed


@pytest.mark.parametrize("url", ["http://github.com/asset.zip", "https://127.0.0.1/asset.zip"])
def test_final_response_url_is_revalidated(
    transport: FakeTransport, tmp_path: Path, url: str
) -> None:
    transport.response.url = url
    with pytest.raises(public.ReleaseError):
        public.download_public(URL, tmp_path / "download.zip", _expected())
    assert transport.response.read_sizes == []
    assert transport.response.closed


@pytest.mark.parametrize("length", ["0", "1", "999", "-1", "invalid", "", "029"])
def test_content_length_must_match_exactly(
    transport: FakeTransport, tmp_path: Path, length: str
) -> None:
    transport.response.headers["Content-Length"] = length
    with pytest.raises(public.ReleaseError, match="Content-Length"):
        public.download_public(URL, tmp_path / "download.zip", _expected())
    assert transport.response.read_sizes == []
    assert transport.response.closed


def test_missing_content_length_is_allowed_when_stream_matches(
    transport: FakeTransport, tmp_path: Path
) -> None:
    transport.response.headers.clear()
    target = tmp_path / "download.zip"
    public.download_public(URL, target, _expected())
    assert target.read_bytes() == BODY


@pytest.mark.parametrize("encoding", ["gzip", "br", "identity, gzip", ""])
def test_encoded_responses_are_rejected(
    transport: FakeTransport, tmp_path: Path, encoding: str
) -> None:
    transport.response.headers["Content-Encoding"] = encoding
    with pytest.raises(public.ReleaseError, match="Content-Encoding"):
        public.download_public(URL, tmp_path / "download.zip", _expected())
    assert transport.response.read_sizes == []


@pytest.mark.parametrize("size", [0, -1, True, 1.5, "29", None])
def test_expected_size_must_be_a_positive_integer(
    transport: FakeTransport, tmp_path: Path, size: object
) -> None:
    expected = _expected()
    expected["size"] = size
    with pytest.raises(public.ReleaseError, match="期待長"):
        public.download_public(URL, tmp_path / "download.zip", expected)
    assert transport.builds == 0


def test_hash_mismatch_is_rejected(transport: FakeTransport, tmp_path: Path) -> None:
    expected = _expected()
    expected["sha256"] = "0" * 64
    with pytest.raises(public.ReleaseError, match="SHA256 / 長さ"):
        public.download_public(URL, tmp_path / "download.zip", expected)
    assert transport.response.closed


@pytest.mark.parametrize("body", [b"", BODY[:-1]])
def test_truncated_body_is_rejected_even_with_a_matching_header(
    transport: FakeTransport, tmp_path: Path, body: bytes
) -> None:
    transport.response = FakeResponse(body)
    transport.response.headers["Content-Length"] = str(len(BODY))
    with pytest.raises(public.ReleaseError, match="SHA256 / 長さ"):
        public.download_public(URL, tmp_path / "download.zip", _expected())
    assert transport.response.closed


def test_overlength_body_is_rejected_and_reads_are_bounded(
    transport: FakeTransport, tmp_path: Path
) -> None:
    transport.response = FakeResponse(BODY + b"unexpected payload")
    transport.response.headers["Content-Length"] = str(len(BODY))
    with pytest.raises(public.ReleaseError, match="期待長を超え"):
        public.download_public(URL, tmp_path / "download.zip", _expected())
    assert transport.response.read_sizes == [len(BODY) + 1]
    assert transport.response.closed


def test_large_download_reads_at_most_one_megabyte_per_chunk(
    transport: FakeTransport, tmp_path: Path
) -> None:
    body = b"a" * (1024 * 1024 + 3)
    transport.response = FakeResponse(body)
    target = tmp_path / "download.zip"
    public.download_public(URL, target, _expected(body))
    assert target.read_bytes() == body
    assert transport.response.read_sizes == [1024 * 1024, 4, 1]


@pytest.mark.parametrize(
    "error", [TimeoutError("fixture read timeout"), OSError("fixture read error")]
)
def test_stream_errors_close_the_response(
    transport: FakeTransport, tmp_path: Path, error: Exception
) -> None:
    transport.response.read_error = error
    with pytest.raises(public.ReleaseError, match="匿名取得に失敗"):
        public.download_public(URL, tmp_path / "download.zip", _expected())
    assert transport.response.closed


def test_overall_download_deadline_is_enforced(
    transport: FakeTransport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    times = iter([1000, 1300])
    monkeypatch.setattr(public.time, "monotonic", lambda: next(times))
    with pytest.raises(public.ReleaseError, match="時間切れ"):
        public.download_public(URL, tmp_path / "download.zip", _expected())
    assert transport.response.closed


@pytest.mark.parametrize("symlink", [False, True])
def test_existing_target_is_never_overwritten(
    transport: FakeTransport, tmp_path: Path, symlink: bool
) -> None:
    original = tmp_path / "existing.zip"
    original.write_bytes(b"keep the original asset")
    target = tmp_path / "download.zip" if symlink else original
    if symlink:
        target.symlink_to(original)
    with pytest.raises(public.ReleaseError):
        public.download_public(URL, target, _expected())
    assert original.read_bytes() == b"keep the original asset"
    assert target.is_symlink() is symlink
    assert transport.response.closed
