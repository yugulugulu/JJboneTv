import httpx
import pytest

from app.services import generation_provider


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/result.png",
        "https://127.0.0.1/result.png",
        "http://169.254.169.254/latest/meta-data",
        "https://localhost/result.png",
        "https://user:password@cdn.example/result.png",
    ],
)
def test_image_download_url_rejects_ssrf_targets(url):
    with pytest.raises(generation_provider.ProviderGenerationError):
        generation_provider._validate_image_download_url(url)


def test_image_download_url_rejects_hostname_with_any_private_resolution(monkeypatch):
    monkeypatch.setattr(
        generation_provider.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                generation_provider.socket.AF_INET,
                generation_provider.socket.SOCK_STREAM,
                6,
                "",
                ("8.8.8.8", 443),
            ),
            (
                generation_provider.socket.AF_INET,
                generation_provider.socket.SOCK_STREAM,
                6,
                "",
                ("10.0.0.8", 443),
            ),
        ],
    )

    with pytest.raises(
        generation_provider.ProviderGenerationError,
        match="受限地址",
    ):
        generation_provider._validate_image_download_url(
            "https://images.example/result.png"
        )


def test_image_download_validates_initial_url_and_every_redirect(monkeypatch):
    validated = []
    requests = []

    def validate(url):
        validated.append(url)
        return url

    def handler(request):
        requests.append(str(request.url))
        if len(requests) == 1:
            return httpx.Response(
                302,
                headers={"location": "https://cdn.example/result.png"},
            )
        return httpx.Response(
            200,
            headers={"content-type": "image/png"},
            content=b"\x89PNG\r\n\x1a\nimage-data",
        )

    monkeypatch.setattr(generation_provider, "_validate_image_download_url", validate)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        data, declared = generation_provider._download_image(
            client, "https://provider.example/result"
        )

    assert validated == [
        "https://provider.example/result",
        "https://cdn.example/result.png",
    ]
    assert requests == validated
    assert data == b"\x89PNG\r\n\x1a\nimage-data"
    assert declared == "image/png"
