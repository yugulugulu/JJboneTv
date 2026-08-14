from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4
import wave

from fastapi import UploadFile
import httpx
import pytest

from app.config import settings
from app.exceptions import DomainValidationError
from app.schemas import GenerationCreate
from app.services import generation_provider, project_domain
from app.services.media_probe import MediaProbeError, inspect_media_file
from app.services.project_domain import MediaUploadTooLargeError, ProjectDomainService
from app.services.video_model_options import VIDEO_OPTIONS_KEY, validate_video_prompt


def test_video_status_accepts_nested_provider_payload():
    assert generation_provider._video_status({"data": {"status": "in_progress"}}) == "in_progress"


def test_video_status_normalizes_provider_aliases():
    assert generation_provider._video_status({"status": "processing"}) == "running"
    assert generation_provider._video_status({"status": "complete"}) == "completed"


def test_provider_json_payload_accepts_utf8_bom_and_sse():
    assert generation_provider._provider_json_payload(
        b"\xef\xbb\xbf{\"id\":\"task_123\"}"
    ) == {"id": "task_123"}
    assert generation_provider._provider_json_payload(
        b"event: update\ndata: {\"status\":\"queued\"}\n\ndata: {\"status\":\"completed\"}\n"
    ) == {"status": "completed"}


def test_video_task_id_accepts_string_task_field():
    assert generation_provider._video_task_id({"task": "task_123"}) == "task_123"


def test_provider_video_status_accepts_plain_text_status():
    response = httpx.Response(
        200,
        headers={"content-type": "text/plain"},
        request=httpx.Request("GET", "https://provider.example/task"),
    )
    assert generation_provider._provider_video_status_response(response, b"processing\n") == {
        "status": "processing"
    }


def test_video_content_download_follows_cross_origin_redirect_without_api_key(
    monkeypatch, tmp_path
):
    requests = []
    r2_host = "d4c280ea2c37b44805c584296cb271a0.r2.cloudflarestorage.com"

    def handler(request):
        requests.append(request)
        if request.url.host == "provider.example":
            return httpx.Response(
                307,
                headers={"location": f"https://{r2_host}/result.mp4?signature=secret"},
            )
        return httpx.Response(200, content=b"video-bytes")

    monkeypatch.setattr(
        generation_provider.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                generation_provider.socket.AF_INET,
                generation_provider.socket.SOCK_STREAM,
                6,
                "",
                ("198.18.1.37", 443),
            )
        ],
    )
    destination = tmp_path / "result.mp4"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        total = generation_provider._download_video_content(
            client,
            "https://provider.example/v1/videos/task_123/content",
            destination,
            {
                "Authorization": "Bearer provider-secret",
                "Accept": "video/mp4",
                "User-Agent": "ThinkTV-test",
            },
        )

    assert total == len(b"video-bytes")
    assert destination.read_bytes() == b"video-bytes"
    assert requests[0].headers["authorization"] == "Bearer provider-secret"
    assert "authorization" not in requests[1].headers
    assert requests[1].headers["accept"] == "video/mp4"


def test_video_content_download_keeps_api_key_for_same_origin_relative_redirect(
    monkeypatch, tmp_path
):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(307, headers={"location": "/download/task_123"})
        return httpx.Response(200, content=b"video")

    monkeypatch.setattr(
        generation_provider,
        "_validate_video_redirect_url",
        lambda value: value,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        generation_provider._download_video_content(
            client,
            "https://provider.example/v1/videos/task_123/content",
            tmp_path / "result.mp4",
            {"Authorization": "Bearer provider-secret"},
        )

    assert requests[1].url == httpx.URL("https://provider.example/download/task_123")
    assert requests[1].headers["authorization"] == "Bearer provider-secret"


def test_video_content_download_rejects_redirect_without_location(tmp_path):
    transport = httpx.MockTransport(lambda _request: httpx.Response(307))
    with httpx.Client(transport=transport) as client:
        with pytest.raises(
            generation_provider.ProviderGenerationError,
            match="缺少 Location",
        ):
            generation_provider._download_video_content(
                client,
                "https://provider.example/v1/videos/task_123/content",
                tmp_path / "result.mp4",
                {"Authorization": "Bearer provider-secret"},
            )


def test_video_content_download_stops_after_three_redirects(monkeypatch, tmp_path):
    request_count = 0

    def handler(_request):
        nonlocal request_count
        request_count += 1
        return httpx.Response(307, headers={"location": f"/redirect/{request_count}"})

    monkeypatch.setattr(
        generation_provider,
        "_validate_video_redirect_url",
        lambda value: value,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(
            generation_provider.ProviderGenerationError,
            match="重定向次数过多",
        ):
            generation_provider._download_video_content(
                client,
                "https://provider.example/v1/videos/task_123/content",
                tmp_path / "result.mp4",
                {"Authorization": "Bearer provider-secret"},
            )

    assert request_count == 4


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/video.mp4",
        "https://127.0.0.1/video.mp4",
        "http://169.254.169.254/latest/meta-data",
        "https://localhost/video.mp4",
        "https://user:password@cdn.example/video.mp4",
    ],
)
def test_video_redirect_url_rejects_unsafe_targets(url):
    with pytest.raises(generation_provider.ProviderGenerationError):
        generation_provider._validate_video_redirect_url(url)


def test_video_redirect_url_accepts_cloudflare_r2_with_proxy_fake_ip(monkeypatch):
    monkeypatch.setattr(
        generation_provider.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                generation_provider.socket.AF_INET,
                generation_provider.socket.SOCK_STREAM,
                6,
                "",
                ("198.18.1.37", 443),
            )
        ],
    )

    url = (
        "https://d4c280ea2c37b44805c584296cb271a0.r2.cloudflarestorage.com/"
        "video.mp4?signature=temporary"
    )

    assert generation_provider._validate_video_redirect_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://d4c280ea2c37b44805c584296cb271a0.r2.cloudflarestorage.com/video.mp4",
        "https://d4c280ea2c37b44805c584296cb271a0.r2.cloudflarestorage.com:8443/video.mp4",
        "https://r2.cloudflarestorage.com/video.mp4",
        "https://d4c280ea2c37b44805c584296cb271a0.r2.cloudflarestorage.com.evil.example/video.mp4",
        "https://evil-d4c280ea2c37b44805c584296cb271a0.r2.cloudflarestorage.com/video.mp4",
        "https://198.18.1.37/video.mp4",
    ],
)
def test_video_redirect_url_does_not_extend_fake_ip_exception_to_unsafe_hosts(
    monkeypatch, url
):
    monkeypatch.setattr(
        generation_provider.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                generation_provider.socket.AF_INET,
                generation_provider.socket.SOCK_STREAM,
                6,
                "",
                ("198.18.1.37", 443),
            )
        ],
    )

    with pytest.raises(generation_provider.ProviderGenerationError):
        generation_provider._validate_video_redirect_url(url)


def test_video_redirect_url_rejects_r2_hostname_resolving_to_private_network(
    monkeypatch,
):
    monkeypatch.setattr(
        generation_provider.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                generation_provider.socket.AF_INET,
                generation_provider.socket.SOCK_STREAM,
                6,
                "",
                ("10.0.0.8", 443),
            )
        ],
    )

    with pytest.raises(generation_provider.ProviderGenerationError):
        generation_provider._validate_video_redirect_url(
            "https://d4c280ea2c37b44805c584296cb271a0.r2.cloudflarestorage.com/video.mp4"
        )


def test_video_prompt_keeps_client_text_and_compiles_image_tokens():
    first, second = uuid4(), uuid4()
    prompt = f"让 @{{image:{second}}} 飞到 @{{image:{first}}} 后面"
    references = [
        {"node_id": str(first), "kind": "image"},
        {"node_id": str(second), "kind": "image"},
    ]
    assert generation_provider.effective_generation_prompt("video", prompt, references) == (
        "让 @参考图二 飞到 @参考图一 后面"
    )


def test_video_prompt_compiles_image_video_and_audio_reference_tokens_by_kind():
    image_id, video_id, audio_id = uuid4(), uuid4(), uuid4()
    prompt = (
        f"让 @{{image:{image_id}}} 配合 @{{video:{video_id}}}，"
        f"并使用 @{{audio:{audio_id}}}"
    )
    references = [
        {"node_id": str(image_id), "kind": "image"},
        {"node_id": str(video_id), "kind": "video"},
        {"node_id": str(audio_id), "kind": "audio"},
    ]
    assert generation_provider.effective_generation_prompt("video", prompt, references) == (
        "让 @参考图一 配合 @参考视频一，并使用 @参考音频一"
    )


def test_video_prompt_rejects_media_token_for_non_direct_or_wrong_kind_reference():
    image_id, video_id = uuid4(), uuid4()
    with pytest.raises(DomainValidationError, match="direct video predecessor"):
        generation_provider.effective_generation_prompt(
            "video",
            f"@{{video:{video_id}}}",
            [{"node_id": str(image_id), "kind": "image"}],
        )


def test_video_text_predecessor_is_rejected_even_when_client_prompt_exists():
    text_node_id = uuid4()
    references = [
        {
            "node_id": str(text_node_id),
            "kind": "text",
            "prompt": "使用前置文本生成视频",
        }
    ]

    with pytest.raises(DomainValidationError, match="does not accept a text predecessor"):
        generation_provider.effective_generation_prompt(
            "video", "这是视频节点遗留的草稿", references
        )


def test_video_prompt_rejects_any_text_predecessor():
    with pytest.raises(DomainValidationError, match="does not accept a text predecessor"):
        generation_provider.effective_generation_prompt(
            "video",
            "客户端草稿",
            [
                {"node_id": str(uuid4()), "kind": "text", "prompt": "第一段"},
                {"node_id": str(uuid4()), "kind": "text", "prompt": "第二段"},
            ],
        )


@pytest.mark.parametrize("prompt", ["", " \n\t "])
def test_pure_video_generation_requires_non_empty_prompt(prompt):
    with pytest.raises(DomainValidationError, match="requires a non-empty prompt"):
        generation_provider.effective_generation_prompt("video", prompt, [])


def test_pure_text_video_generation_can_create_task_without_predecessors(monkeypatch):
    owner_id = uuid4()
    project_id = uuid4()
    model_id = uuid4()
    target_node_id = uuid4()
    payload = GenerationCreate(
        project_id=project_id,
        node_id=target_node_id,
        input_node_ids=[],
        input_revision=1,
        kind="video",
        model=str(model_id),
        prompt="一艘红色火箭从雨夜城市上空升起",
        parameters={},
    )
    project = SimpleNamespace(
        id=project_id,
        current_revision=1,
        workflow={
            "nodes": [
                {"id": str(target_node_id), "type": "video", "data": {}},
            ],
            "edges": [],
        },
    )
    provider = {
        "kind": "video",
        "config_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "default_parameters": {},
        "metadata": {},
    }
    added_tasks = []
    db = SimpleNamespace(
        add=lambda task: added_tasks.append(task),
        commit=lambda: None,
        rollback=lambda: None,
        refresh=lambda _task: None,
    )
    service = ProjectDomainService(db)
    service.repo = SimpleNamespace(
        get_generation_by_idempotency=lambda *_args: None,
        lock_project=lambda *_args: project,
    )
    monkeypatch.setattr(
        project_domain,
        "ModelCatalogService",
        lambda _db: SimpleNamespace(
            resolve_provider_configuration=lambda *_args, **_kwargs: provider,
        ),
    )

    result = service.create_generation(owner_id, payload, "pure-text-video")

    assert result.replayed is False
    assert result.task is added_tasks[0]
    assert result.task.prompt == payload.prompt
    assert result.task.parameters == {
        generation_provider.INPUT_REFERENCES_PARAMETER: []
    }


def test_video_generation_task_rejects_direct_text_predecessor(monkeypatch):
    owner_id = uuid4()
    project_id = uuid4()
    model_id = uuid4()
    text_node_id = uuid4()
    target_node_id = uuid4()
    authoritative_prompt = "前置文本节点中的权威视频提示词"
    payload = GenerationCreate(
        project_id=project_id,
        node_id=target_node_id,
        input_node_ids=[text_node_id],
        input_revision=1,
        kind="video",
        model=str(model_id),
        prompt="客户端遗留的视频节点草稿",
        parameters={},
    )
    project = SimpleNamespace(
        id=project_id,
        current_revision=1,
        workflow={
            "nodes": [
                {
                    "id": str(text_node_id),
                    "type": "prompt",
                    "data": {"prompt": authoritative_prompt},
                },
                {"id": str(target_node_id), "type": "video", "data": {}},
            ],
            "edges": [
                {
                    "source_node_id": str(text_node_id),
                    "target_node_id": str(target_node_id),
                }
            ],
        },
    )
    provider = {
        "kind": "video",
        "config_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "default_parameters": {},
        "metadata": {},
    }
    added_tasks = []
    db = SimpleNamespace(
        add=lambda task: added_tasks.append(task),
        commit=lambda: None,
        rollback=lambda: None,
        refresh=lambda _task: None,
    )
    service = ProjectDomainService(db)
    service.repo = SimpleNamespace(
        get_generation_by_idempotency=lambda *_args: None,
        lock_project=lambda *_args: project,
    )
    monkeypatch.setattr(
        project_domain,
        "ModelCatalogService",
        lambda _db: SimpleNamespace(
            resolve_provider_configuration=lambda *_args, **_kwargs: provider,
        ),
    )

    with pytest.raises(DomainValidationError, match="does not accept a text predecessor"):
        service.create_generation(owner_id, payload, "linked-text-video")
    assert added_tasks == []


def test_video_prompt_limit_is_applied_after_image_reference_compilation():
    image_id = uuid4()
    prompt = f"让 @{{image:{image_id}}} 飞行"
    references = [{"node_id": str(image_id), "kind": "image"}]
    compiled = generation_provider.effective_generation_prompt("video", prompt, references)

    validate_video_prompt(
        {VIDEO_OPTIONS_KEY: {"promptMaxLength": len(compiled)}},
        compiled,
    )
    with pytest.raises(DomainValidationError, match=f"cannot exceed {len(compiled) - 1}"):
        validate_video_prompt(
            {VIDEO_OPTIONS_KEY: {"promptMaxLength": len(compiled) - 1}},
            compiled,
        )


def test_video_generation_rejects_compiled_prompt_before_task_is_created(monkeypatch):
    owner_id = uuid4()
    project_id = uuid4()
    model_id = uuid4()
    source_node_id = uuid4()
    target_node_id = uuid4()
    asset_id = uuid4()
    compiled_prompt = "@参考图一向前飞行"
    payload = GenerationCreate(
        project_id=project_id,
        node_id=target_node_id,
        input_node_ids=[source_node_id],
        input_revision=1,
        kind="video",
        model=str(model_id),
        prompt=f"@{{image:{source_node_id}}}向前飞行",
        parameters={},
    )
    project = SimpleNamespace(
        id=project_id,
        current_revision=1,
        workflow={
            "nodes": [
                {
                    "id": str(source_node_id),
                    "type": "image",
                    "data": {"asset_id": str(asset_id)},
                },
                {"id": str(target_node_id), "type": "video", "data": {}},
            ],
            "edges": [
                {
                    "source_node_id": str(source_node_id),
                    "target_node_id": str(target_node_id),
                }
            ],
        },
    )
    asset = SimpleNamespace(
        id=asset_id,
        project_id=project_id,
        kind="image",
        status="ready",
        mime_type="image/png",
        byte_size=1,
    )
    provider = {
        "kind": "video",
        "config_schema": {"type": "object", "properties": {}},
        "default_parameters": {},
        "metadata": {
            "supported_input_kinds": ["image"],
            VIDEO_OPTIONS_KEY: {"promptMaxLength": len(compiled_prompt) - 1},
        },
    }
    added_tasks = []
    service = ProjectDomainService(
        SimpleNamespace(add=lambda task: added_tasks.append(task))
    )
    service.repo = SimpleNamespace(
        get_generation_by_idempotency=lambda *_args: None,
        lock_project=lambda *_args: project,
        get_asset=lambda *_args: asset,
    )
    monkeypatch.setattr(
        project_domain,
        "ModelCatalogService",
        lambda _db: SimpleNamespace(
            resolve_provider_configuration=lambda *_args, **_kwargs: provider,
        ),
    )

    with pytest.raises(
        DomainValidationError,
        match=f"cannot exceed {len(compiled_prompt) - 1}",
    ):
        service.create_generation(owner_id, payload, "video-prompt-limit")

    assert added_tasks == []


def test_video_worker_rejects_tightened_prompt_limit_before_provider_request(monkeypatch):
    task_id = uuid4()
    lease_token = uuid4()
    owner_id = uuid4()
    project_id = uuid4()
    model_id = uuid4()
    image_node_id = uuid4()
    task = SimpleNamespace(
        owner_id=owner_id,
        project_id=project_id,
        model=str(model_id),
        prompt=f"使用 @{{image:{image_node_id}}} 生成视频",
        parameters=generation_provider.stored_generation_parameters(
            {},
            [{"node_id": str(image_node_id), "kind": "image"}],
        ),
    )
    provider = {
        "kind": "video",
        "model_name": "video-test",
        "base_url": "https://provider.example/v1",
        "api_key": "sk-test",
        "metadata": {VIDEO_OPTIONS_KEY: {"promptMaxLength": 2}},
    }
    failed_calls = []
    provider_calls = []

    class Transaction:
        def __enter__(self):
            return SimpleNamespace()

        def __exit__(self, *_):
            return False

    class SessionFactory:
        @staticmethod
        def begin():
            return Transaction()

    monkeypatch.setattr(generation_provider, "SessionLocal", SessionFactory)
    monkeypatch.setattr(
        generation_provider,
        "GenerationTaskRepository",
        lambda _db: SimpleNamespace(
            leased_task=lambda *_args, **_kwargs: task,
        ),
    )
    monkeypatch.setattr(
        generation_provider,
        "ModelCatalogService",
        lambda _db: SimpleNamespace(
            resolve_provider_configuration=lambda *_args, **_kwargs: provider,
        ),
    )
    monkeypatch.setattr(
        generation_provider,
        "provider_http_client",
        lambda *_args, **_kwargs: provider_calls.append(True),
    )
    monkeypatch.setattr(
        generation_provider,
        "fail_task",
        lambda *args, **kwargs: failed_calls.append((args, kwargs)),
    )

    generation_provider._process_video_generation_task(task_id, lease_token)

    assert provider_calls == []
    assert failed_calls
    assert "cannot exceed 2" in failed_calls[0][0][2]


def test_video_endpoint_does_not_duplicate_videos_path():
    assert (
        generation_provider.video_endpoint("https://provider.example/v1/videos", {})
        == "https://provider.example/v1/videos"
    )
    assert (
        generation_provider.video_endpoint("https://provider.example/v1", {})
        == "https://provider.example/v1/videos"
    )


def test_local_reference_url_uses_configured_public_base(monkeypatch):
    monkeypatch.setattr(
        generation_provider,
        "storage_for_asset",
        lambda _metadata: SimpleNamespace(),
    )
    monkeypatch.setattr(settings, "app_public_base_url", "https://app.example/")
    asset_id = uuid4()
    asset = SimpleNamespace(
        id=asset_id,
        storage_key="owner/project/input.mp4",
        asset_metadata={"storage_backend": "local"},
    )
    assert (
        generation_provider._asset_reference_url(asset)
        == f"https://app.example/api/assets/{asset_id}/content"
    )


def test_video_upload_is_rejected_before_full_file_is_spooled(monkeypatch):
    monkeypatch.setattr(settings, "user_video_upload_max_bytes", 4)
    upload = UploadFile(
        filename="clip.mp4",
        file=BytesIO(b"12345"),
        headers={"content-type": "video/mp4"},
    )
    try:
        ProjectDomainService._spool_media(upload, "video")
    except MediaUploadTooLargeError:
        pass
    else:
        raise AssertionError("oversized video upload should be rejected")


def test_media_probe_parses_video_metadata(monkeypatch, tmp_path):
    payload = (
        '{"streams":[{"codec_type":"video","width":1280,"height":720,'
        '"duration":"3.25"}],"format":{"duration":"3.5"}}'
    )
    monkeypatch.setattr(
        "app.services.media_probe.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=payload, stderr=""
        ),
    )

    result = inspect_media_file(tmp_path / "clip.mp4", "video")

    assert result.duration_ms == 3500
    assert result.width == 1280
    assert result.height == 720


def test_media_probe_rejects_wrong_stream_kind(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.services.media_probe.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"streams":[{"codec_type":"audio"}],"format":{"duration":"1"}}',
            stderr="",
        ),
    )

    try:
        inspect_media_file(tmp_path / "audio.mp3", "video")
    except MediaProbeError:
        pass
    else:
        raise AssertionError("audio-only content must not be accepted as video")


def test_media_probe_parses_wav_without_ffprobe(tmp_path):
    path = tmp_path / "tone.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\x00\x00" * 8000)

    result = inspect_media_file(path, "audio")

    assert result.duration_ms == 1000
    assert result.width is None
    assert result.height is None


def test_media_probe_parses_mp3_without_ffprobe(tmp_path):
    path = tmp_path / "tone.mp3"
    # MPEG-1 Layer III, 128 kbps, 44.1 kHz: each frame is 417 bytes.
    frame = bytes((0xFF, 0xFB, 0x90, 0x00)) + bytes(413)
    path.write_bytes(frame * 10)

    result = inspect_media_file(path, "audio")

    assert 250 <= result.duration_ms <= 270


def test_media_probe_rejects_non_mp3_wav_audio(tmp_path):
    path = tmp_path / "fake.mp3"
    path.write_bytes(b"this is not audio")

    try:
        inspect_media_file(path, "audio")
    except MediaProbeError as error:
        assert "MP3 or WAV" in str(error)
    else:
        raise AssertionError("non-audio content must not be accepted")
