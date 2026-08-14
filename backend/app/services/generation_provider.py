import base64
import binascii
import hashlib
import ipaddress
import json
import logging
import random
import re
import socket
import tempfile
import time
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from uuid import UUID

import httpx
from sqlalchemy import select

from ..config import settings
from ..database import SessionLocal
from ..exceptions import DomainValidationError, ResourceNotFoundError
from ..models import Asset, GenerationOutput, GenerationTask
from ..repositories.generation_tasks import GenerationTaskRepository
from .asset_storage import (
    AssetStorageError,
    LocalAssetStorage,
    generation_asset_storage,
    storage_for_asset,
    storage_metadata,
)
from .model_catalog import ModelCatalogService, validate_http_api_key
from .media_probe import MediaProbeError, inspect_media_file
from .image_quality_options import resolve_image_quality_parameters
from .image_size_matrix import (
    IMAGE_SIZE_UI_PARAMETERS,
    SEMANTIC_SIZE_PARAMETERS,
    ensure_matrix_size_is_supported,
    image_size_lookup,
    is_valid_concrete_image_size,
    resolve_image_size_parameters,
)
from .generation_tasks import (
    claim_specific_task,
    fail_task,
    lease_heartbeat,
    mark_provider_started,
    retry_task,
)
from .video_model_options import validate_video_input_references, validate_video_prompt


logger = logging.getLogger(__name__)

INPUT_REFERENCES_PARAMETER = "_input_references"
OPENAI_IMAGE_EDIT_PROTOCOL = "openai_images_multipart"
PROTOCOLS_REQUIRING_PROMPT = {"openai_images"}
IMAGE_REFERENCE_TOKEN_RE = re.compile(
    r"@\{image:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\}"
)
MEDIA_REFERENCE_TOKEN_RE = re.compile(
    r"@\{(image|video|audio):([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\}",
    re.IGNORECASE,
)
REFERENCE_IMAGE_ORDINALS = ("一", "二", "三", "四", "五", "六", "七", "八", "九", "十")
VIDEO_STATUS_ALIASES = {
    "pending": "queued",
    "created": "queued",
    "waiting": "queued",
    "processing": "running",
    "in-progress": "running",
    "in progress": "running",
    "complete": "completed",
    "success": "succeeded",
    "done": "succeeded",
    "canceled": "cancelled",
}
VIDEO_PLAIN_STATUS_VALUES = {
    "queued",
    "running",
    "in_progress",
    "completed",
    "succeeded",
    "failed",
    "error",
    "cancelled",
    "canceled",
    "pending",
    "created",
    "waiting",
    "processing",
    "success",
    "done",
}
VIDEO_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,159}$")
VIDEO_DOWNLOAD_REDIRECT_CODES = {301, 302, 303, 307, 308}
VIDEO_DOWNLOAD_MAX_REDIRECTS = 3
VIDEO_DOWNLOAD_MAX_URL_LENGTH = 8192
VIDEO_R2_DOWNLOAD_HOST_RE = re.compile(
    r"^[0-9a-f]{32}\.r2\.cloudflarestorage\.com$"
)
VIDEO_PROXY_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


class ProviderGenerationError(Exception):
    pass


class ProviderHTTPError(ProviderGenerationError):
    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


def frozen_generation_parameters(supplied: dict, provider: dict | None = None) -> dict:
    """Return a frozen task snapshot, cleaning matrix UI aliases defensively."""

    has_matrix = (
        provider is not None
        and provider.get("kind") == "image"
        and image_size_lookup(provider) is not None
    )
    unresolved = (
        IMAGE_SIZE_UI_PARAMETERS if has_matrix else SEMANTIC_SIZE_PARAMETERS
    ).intersection(supplied)
    if not unresolved:
        return dict(supplied)
    if not has_matrix or not is_valid_concrete_image_size(supplied.get("size")):
        raise ProviderGenerationError(
            "Persisted generation parameters contain unresolved image size selections"
        )
    return {
        key: value
        for key, value in supplied.items()
        if key not in IMAGE_SIZE_UI_PARAMETERS
    }


def _validate_resolved_generation_parameters(provider: dict, parameters: dict) -> dict:
    schema = provider.get("config_schema") or {}
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}

    if schema.get("additionalProperties") is False:
        unknown = sorted(set(parameters) - set(properties))
        if unknown:
            raise DomainValidationError(f"Unsupported model parameter: {unknown[0]}")

    for name in schema.get("required") or []:
        if name not in parameters:
            raise DomainValidationError(f"Missing required model parameter: {name}")

    for name, value in parameters.items():
        definition = properties.get(name)
        if not isinstance(definition, dict):
            continue
        expected = definition.get("type")
        valid_type = (
            expected == "string" and isinstance(value, str)
            or expected == "integer" and isinstance(value, int) and not isinstance(value, bool)
            or expected == "number" and isinstance(value, (int, float)) and not isinstance(value, bool)
            or expected == "boolean" and isinstance(value, bool)
            or expected == "object" and isinstance(value, dict)
            or expected == "array" and isinstance(value, list)
            or expected is None
        )
        if not valid_type:
            raise DomainValidationError(f"Invalid value type for model parameter: {name}")
        if "enum" in definition and value not in definition["enum"]:
            raise DomainValidationError(f"Unsupported value for model parameter: {name}")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in definition and value < definition["minimum"]:
                raise DomainValidationError(f"Model parameter is below minimum: {name}")
            if "maximum" in definition and value > definition["maximum"]:
                raise DomainValidationError(f"Model parameter is above maximum: {name}")
    return parameters


def validate_generation_parameters(provider: dict, supplied: dict) -> dict:
    parameters = {**(provider.get("default_parameters") or {}), **supplied}
    return _validate_resolved_generation_parameters(provider, parameters)


def normalize_generation_parameters(provider: dict, supplied: dict) -> dict:
    """Resolve image UI selections before validating provider-facing parameters."""

    resolved_defaults = resolve_image_size_parameters(
        provider, provider.get("default_parameters") or {}
    )
    resolved_supplied = resolve_image_size_parameters(provider, supplied)
    resolved_defaults = resolve_image_quality_parameters(provider, resolved_defaults)
    resolved_supplied = resolve_image_quality_parameters(provider, resolved_supplied)
    parameters = _validate_resolved_generation_parameters(
        provider, {**resolved_defaults, **resolved_supplied}
    )
    ensure_matrix_size_is_supported(provider, parameters)
    return parameters


def stored_generation_parameters(model_parameters: dict, input_references: list[dict]) -> dict:
    return {**model_parameters, INPUT_REFERENCES_PARAMETER: input_references}


def split_generation_parameters(stored: dict) -> tuple[dict, list[dict]]:
    references = stored.get(INPUT_REFERENCES_PARAMETER, [])
    if not isinstance(references, list) or not all(
        isinstance(reference, dict) for reference in references
    ):
        raise DomainValidationError("Persisted generation input references are invalid")
    return (
        {key: value for key, value in stored.items() if key != INPUT_REFERENCES_PARAMETER},
        references,
    )


def validate_provider_input_support(provider: dict, input_references: list[dict]) -> None:
    media_kinds = {
        reference.get("kind")
        for reference in input_references
        if reference.get("kind") in {"image", "video", "audio"}
    }
    if not media_kinds:
        return
    metadata = provider.get("metadata") or {}
    declared = metadata.get("supported_input_kinds")
    declared_kinds = (
        {kind for kind in declared if isinstance(kind, str)}
        if isinstance(declared, list)
        else {"text"}
    )
    unsupported_by_model = sorted(media_kinds - declared_kinds)
    if unsupported_by_model:
        raise DomainValidationError(
            "unsupported-input: Selected model does not support predecessor input type: "
            + unsupported_by_model[0]
        )
    if provider.get("kind") == "video":
        # Video providers use the asynchronous /videos adapter below and may
        # accept mixed reference media according to video_options.
        return
    if media_kinds != {"image"}:
        raise DomainValidationError(
            "unsupported-input: Provider protocol does not support predecessor input type: "
            + sorted(media_kinds)[0]
        )
    if metadata.get("protocol") != "openai_images":
        raise DomainValidationError("unsupported-input: Provider does not support image generation")
    if metadata.get("edit_protocol") != OPENAI_IMAGE_EDIT_PROTOCOL:
        raise DomainValidationError(
            "unsupported-input: Provider does not support OpenAI multipart image editing"
        )
    edit_path = metadata.get("edit_endpoint_path")
    if not _valid_relative_endpoint_path(edit_path):
        raise DomainValidationError(
            "unsupported-input: Provider image edit endpoint is not configured"
        )
    image_count = sum(reference.get("kind") == "image" for reference in input_references)
    image_edit_transport(provider, image_count)


def image_edit_transport(provider: dict, image_count: int) -> tuple[int, str]:
    metadata = provider.get("metadata") or {}
    configured_max = metadata.get("edit_max_input_images")
    configured_field = metadata.get("edit_image_field")
    if configured_max is None and configured_field is None:
        maximum, field_name = 1, "image"
    else:
        if (
            not isinstance(configured_max, int)
            or isinstance(configured_max, bool)
            or not 1 <= configured_max <= 5
            or configured_field not in {"image", "image[]"}
        ):
            raise DomainValidationError(
                "unsupported-input: Provider edit_max_input_images/edit_image_field configuration is invalid"
            )
        maximum, field_name = configured_max, configured_field
    if image_count > maximum:
        raise DomainValidationError(
            f"unsupported-input: Selected model accepts at most {maximum} image predecessors"
        )
    return maximum, field_name


def _valid_relative_endpoint_path(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    candidate = value.strip()
    parsed = urlparse(candidate)
    parts = parsed.path.lstrip("/").split("/")
    return (
        not parsed.scheme
        and not parsed.netloc
        and not parsed.query
        and not parsed.fragment
        and not candidate.startswith("//")
        and all(part not in {"", ".", ".."} for part in parts)
        and not any(character.isspace() or ord(character) < 32 for character in candidate)
    )


def multipart_scalar_parameters(parameters: dict) -> dict[str, str]:
    encoded: dict[str, str] = {}
    for name, value in parameters.items():
        if name in {"image", "model", "prompt"}:
            raise DomainValidationError(f"Image edit model parameter is reserved: {name}")
        if isinstance(value, bool):
            encoded[name] = "true" if value else "false"
        elif isinstance(value, (str, int, float)):
            encoded[name] = str(value)
        else:
            raise DomainValidationError(
                f"Image edit model parameter must be a scalar value: {name}"
            )
    return encoded


def resolved_generation_prompt(client_prompt: str, input_references: list[dict]) -> str:
    text_prompts = [
        reference["prompt"]
        for reference in input_references
        if reference.get("kind") == "text" and isinstance(reference.get("prompt"), str)
    ]
    if not text_prompts:
        return client_prompt
    return "\n\n".join(text_prompts)


def effective_generation_prompt(
    kind: str, client_prompt: str, input_references: list[dict]
) -> str:
    if kind == "video":
        text_prompts = [
            reference["prompt"]
            for reference in input_references
            if reference.get("kind") == "text" and isinstance(reference.get("prompt"), str)
        ]
        if text_prompts:
            raise DomainValidationError(
                "invalid-input: Video generation does not accept a text predecessor; enter the prompt in the video node"
            )
        source_prompt = client_prompt
        compiled_prompt = compile_video_reference_tokens(source_prompt, input_references)
        if not compiled_prompt.strip():
            raise DomainValidationError(
                "invalid-input: Video generation requires a non-empty prompt"
            )
        return compiled_prompt
    if kind != "image":
        return resolved_generation_prompt(client_prompt, input_references)
    text_prompts = [
        reference["prompt"]
        for reference in input_references
        if reference.get("kind") == "text" and isinstance(reference.get("prompt"), str)
    ]
    source_prompt = client_prompt if client_prompt.strip() else "\n\n".join(text_prompts)
    return compile_image_reference_tokens(source_prompt, input_references)


def compile_image_reference_tokens(prompt: str, input_references: list[dict]) -> str:
    image_node_ids = [
        str(reference.get("node_id"))
        for reference in input_references
        if reference.get("kind") == "image"
    ]
    node_positions = {node_id: index for index, node_id in enumerate(image_node_ids)}
    matches = list(IMAGE_REFERENCE_TOKEN_RE.finditer(prompt))
    if prompt.count("@{image:") != len(matches):
        raise DomainValidationError("invalid-input: Image reference token is malformed")
    mentioned_ids = [str(UUID(match.group(1))) for match in matches]
    if any(node_id not in node_positions for node_id in mentioned_ids):
        raise DomainValidationError(
            "invalid-input: Image reference token must reference a direct image predecessor"
        )
    return IMAGE_REFERENCE_TOKEN_RE.sub(
        lambda match: f"图片{node_positions[str(UUID(match.group(1)))] + 1}", prompt
    )


def compile_video_reference_tokens(prompt: str, input_references: list[dict]) -> str:
    node_positions: dict[tuple[str, str], int] = {}
    counters: dict[str, int] = {"image": 0, "video": 0, "audio": 0}
    for reference in input_references:
        kind = str(reference.get("kind") or "").lower()
        if kind not in counters:
            continue
        node_id = str(reference.get("node_id") or "")
        node_positions[(kind, node_id)] = counters[kind]
        counters[kind] += 1

    matches = list(MEDIA_REFERENCE_TOKEN_RE.finditer(prompt))
    token_prefixes = sum(prompt.lower().count(f"@{{{kind}:") for kind in counters)
    if token_prefixes != len(matches):
        raise DomainValidationError("invalid-input: Video media reference token is malformed")

    def reference_label(kind: str, position: int) -> str:
        ordinal = (
            REFERENCE_IMAGE_ORDINALS[position]
            if position < len(REFERENCE_IMAGE_ORDINALS)
            else str(position + 1)
        )
        prefix = {"image": "参考图", "video": "参考视频", "audio": "参考音频"}[kind]
        return f"@{prefix}{ordinal}"

    def replace(match: re.Match[str]) -> str:
        kind = match.group(1).lower()
        node_id = str(UUID(match.group(2)))
        position = node_positions.get((kind, node_id))
        if position is None:
            raise DomainValidationError(
                f"invalid-input: Video {kind} reference token must reference a direct {kind} predecessor"
            )
        return reference_label(kind, position)

    return MEDIA_REFERENCE_TOKEN_RE.sub(replace, prompt)


def validate_required_provider_prompt(
    provider: dict, effective_prompt: str, input_references: list[dict]
) -> None:
    protocol = (provider.get("metadata") or {}).get("protocol")
    has_media_input = any(
        reference.get("kind") in {"image", "video", "audio"}
        for reference in input_references
    )
    if (
        protocol in PROTOCOLS_REQUIRING_PROMPT
        and not has_media_input
        and not effective_prompt.strip()
    ):
        raise DomainValidationError(
            "invalid-input: Provider requires a non-empty text predecessor or legacy prompt"
        )


def provider_prompt(prompt: str, input_references: list[dict]) -> str:
    media_kinds = sorted(
        {
            reference.get("kind")
            for reference in input_references
            if reference.get("kind") in {"image", "video", "audio"}
        }
    )
    if media_kinds:
        raise ProviderGenerationError(
            "unsupported-input: Provider adapter cannot serialize predecessor input type: "
            + media_kinds[0]
        )
    return resolved_generation_prompt(prompt, input_references)


def _validated_provider_base_url(base_url: str) -> str:
    candidate = base_url.strip().rstrip("/")
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ProviderGenerationError("模型接口地址无效")
    return candidate


def image_generation_endpoint(base_url: str, metadata: dict) -> str:
    candidate = _validated_provider_base_url(base_url)
    endpoint_path = metadata.get("endpoint_path")
    if isinstance(endpoint_path, str) and endpoint_path.strip():
        return candidate + "/" + endpoint_path.strip().lstrip("/")
    if candidate.endswith("/images/generations"):
        return candidate
    return candidate + "/images/generations"


def video_endpoint(base_url: str, metadata: dict, suffix: str = "") -> str:
    candidate = _validated_provider_base_url(base_url)
    configured = metadata.get("video_endpoint_path") or metadata.get("endpoint_path") or "/videos"
    if not isinstance(configured, str) or not _valid_relative_endpoint_path(configured):
        raise ProviderGenerationError("模型视频接口地址未正确配置")
    path = "/" + configured.strip().lstrip("/")
    path = path.rstrip("/")
    if candidate.endswith(path):
        endpoint = candidate
    elif path == "/videos" and candidate.endswith("/videos"):
        endpoint = candidate
    else:
        endpoint = candidate + path
    return endpoint.rstrip("/") + suffix


def _asset_reference_url(asset: Asset) -> str:
    if not asset.storage_key:
        raise ProviderGenerationError("视频参考素材缺少存储地址")
    storage = storage_for_asset(asset.asset_metadata)
    metadata = asset.asset_metadata if isinstance(asset.asset_metadata, dict) else {}
    if isinstance(storage, LocalAssetStorage) or metadata.get("storage_backend") == "local":
        base_url = settings.app_public_base_url.strip().rstrip("/")
        if not base_url:
            raise ProviderGenerationError("视频参考素材需要配置 OSS 或 APP_PUBLIC_BASE_URL")
        return f"{base_url}/api/assets/{asset.id}/content"
    try:
        return storage.signed_get_url(asset.storage_key)
    except Exception as error:
        raise ProviderGenerationError("无法生成视频参考素材地址") from error


def _video_status(payload: dict) -> str:
    value = payload.get("status") or payload.get("state")
    if value is None and isinstance(payload.get("data"), dict):
        value = payload["data"].get("status") or payload["data"].get("state")
    normalized = str(value).strip().lower() if value is not None else "unknown"
    return VIDEO_STATUS_ALIASES.get(normalized, normalized)


def _video_task_id(payload: dict) -> str | None:
    for key in ("id", "task_id", "taskId"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    data = payload.get("data")
    if isinstance(data, dict):
        return _video_task_id(data)
    task = payload.get("task")
    if isinstance(task, str) and task.strip():
        return task.strip()
    if isinstance(task, dict):
        return _video_task_id(task)
    return None


def _video_task_id_from_location(location: str | None) -> str | None:
    if not isinstance(location, str) or not location.strip():
        return None
    candidate = location.rstrip("/").rsplit("/", 1)[-1]
    return candidate if VIDEO_TASK_ID_RE.fullmatch(candidate) else None


def _provider_video_create_response(
    client: httpx.Client, endpoint: str, **kwargs
) -> dict:
    """Accept JSON, a plain task ID, or a 202 Location response."""

    with client.stream("POST", endpoint, **kwargs) as response:
        body = _limited_response_bytes(
            response, settings.provider_response_max_bytes, "供应商响应"
        )
        if not response.is_success:
            raise ProviderHTTPError(
                _provider_error(response.status_code, body),
                retryable=response.status_code == 429 or response.status_code >= 500,
            )
        payload = _provider_json_payload(body)
        if isinstance(payload, dict):
            return payload
        plain_id = _provider_body_text(body)
        if VIDEO_TASK_ID_RE.fullmatch(plain_id):
            return {"id": plain_id}
        location_id = _video_task_id_from_location(response.headers.get("location"))
        if location_id:
            return {"id": location_id}
        raise _provider_non_json_error("供应商返回的不是 JSON", response, body)


def _provider_video_status_response(response: httpx.Response, body: bytes) -> dict:
    payload = _provider_json_payload(body)
    if isinstance(payload, dict):
        return payload
    plain_status = _provider_body_text(body).lower()
    if plain_status in VIDEO_PLAIN_STATUS_VALUES:
        return {"status": plain_status}
    raise _provider_non_json_error("供应商视频状态响应不是 JSON", response, body)


def _url_origin(url: str) -> tuple[str, str, int]:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        raise ProviderGenerationError("供应商返回了无效的视频下载地址")
    try:
        port = parsed.port
    except ValueError as error:
        raise ProviderGenerationError("供应商返回了无效的视频下载地址") from error
    return (
        parsed.scheme.lower(),
        parsed.hostname.lower(),
        port or (443 if parsed.scheme.lower() == "https" else 80),
    )


def _validate_download_url(url: str, *, media_label: str) -> str:
    if not isinstance(url, str) or not url.strip() or len(url) > VIDEO_DOWNLOAD_MAX_URL_LENGTH:
        raise ProviderGenerationError(f"供应商返回了无效的{media_label}下载地址")
    candidate = url.strip()
    parsed = urlparse(candidate)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ProviderGenerationError(f"供应商返回了不安全的{media_label}下载地址")
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as error:
        raise ProviderGenerationError(f"供应商返回了无效的{media_label}下载地址") from error

    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as error:
        raise ProviderGenerationError(f"供应商返回了无效的{media_label}下载地址") from error
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ProviderGenerationError(f"供应商{media_label}下载地址指向受限地址")
    try:
        literal_addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        try:
            resolved = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except OSError as error:
            raise ProviderGenerationError(f"供应商{media_label}下载地址无法解析") from error
        literal_addresses = []
        for item in resolved:
            try:
                address = ipaddress.ip_address(item[4][0].split("%", 1)[0])
            except ValueError:
                continue
            if address not in literal_addresses:
                literal_addresses.append(address)
        if not literal_addresses:
            raise ProviderGenerationError(f"供应商{media_label}下载地址无法解析")
    restricted_addresses = [
        address for address in literal_addresses if not address.is_global
    ]
    trusted_r2_fake_ip = (
        parsed.scheme.lower() == "https"
        and port == 443
        and VIDEO_R2_DOWNLOAD_HOST_RE.fullmatch(hostname) is not None
        and bool(restricted_addresses)
        and all(
            address.version == 4 and address in VIDEO_PROXY_FAKE_IP_NETWORK
            for address in restricted_addresses
        )
    )
    if restricted_addresses and not trusted_r2_fake_ip:
        raise ProviderGenerationError(f"供应商{media_label}下载地址指向受限地址")
    return candidate


def _validate_video_redirect_url(url: str) -> str:
    return _validate_download_url(url, media_label="视频")


def _validate_image_download_url(url: str) -> str:
    return _validate_download_url(url, media_label="图片")


def _download_video_content(
    client: httpx.Client,
    content_url: str,
    destination: Path,
    headers: dict[str, str],
) -> int:
    """Download a provider video while following redirects without leaking credentials."""

    original_origin = _url_origin(content_url)
    current_url = content_url
    request_headers = dict(headers)
    redirects_followed = 0

    while True:
        with client.stream("GET", current_url, headers=request_headers) as response:
            if response.status_code in VIDEO_DOWNLOAD_REDIRECT_CODES:
                if redirects_followed >= VIDEO_DOWNLOAD_MAX_REDIRECTS:
                    raise ProviderGenerationError("供应商视频下载重定向次数过多")
                location = response.headers.get("location")
                if not location:
                    raise ProviderGenerationError("供应商视频下载重定向缺少 Location")
                next_url = _validate_video_redirect_url(urljoin(current_url, location))
                redirects_followed += 1
                if _url_origin(next_url) != original_origin:
                    request_headers.pop("Authorization", None)
                current_url = next_url
                continue

            if not response.is_success:
                body = _limited_response_bytes(
                    response, settings.provider_response_max_bytes, "视频结果"
                )
                raise ProviderHTTPError(
                    _provider_error(response.status_code, body),
                    retryable=response.status_code >= 500 or response.status_code == 429,
                )

            total = 0
            with destination.open("wb") as output:
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > settings.generated_video_asset_max_bytes:
                        raise ProviderGenerationError("供应商返回的视频超过大小限制")
                    output.write(chunk)
            return total


def image_edit_endpoint(base_url: str, metadata: dict) -> str:
    candidate = _validated_provider_base_url(base_url)
    endpoint_path = metadata.get("edit_endpoint_path")
    if not _valid_relative_endpoint_path(endpoint_path):
        raise ProviderGenerationError("模型图片编辑接口未正确配置")
    return candidate + "/" + endpoint_path.strip().lstrip("/")


def _provider_error(status_code: int, body: bytes) -> str:
    detail = ""
    try:
        payload = json.loads(body)
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            detail = str(error.get("message") or error.get("detail") or "")
        elif error:
            detail = str(error)
        elif isinstance(payload, dict):
            detail = str(payload.get("detail") or payload.get("message") or "")
    except (ValueError, UnicodeDecodeError):
        detail = body[:500].decode("utf-8", errors="replace")
    detail = detail.strip()[:500]
    return f"供应商请求失败 ({status_code})" + (f"：{detail}" if detail else "")


def _limited_response_bytes(response: httpx.Response, limit: int, label: str) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > limit:
                raise ProviderGenerationError(f"{label}超过大小限制")
        except ValueError:
            pass
    data = bytearray()
    for chunk in response.iter_bytes():
        data.extend(chunk)
        if len(data) > limit:
            raise ProviderGenerationError(f"{label}超过大小限制")
    return bytes(data)


def _decode_base64_image(value: str) -> bytes:
    encoded = value.split(",", 1)[1] if value.startswith("data:") and "," in value else value
    padding = len(encoded) - len(encoded.rstrip("="))
    estimated_size = (len(encoded) * 3) // 4 - padding
    if estimated_size > settings.generated_asset_max_bytes:
        raise ProviderGenerationError("供应商返回的图片超过大小限制")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ProviderGenerationError("供应商返回了无效的图片数据") from error
    if not data:
        raise ProviderGenerationError("供应商返回了空图片")
    if len(data) > settings.generated_asset_max_bytes:
        raise ProviderGenerationError("供应商返回的图片超过大小限制")
    return data


def _image_format(data: bytes, declared: str | None = None) -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    if declared in {"image/png", "image/jpeg", "image/webp"}:
        extensions = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
        return declared, extensions[declared]
    raise ProviderGenerationError("供应商返回的文件不是支持的图片格式")


def _download_image(client: httpx.Client, url: str) -> tuple[bytes, str | None]:
    current_url = _validate_image_download_url(url)
    redirects_followed = 0

    while True:
        with client.stream("GET", current_url, follow_redirects=False) as response:
            if response.status_code in VIDEO_DOWNLOAD_REDIRECT_CODES:
                if redirects_followed >= VIDEO_DOWNLOAD_MAX_REDIRECTS:
                    raise ProviderGenerationError("供应商图片下载重定向次数过多")
                location = response.headers.get("location")
                if not location:
                    raise ProviderGenerationError("供应商图片下载重定向缺少 Location")
                current_url = _validate_image_download_url(urljoin(current_url, location))
                redirects_followed += 1
                continue

            data = _limited_response_bytes(
                response, settings.generated_asset_max_bytes, "供应商返回的图片"
            )
            if not response.is_success:
                raise ProviderGenerationError(f"下载供应商图片失败 ({response.status_code})")
            declared = response.headers.get("content-type", "").split(";", 1)[0]
            return data, declared


def _provider_body_text(body: bytes) -> str:
    return body.decode("utf-8-sig", errors="replace").strip()


def _provider_json_payload(body: bytes) -> object | None:
    """Parse JSON and SSE-style `data: {...}` responses from async gateways."""

    text = _provider_body_text(body)
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, UnicodeDecodeError):
        pass

    candidates: list[object] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue
        try:
            candidates.append(json.loads(line))
        except (ValueError, UnicodeDecodeError):
            continue
    return candidates[-1] if candidates else None


def _provider_body_preview(body: bytes, limit: int = 400) -> str:
    text = _provider_body_text(body)
    if not text:
        return "空响应"
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[已隐藏]", text)
    text = re.sub(r"(?i)(api[_-]?key\s*[:=]\s*)[^,\s}\"']+", r"\1[已隐藏]", text)
    return text[:limit]


def _provider_non_json_error(
    label: str, response: httpx.Response, body: bytes
) -> ProviderGenerationError:
    content_type = response.headers.get("content-type", "未知")
    return ProviderGenerationError(
        f"{label}（HTTP {response.status_code}，Content-Type={content_type}）："
        f"{_provider_body_preview(body)}"
    )


def _provider_json_response(client: httpx.Client, endpoint: str, **kwargs) -> dict:
    with client.stream("POST", endpoint, **kwargs) as response:
        body = _limited_response_bytes(
            response, settings.provider_response_max_bytes, "供应商响应"
        )
        if not response.is_success:
            raise ProviderHTTPError(
                _provider_error(response.status_code, body),
                retryable=response.status_code == 429 or response.status_code >= 500,
            )
    payload = _provider_json_payload(body)
    if payload is None:
        raise _provider_non_json_error("供应商返回的不是 JSON", response, body)
    if not isinstance(payload, dict):
        raise ProviderGenerationError("供应商响应格式无效")
    return payload


def _parse_size(parameters: dict) -> tuple[int | None, int | None]:
    value = parameters.get("size")
    match = re.fullmatch(r"(\d{1,5})x(\d{1,5})", value) if isinstance(value, str) else None
    return (int(match.group(1)), int(match.group(2))) if match else (None, None)


def _safe_result(payload: dict, output_count: int) -> dict:
    result = {"output_count": output_count}
    for key in ("created", "usage"):
        if key in payload:
            result[key] = payload[key]
    return result


def provider_http_client(timeout: httpx.Timeout) -> httpx.Client:
    return httpx.Client(timeout=timeout)


def _mark_failed(task_id: UUID, message: str) -> None:
    with SessionLocal() as db:
        task = db.get(GenerationTask, task_id)
        if task is None or task.status in {"succeeded", "cancelled"}:
            return
        task.status = "failed"
        task.error = message.strip()[:2000] or "生成任务失败"
        task.completed_at = datetime.now(timezone.utc)
        db.commit()


def _cleanup_written_objects(written_objects: list[tuple[object, str]]) -> None:
    for storage, storage_key in written_objects:
        try:
            storage.delete_object(storage_key)
        except Exception:
            logger.exception("Failed to clean up generated asset %s", storage_key)


def _stage_edit_image(
    db,
    task: GenerationTask,
    reference: dict,
    destination_dir: Path,
    remaining_bytes: int,
    ordinal: int,
) -> tuple[Path, str, str, int]:
    try:
        asset_id = UUID(str(reference.get("asset_id")))
    except (TypeError, ValueError):
        raise ProviderGenerationError("图片编辑输入引用无效") from None
    asset = db.get(Asset, asset_id)
    if (
        asset is None
        or asset.owner_id != task.owner_id
        or asset.project_id != task.project_id
        or asset.kind != "image"
        or asset.status != "ready"
        or asset.deleted_at is not None
        or not asset.storage_key
        or asset.mime_type not in {"image/png", "image/jpeg", "image/webp"}
    ):
        raise ProviderGenerationError("图片编辑输入资产不可用")
    if asset.byte_size > remaining_bytes:
        raise ProviderGenerationError("图片编辑输入总大小超过限制")
    storage = storage_for_asset(asset.asset_metadata)
    try:
        storage.validate_project_key(asset.storage_key, task.owner_id, task.project_id)
    except ValueError as error:
        raise ProviderGenerationError("图片编辑输入资产不属于当前项目") from error
    destination_path = destination_dir / f"input-{ordinal}.bin"
    written = storage.copy_object_to_file(asset.storage_key, destination_path, remaining_bytes)
    if written != asset.byte_size:
        destination_path.unlink(missing_ok=True)
        raise ProviderGenerationError("图片编辑输入资产大小不匹配")
    with destination_path.open("rb") as source:
        header = source.read(16)
    mime_type, extension = _image_format(header)
    if mime_type != asset.mime_type:
        destination_path.unlink(missing_ok=True)
        raise ProviderGenerationError("图片编辑输入资产类型不匹配")
    return destination_path, mime_type, f"input-{ordinal + 1}.{extension}", written


def _persist_video_progress(task_id: UUID, lease_token: UUID, **values) -> bool:
    with SessionLocal.begin() as db:
        task = GenerationTaskRepository(db).leased_task(task_id, lease_token, lock=True)
        if task is None:
            return False
        for key, value in values.items():
            setattr(task, key, value)
        return True


def _release_task_lease(task: GenerationTask) -> None:
    task.lease_token = None
    task.lease_owner = None
    task.lease_expires_at = None


def _image_spool_dir() -> Path:
    path = Path(settings.generated_storage_root) / ".worker-spool" / "images"
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _validated_spool_path(value: str | None) -> Path:
    if not value:
        raise ProviderGenerationError("图片任务缺少临时供应商响应")
    root = _image_spool_dir()
    path = Path(value).resolve()
    if path.parent != root or path.suffix != ".json":
        raise ProviderGenerationError("图片任务临时响应路径无效")
    return path


def _stream_provider_response_to_file(
    client: httpx.Client, endpoint: str, destination: Path, **kwargs
) -> None:
    partial = destination.with_suffix(".part")
    partial.unlink(missing_ok=True)
    try:
        with client.stream("POST", endpoint, **kwargs) as response:
            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > settings.provider_response_max_bytes:
                        raise ProviderGenerationError("供应商响应超过大小限制")
                except ValueError:
                    pass
            total = 0
            with partial.open("wb") as output:
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > settings.provider_response_max_bytes:
                        raise ProviderGenerationError("供应商响应超过大小限制")
                    output.write(chunk)
            if not response.is_success:
                body = partial.read_bytes()
                raise ProviderHTTPError(
                    _provider_error(response.status_code, body),
                    retryable=response.status_code in {429, 502, 503, 504},
                )
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)


def _retry_image_result(task_id: UUID, lease_token: UUID, message: str) -> bool:
    with SessionLocal.begin() as db:
        task = GenerationTaskRepository(db).leased_task(task_id, lease_token, lock=True)
        if task is None:
            return False
        now = datetime.now(timezone.utc)
        task.poll_error_count += 1
        task.last_provider_error = message.strip()[:2000]
        task.error = message.strip()[:2000]
        if task.poll_error_count >= settings.provider_retry_max_attempts:
            task.status = "failed"
            task.execution_phase = "failed"
            task.completed_at = now
            task.temporary_result_path = None
            _release_task_lease(task)
            return True
        delay = min(
            settings.provider_retry_max_delay_seconds,
            max(1, 2**task.poll_error_count),
        ) + random.uniform(0, 2)
        task.next_attempt_at = now + timedelta(seconds=delay)
        _release_task_lease(task)
        return False


def process_image_provider_request(task_id: UUID, lease_token: UUID) -> None:
    """Call the image provider and hand its bounded response to the result pool."""
    input_temp_dir = tempfile.TemporaryDirectory(prefix=f"jjbonetv-image-input-{task_id}-")
    spool_path = _image_spool_dir() / f"{task_id}-{lease_token}.json"
    provider_started = False
    try:
        with SessionLocal.begin() as db:
            task = GenerationTaskRepository(db).leased_task(task_id, lease_token)
            if task is None or task.kind != "image":
                return
            owner_id = task.owner_id
            project_id = task.project_id
            try:
                provider = ModelCatalogService(db).resolve_provider_configuration(
                    owner_id, UUID(task.model)
                )
            except (ValueError, ResourceNotFoundError) as error:
                raise ProviderGenerationError("模型已停用或 API Key 已删除") from error
            if (provider.get("metadata") or {}).get("protocol") != "openai_images":
                raise ProviderGenerationError("当前模型尚未配置可用的图片生成协议")
            provider["api_key"] = validate_http_api_key(provider["api_key"])
            supplied_parameters, input_references = split_generation_parameters(task.parameters)
            parameters = frozen_generation_parameters(supplied_parameters, provider)
            validate_provider_input_support(provider, input_references)
            effective_prompt = effective_generation_prompt("image", task.prompt, input_references)
            validate_required_provider_prompt(provider, effective_prompt, input_references)
            image_references = [
                reference for reference in input_references if reference.get("kind") == "image"
            ]
            staged_images = []
            if image_references:
                form_parameters = multipart_scalar_parameters(parameters)
                _, image_field_name = image_edit_transport(provider, len(image_references))
                remaining_bytes = settings.generation_input_images_max_total_bytes
                for ordinal, reference in enumerate(image_references):
                    staged = _stage_edit_image(
                        db,
                        task,
                        reference,
                        Path(input_temp_dir.name),
                        remaining_bytes,
                        ordinal,
                    )
                    staged_images.append(staged[:3])
                    remaining_bytes -= staged[3]
            else:
                provider_prompt("", input_references)
                form_parameters = {}
                image_field_name = "image"
            task.parameters = stored_generation_parameters(parameters, input_references)
            task.prompt = effective_prompt

        is_edit = bool(image_references)
        endpoint = (
            image_edit_endpoint(provider["base_url"], provider["metadata"])
            if is_edit
            else image_generation_endpoint(provider["base_url"], provider["metadata"])
        )
        timeout = httpx.Timeout(
            settings.provider_read_timeout_seconds,
            connect=settings.provider_connect_timeout_seconds,
        )
        if not mark_provider_started(task_id, lease_token):
            return
        provider_started = True
        with lease_heartbeat(task_id, lease_token) as lease_lost:
            with provider_http_client(timeout) as client, ExitStack() as file_stack:
                request_kwargs = (
                    {
                        "headers": {"Authorization": f"Bearer {provider['api_key']}"},
                        "data": {
                            "model": provider["model_name"],
                            "prompt": effective_prompt,
                            **form_parameters,
                        },
                        "files": [
                            (
                                image_field_name,
                                (filename, file_stack.enter_context(path.open("rb")), mime_type),
                            )
                            for path, mime_type, filename in staged_images
                        ],
                    }
                    if is_edit
                    else {
                        "headers": {
                            "Authorization": f"Bearer {provider['api_key']}",
                            "Content-Type": "application/json",
                        },
                        "json": {
                            "model": provider["model_name"],
                            "prompt": effective_prompt,
                            **parameters,
                        },
                    }
                )
                _stream_provider_response_to_file(client, endpoint, spool_path, **request_kwargs)
            if lease_lost.is_set():
                spool_path.unlink(missing_ok=True)
                return
        with SessionLocal.begin() as db:
            task = GenerationTaskRepository(db).leased_task(task_id, lease_token, lock=True)
            if task is None:
                spool_path.unlink(missing_ok=True)
                return
            task.execution_phase = "image_result_processing"
            task.temporary_result_path = str(spool_path)
            task.result = {**(task.result or {}), "provider_response_received": True}
            _release_task_lease(task)
    except ProviderHTTPError as error:
        spool_path.unlink(missing_ok=True)
        if error.retryable:
            retry_task(task_id, lease_token, str(error))
        else:
            fail_task(task_id, lease_token, str(error))
    except (httpx.ConnectError, httpx.ConnectTimeout):
        spool_path.unlink(missing_ok=True)
        retry_task(task_id, lease_token, "连接模型供应商失败")
    except httpx.HTTPError:
        spool_path.unlink(missing_ok=True)
        fail_task(
            task_id,
            lease_token,
            "供应商连接中断，结果未知，请重新发起",
            outcome_unknown=provider_started,
        )
    except (DomainValidationError, ProviderGenerationError) as error:
        spool_path.unlink(missing_ok=True)
        fail_task(task_id, lease_token, str(error))
    except Exception:
        spool_path.unlink(missing_ok=True)
        logger.exception("Image provider request %s failed", task_id)
        fail_task(
            task_id,
            lease_token,
            "图片生成服务发生内部错误",
            outcome_unknown=provider_started,
        )
    finally:
        input_temp_dir.cleanup()


def process_image_result(task_id: UUID, lease_token: UUID) -> None:
    """Decode/download and persist one already-received image provider response."""
    written_objects: list[tuple[object, str]] = []
    spool_path: Path | None = None
    terminal = False
    try:
        with SessionLocal.begin() as db:
            task = GenerationTaskRepository(db).leased_task(task_id, lease_token)
            if task is None or task.execution_phase != "image_result_processing":
                return
            owner_id, project_id = task.owner_id, task.project_id
            provider = ModelCatalogService(db).resolve_provider_configuration(
                owner_id, UUID(task.model)
            )
            provider["api_key"] = validate_http_api_key(provider["api_key"])
            supplied_parameters, _ = split_generation_parameters(task.parameters)
            parameters = frozen_generation_parameters(supplied_parameters, provider)
            spool_path = _validated_spool_path(task.temporary_result_path)
        body = spool_path.read_bytes()
        if len(body) > settings.provider_response_max_bytes:
            raise ProviderGenerationError("供应商响应超过大小限制")
        payload = _provider_json_payload(body)
        if not isinstance(payload, dict):
            raise ProviderGenerationError("供应商响应格式无效")
        items = payload.get("data")
        if not isinstance(items, list) or not items:
            raise ProviderGenerationError("供应商响应中没有图片结果")
        if len(items) > settings.provider_max_outputs:
            raise ProviderGenerationError("供应商返回的图片数量超过限制")
        width, height = _parse_size(parameters)
        storage = generation_asset_storage()
        prepared_outputs = []
        timeout = httpx.Timeout(
            settings.provider_read_timeout_seconds,
            connect=settings.provider_connect_timeout_seconds,
        )
        with lease_heartbeat(task_id, lease_token) as lease_lost:
            with provider_http_client(timeout) as client:
                for ordinal, item in enumerate(items):
                    if not isinstance(item, dict):
                        raise ProviderGenerationError("供应商图片结果格式无效")
                    if isinstance(item.get("b64_json"), str):
                        data = _decode_base64_image(item["b64_json"])
                        declared_type = None
                    elif isinstance(item.get("url"), str):
                        data, declared_type = _download_image(client, item["url"])
                    else:
                        raise ProviderGenerationError("供应商图片结果缺少 url 或 b64_json")
                    mime_type, extension = _image_format(data, declared_type)
                    storage_key = storage.object_key(
                        f"{owner_id}/{project_id}/{task_id}/{lease_token}-{ordinal}.{extension}"
                    )
                    storage.put_object(storage_key, data, mime_type)
                    written_objects.append((storage, storage_key))
                    prepared_outputs.append(
                        (ordinal, item, data, mime_type, extension, storage_key)
                    )
            if lease_lost.is_set():
                _cleanup_written_objects(written_objects)
                return
        with SessionLocal.begin() as db:
            task = GenerationTaskRepository(db).leased_task(task_id, lease_token, lock=True)
            if task is None:
                _cleanup_written_objects(written_objects)
                return
            for ordinal, item, data, mime_type, extension, storage_key in prepared_outputs:
                asset = Asset(
                    owner_id=owner_id,
                    project_id=project_id,
                    kind="image",
                    source="generation",
                    original_name=f"{provider['model_name']}-{ordinal}.{extension}",
                    storage_key=storage_key,
                    mime_type=mime_type,
                    byte_size=len(data),
                    width=width,
                    height=height,
                    checksum_sha256=hashlib.sha256(data).hexdigest(),
                    status="ready",
                    asset_metadata={
                        **storage_metadata(storage),
                        "model_id": str(provider["model_id"]),
                        "model_name": provider["model_name"],
                        "revised_prompt": item.get("revised_prompt"),
                        "generation_lease_token": str(lease_token),
                    },
                )
                db.add(asset)
                db.flush()
                db.add(GenerationOutput(generation_task_id=task.id, asset_id=asset.id, ordinal=ordinal))
            task.status = "succeeded"
            task.execution_phase = "succeeded"
            task.result = _safe_result(payload, len(items))
            task.completed_at = datetime.now(timezone.utc)
            task.temporary_result_path = None
            _release_task_lease(task)
            terminal = True
    except (httpx.HTTPError, AssetStorageError) as error:
        _cleanup_written_objects(written_objects)
        logger.warning("Image result %s will retry after local/provider download error", task_id)
        terminal = _retry_image_result(task_id, lease_token, str(error))
    except (DomainValidationError, ResourceNotFoundError, ProviderGenerationError, ValueError) as error:
        _cleanup_written_objects(written_objects)
        fail_task(task_id, lease_token, str(error))
        terminal = True
    except Exception as error:
        _cleanup_written_objects(written_objects)
        logger.exception("Image result %s failed", task_id)
        terminal = _retry_image_result(
            task_id,
            lease_token,
            f"图片结果处理失败: {type(error).__name__}",
        )
    finally:
        if terminal and spool_path is not None:
            spool_path.unlink(missing_ok=True)


def cleanup_orphaned_image_spool_files() -> int:
    root = _image_spool_dir()
    now = datetime.now(timezone.utc).timestamp()
    cleanup_grace_seconds = max(
        60.0,
        float(settings.provider_read_timeout_seconds) + float(settings.generation_lease_seconds),
    )
    with SessionLocal() as db:
        referenced = {
            str(Path(value).resolve())
            for value in db.scalars(
                select(GenerationTask.temporary_result_path).where(
                    GenerationTask.status == "running",
                    GenerationTask.execution_phase == "image_result_processing",
                    GenerationTask.temporary_result_path.is_not(None),
                )
            )
            if value
        }
    removed = 0
    for path in root.iterdir():
        if not path.is_file():
            continue
        is_orphan = path.suffix == ".part" or str(path.resolve()) not in referenced
        if not is_orphan:
            continue
        try:
            age_seconds = now - path.stat().st_mtime
        except FileNotFoundError:
            continue
        if age_seconds < cleanup_grace_seconds:
            continue
        path.unlink(missing_ok=True)
        removed += 1
    return removed


def video_poll_delay_seconds(submitted_at: datetime, now: datetime | None = None) -> float:
    current = now or datetime.now(timezone.utc)
    elapsed = max(0.0, (current - submitted_at).total_seconds())
    base = 5.0 if elapsed < 120 else 15.0 if elapsed < 600 else 30.0
    return base + random.uniform(0, 2)


def _provider_error_backoff(error_count: int) -> float:
    return min(
        settings.provider_retry_max_delay_seconds,
        max(5, 2 ** min(6, error_count + 1)),
    ) + random.uniform(0, 2)


def _reschedule_video_poll(
    task_id: UUID,
    lease_token: UUID,
    *,
    delay_seconds: float,
    error: str | None = None,
    result: dict | None = None,
) -> None:
    with SessionLocal.begin() as db:
        task = GenerationTaskRepository(db).leased_task(task_id, lease_token, lock=True)
        if task is None:
            return
        task.execution_phase = "video_provider_processing"
        task.next_poll_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        if result is not None:
            task.result = result
        if error:
            task.poll_error_count += 1
            task.last_provider_error = error.strip()[:2000]
            task.error = error.strip()[:2000]
        else:
            task.error = None
            task.last_provider_error = None
        _release_task_lease(task)


def _video_provider_context(task_id: UUID, lease_token: UUID) -> dict | None:
    with SessionLocal.begin() as db:
        task = GenerationTaskRepository(db).leased_task(task_id, lease_token)
        if task is None or task.kind != "video":
            return None
        owner_id, project_id = task.owner_id, task.project_id
        provider = ModelCatalogService(db).resolve_provider_configuration(
            owner_id, UUID(task.model)
        )
        provider["api_key"] = validate_http_api_key(provider["api_key"])
        supplied_parameters, input_references = split_generation_parameters(task.parameters)
        parameters = frozen_generation_parameters(supplied_parameters, provider)
        effective_prompt = effective_generation_prompt("video", task.prompt, input_references)
        validate_video_input_references(provider.get("metadata"), input_references)
        validate_video_prompt(provider.get("metadata"), effective_prompt)
        task.parameters = stored_generation_parameters(parameters, input_references)
        task.prompt = effective_prompt
        refs_by_kind: dict[str, list[str]] = {"image": [], "video": [], "audio": []}
        for reference in input_references:
            if reference.get("kind") not in refs_by_kind:
                continue
            asset = db.get(Asset, UUID(str(reference.get("asset_id"))))
            if asset is None or asset.owner_id != owner_id or asset.project_id != project_id:
                raise ProviderGenerationError("视频参考素材不可用")
            refs_by_kind[reference["kind"]].append(_asset_reference_url(asset))
        return {
            "owner_id": owner_id,
            "project_id": project_id,
            "provider": provider,
            "parameters": parameters,
            "prompt": effective_prompt,
            "references": refs_by_kind,
            "provider_task_id": task.provider_task_id,
            "provider_submitted_at": task.provider_submitted_at,
            "provider_deadline_at": task.provider_deadline_at,
            "poll_error_count": task.poll_error_count,
            "result": task.result or {},
        }


def process_video_submission(task_id: UUID, lease_token: UUID) -> None:
    """Create one provider-side video task, then release the submit slot."""
    provider_started = False
    try:
        context = _video_provider_context(task_id, lease_token)
        if context is None:
            return
        if context["provider_task_id"]:
            _reschedule_video_poll(task_id, lease_token, delay_seconds=0)
            return
        provider = context["provider"]
        endpoint = video_endpoint(provider["base_url"], provider.get("metadata") or {})
        headers = {
            "Authorization": f"Bearer {provider['api_key']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Idempotency-Key": str(task_id),
            "User-Agent": "JJboneTv-video-worker/2.0",
        }
        payload_body = {
            "model": provider["model_name"],
            "prompt": context["prompt"],
            **context["parameters"],
            **(
                {"referenceImages": context["references"]["image"]}
                if context["references"]["image"]
                else {}
            ),
            **(
                {"referenceVideos": context["references"]["video"]}
                if context["references"]["video"]
                else {}
            ),
            **(
                {"referenceAudios": context["references"]["audio"]}
                if context["references"]["audio"]
                else {}
            ),
        }
        timeout = httpx.Timeout(
            settings.provider_read_timeout_seconds,
            connect=settings.provider_connect_timeout_seconds,
        )
        if not mark_provider_started(task_id, lease_token):
            return
        provider_started = True
        with lease_heartbeat(task_id, lease_token) as lease_lost:
            with provider_http_client(timeout) as client:
                payload = _provider_video_create_response(
                    client, endpoint, headers=headers, json=payload_body
                )
            if lease_lost.is_set():
                return
        provider_task_id = _video_task_id(payload)
        if not provider_task_id:
            raise ProviderGenerationError("供应商未返回视频任务 ID")
        submitted_at = datetime.now(timezone.utc)
        with SessionLocal.begin() as db:
            task = GenerationTaskRepository(db).leased_task(task_id, lease_token, lock=True)
            if task is None:
                return
            task.provider_task_id = provider_task_id
            task.provider_submitted_at = submitted_at
            task.provider_deadline_at = submitted_at + timedelta(
                seconds=settings.video_provider_timeout_seconds
            )
            task.next_poll_at = submitted_at + timedelta(
                seconds=video_poll_delay_seconds(submitted_at, submitted_at)
            )
            task.execution_phase = "video_provider_processing"
            task.result = {"provider_status": _video_status(payload)}
            task.error = None
            _release_task_lease(task)
    except ProviderHTTPError as error:
        if error.retryable:
            retry_task(task_id, lease_token, str(error))
        else:
            fail_task(task_id, lease_token, str(error))
    except httpx.ConnectError:
        retry_task(task_id, lease_token, "连接视频模型供应商失败")
    except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.HTTPError):
        fail_task(
            task_id,
            lease_token,
            "视频创建请求结果未知，已停止自动重试",
            outcome_unknown=provider_started,
        )
    except (DomainValidationError, ResourceNotFoundError, ProviderGenerationError, ValueError) as error:
        fail_task(task_id, lease_token, str(error))
    except Exception:
        logger.exception("Video submission %s failed", task_id)
        fail_task(
            task_id,
            lease_token,
            "视频创建服务发生内部错误",
            outcome_unknown=provider_started,
        )


def process_video_poll(task_id: UUID, lease_token: UUID) -> None:
    """Perform exactly one status query and release the poll slot."""
    temp_dir = tempfile.TemporaryDirectory(prefix=f"jjbonetv-video-{task_id}-")
    written_objects: list[tuple[object, str]] = []
    poll_error_count = 0
    try:
        context = _video_provider_context(task_id, lease_token)
        if context is None:
            return
        poll_error_count = int(context.get("poll_error_count") or 0)
        provider_task_id = context["provider_task_id"]
        submitted_at = context["provider_submitted_at"] or datetime.now(timezone.utc)
        deadline = context["provider_deadline_at"] or (
            submitted_at + timedelta(seconds=settings.video_provider_timeout_seconds)
        )
        now = datetime.now(timezone.utc)
        if now >= deadline:
            fail_task(
                task_id,
                lease_token,
                "视频供应商处理超过 60 分钟，结果未知，请重新发起",
                outcome_unknown=True,
            )
            return
        provider = context["provider"]
        status_url = video_endpoint(
            provider["base_url"], provider.get("metadata") or {}, f"/{provider_task_id}"
        )
        content_url = video_endpoint(
            provider["base_url"],
            provider.get("metadata") or {},
            f"/{provider_task_id}/content",
        )
        headers = {
            "Authorization": f"Bearer {provider['api_key']}",
            "Accept": "application/json",
            "User-Agent": "JJboneTv-video-worker/2.0",
        }
        timeout = httpx.Timeout(
            settings.video_poll_read_timeout_seconds,
            connect=settings.video_poll_connect_timeout_seconds,
        )
        with lease_heartbeat(task_id, lease_token) as lease_lost:
            with provider_http_client(timeout) as client:
                with client.stream("GET", status_url, headers=headers) as response:
                    body = _limited_response_bytes(
                        response, settings.provider_response_max_bytes, "视频任务状态"
                    )
                    if not response.is_success:
                        raise ProviderHTTPError(
                            _provider_error(response.status_code, body),
                            retryable=response.status_code in {429, 502, 503, 504},
                        )
                status_payload = _provider_video_status_response(response, body)
                status_value = _video_status(status_payload)
                if status_value in {"failed", "error", "cancelled", "canceled"}:
                    raise ProviderGenerationError(
                        str(
                            status_payload.get("error")
                            or status_payload.get("message")
                            or "视频生成失败"
                        )
                    )
                if status_value not in {"completed", "succeeded", "success", "done"}:
                    _reschedule_video_poll(
                        task_id,
                        lease_token,
                        delay_seconds=video_poll_delay_seconds(submitted_at),
                        result={**status_payload, "provider_status": status_value},
                    )
                    return
                destination = Path(temp_dir.name) / "output.mp4"
                total = _download_video_content(
                    client,
                    content_url,
                    destination,
                    {
                        "Authorization": f"Bearer {provider['api_key']}",
                        "Accept": "video/mp4,application/octet-stream,application/json",
                        "User-Agent": "JJboneTv-video-worker/2.0",
                    },
                )
            if lease_lost.is_set():
                return
        if total == 0:
            raise ProviderGenerationError("供应商返回了空视频")
        try:
            inspected_video = inspect_media_file(destination, "video")
        except MediaProbeError as error:
            raise ProviderGenerationError("供应商返回的视频文件无效") from error
        storage = generation_asset_storage()
        storage_key = storage.object_key(
            f"{context['owner_id']}/{context['project_id']}/{task_id}-{provider_task_id}.mp4"
        )
        storage.put_file(storage_key, destination, "video/mp4")
        written_objects.append((storage, storage_key))
        with SessionLocal.begin() as db:
            task = GenerationTaskRepository(db).leased_task(task_id, lease_token, lock=True)
            if task is None:
                _cleanup_written_objects(written_objects)
                return
            asset = Asset(
                owner_id=context["owner_id"],
                project_id=context["project_id"],
                kind="video",
                source="generation",
                original_name=f"{provider['model_name']}.mp4",
                storage_key=storage_key,
                mime_type="video/mp4",
                byte_size=total,
                width=inspected_video.width,
                height=inspected_video.height,
                duration_ms=inspected_video.duration_ms,
                status="ready",
                asset_metadata={
                    **storage_metadata(storage),
                    "model_id": str(provider["model_id"]),
                    "model_name": provider["model_name"],
                    "provider_task_id": provider_task_id,
                },
            )
            db.add(asset)
            db.flush()
            db.add(GenerationOutput(generation_task_id=task.id, asset_id=asset.id, ordinal=0))
            task.status = "succeeded"
            task.execution_phase = "succeeded"
            task.result = {
                **(task.result or {}),
                "provider_status": "completed",
                "output_count": 1,
            }
            task.completed_at = datetime.now(timezone.utc)
            task.next_poll_at = None
            task.error = None
            _release_task_lease(task)
    except ProviderHTTPError as error:
        _cleanup_written_objects(written_objects)
        _reschedule_video_poll(
            task_id,
            lease_token,
            delay_seconds=_provider_error_backoff(poll_error_count),
            error=str(error),
        )
    except (httpx.HTTPError, AssetStorageError) as error:
        _cleanup_written_objects(written_objects)
        _reschedule_video_poll(
            task_id,
            lease_token,
            delay_seconds=_provider_error_backoff(poll_error_count),
            error=str(error),
        )
    except ProviderGenerationError as error:
        _cleanup_written_objects(written_objects)
        fail_task(task_id, lease_token, str(error))
    except Exception as error:
        _cleanup_written_objects(written_objects)
        logger.exception("Video poll %s failed", task_id)
        _reschedule_video_poll(
            task_id,
            lease_token,
            delay_seconds=min(settings.provider_retry_max_delay_seconds, 30),
            error=f"视频状态查询失败: {type(error).__name__}",
        )
    finally:
        temp_dir.cleanup()


def _process_video_generation_task(task_id: UUID, lease_token: UUID) -> None:
    """Submit and durably poll a provider's asynchronous /videos task."""
    temp_dir = tempfile.TemporaryDirectory(prefix=f"thinktv-video-{task_id}-")
    written_objects: list[tuple[object, str]] = []
    try:
        with SessionLocal.begin() as db:
            task = GenerationTaskRepository(db).leased_task(task_id, lease_token, lock=True)
            if task is None:
                return
            owner_id, project_id = task.owner_id, task.project_id
            model_id = UUID(task.model)
            provider = ModelCatalogService(db).resolve_provider_configuration(owner_id, model_id)
            provider["api_key"] = validate_http_api_key(provider["api_key"])
            supplied_parameters, input_references = split_generation_parameters(task.parameters)
            parameters = frozen_generation_parameters(supplied_parameters, provider)
            effective_prompt = effective_generation_prompt("video", task.prompt, input_references)
            validate_video_input_references(provider.get("metadata"), input_references)
            validate_video_prompt(provider.get("metadata"), effective_prompt)
            task.parameters = stored_generation_parameters(parameters, input_references)
            task.prompt = effective_prompt
            refs_by_kind: dict[str, list[str]] = {"image": [], "video": [], "audio": []}
            for reference in input_references:
                if reference.get("kind") in refs_by_kind:
                    asset = db.get(Asset, UUID(str(reference.get("asset_id"))))
                    if asset is None or asset.owner_id != owner_id or asset.project_id != project_id:
                        raise ProviderGenerationError("视频参考素材不可用")
                    refs_by_kind[reference["kind"]].append(_asset_reference_url(asset))
            endpoint = video_endpoint(provider["base_url"], provider.get("metadata") or {})
            headers = {
                "Authorization": f"Bearer {provider['api_key']}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "ThinkTV-video-worker/1.0",
            }

        timeout = httpx.Timeout(
            settings.provider_read_timeout_seconds,
            connect=settings.provider_connect_timeout_seconds,
        )
        with lease_heartbeat(task_id, lease_token) as lease_lost:
            with provider_http_client(timeout) as client:
                provider_task_id = None
                with SessionLocal() as db:
                    current = db.get(GenerationTask, task_id)
                    provider_task_id = current.provider_task_id if current else None
                if not provider_task_id:
                    payload = _provider_video_create_response(
                        client,
                        endpoint,
                        headers=headers,
                        json={
                            "model": provider["model_name"],
                            "prompt": effective_prompt,
                            **parameters,
                            **(
                                {"referenceImages": refs_by_kind["image"]}
                                if refs_by_kind["image"]
                                else {}
                            ),
                            **(
                                {"referenceVideos": refs_by_kind["video"]}
                                if refs_by_kind["video"]
                                else {}
                            ),
                            **(
                                {"referenceAudios": refs_by_kind["audio"]}
                                if refs_by_kind["audio"]
                                else {}
                            ),
                        },
                    )
                    provider_task_id = _video_task_id(payload)
                    if not provider_task_id:
                        raise ProviderGenerationError("供应商未返回视频任务 ID")
                    if not _persist_video_progress(
                        task_id,
                        lease_token,
                        provider_task_id=provider_task_id,
                        result={"provider_status": _video_status(payload)},
                        provider_request_started_at=datetime.now(timezone.utc),
                    ):
                        return

                status_url = video_endpoint(
                    provider["base_url"], provider.get("metadata") or {}, f"/{provider_task_id}"
                )
                content_url = video_endpoint(
                    provider["base_url"],
                    provider.get("metadata") or {},
                    f"/{provider_task_id}/content",
                )
                last_status = None
                while True:
                    if lease_lost.is_set():
                        return
                    with client.stream(
                        "GET",
                        status_url,
                        headers={
                            "Authorization": f"Bearer {provider['api_key']}",
                            "Accept": "application/json",
                            "User-Agent": "ThinkTV-video-worker/1.0",
                        },
                    ) as response:
                        body = _limited_response_bytes(response, settings.provider_response_max_bytes, "视频任务状态")
                        if not response.is_success:
                            raise ProviderHTTPError(
                                _provider_error(response.status_code, body),
                                retryable=response.status_code >= 500 or response.status_code == 429,
                            )
                    status_payload = _provider_video_status_response(response, body)
                    status_value = _video_status(status_payload)
                    if status_value != last_status:
                        last_status = status_value
                        _persist_video_progress(
                            task_id,
                            lease_token,
                            result={
                                **status_payload,
                                "provider_status": status_value,
                            },
                        )
                    if status_value in {"failed", "error", "cancelled", "canceled"}:
                        raise ProviderGenerationError(
                            str(
                                status_payload.get("error")
                                or status_payload.get("message")
                                or "视频生成失败"
                            )
                        )
                    if status_value in {"completed", "succeeded", "success", "done"}:
                        break
                    time.sleep(max(1.0, settings.provider_video_poll_seconds))

                destination = Path(temp_dir.name) / "output.mp4"
                total = _download_video_content(
                    client,
                    content_url,
                    destination,
                    {
                        "Authorization": f"Bearer {provider['api_key']}",
                        "Accept": "video/mp4,application/octet-stream,application/json",
                        "User-Agent": "ThinkTV-video-worker/1.0",
                    },
                )
                if total == 0:
                    raise ProviderGenerationError("供应商返回了空视频")
                try:
                    inspected_video = inspect_media_file(destination, "video")
                except MediaProbeError as error:
                    raise ProviderGenerationError("供应商返回的视频文件无效") from error
                storage = generation_asset_storage()
                storage_key = storage.object_key(
                    f"{owner_id}/{project_id}/{task_id}-{provider_task_id}.mp4"
                )
                storage.put_file(storage_key, destination, "video/mp4")
                written_objects.append((storage, storage_key))
                with SessionLocal.begin() as db:
                    task = GenerationTaskRepository(db).leased_task(task_id, lease_token, lock=True)
                    if task is None:
                        _cleanup_written_objects(written_objects)
                        return
                    asset = Asset(
                        owner_id=owner_id,
                        project_id=project_id,
                        kind="video",
                        source="generation",
                        original_name=f"{provider['model_name']}.mp4",
                        storage_key=storage_key,
                        mime_type="video/mp4",
                        byte_size=total,
                        width=inspected_video.width,
                        height=inspected_video.height,
                        duration_ms=inspected_video.duration_ms,
                        status="ready",
                        asset_metadata={
                            **storage_metadata(storage),
                            "model_id": str(provider["model_id"]),
                            "model_name": provider["model_name"],
                            "provider_task_id": provider_task_id,
                        },
                    )
                    db.add(asset)
                    db.flush()
                    db.add(GenerationOutput(generation_task_id=task.id, asset_id=asset.id, ordinal=0))
                    task.status = "succeeded"
                    task.result = {
                        **(task.result or {}),
                        "provider_status": "completed",
                        "output_count": 1,
                    }
                    task.completed_at = datetime.now(timezone.utc)
                    task.lease_token = task.lease_owner = task.lease_expires_at = None
    except ProviderHTTPError as error:
        _cleanup_written_objects(written_objects)
        if error.retryable:
            retry_task(task_id, lease_token, str(error))
        else:
            fail_task(task_id, lease_token, str(error))
    except (ProviderGenerationError, DomainValidationError) as error:
        _cleanup_written_objects(written_objects)
        fail_task(task_id, lease_token, str(error))
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as error:
        _cleanup_written_objects(written_objects)
        retry_task(task_id, lease_token, f"连接视频模型供应商失败: {error}")
    except Exception:
        _cleanup_written_objects(written_objects)
        logger.exception("Video generation task %s failed", task_id)
        fail_task(task_id, lease_token, "视频生成服务发生内部错误")
    finally:
        temp_dir.cleanup()


def process_claimed_generation_task(task_id: UUID, lease_token: UUID) -> None:
    written_objects: list[tuple[object, str]] = []
    input_temp_dir = tempfile.TemporaryDirectory(prefix=f"thinktv-input-{task_id}-")
    stage = "load_task"
    provider_started = False
    provider_responded = False
    try:
        with SessionLocal.begin() as db:
            task_kind = db.scalar(select(GenerationTask.kind).where(GenerationTask.id == task_id))
        if task_kind == "video":
            _process_video_generation_task(task_id, lease_token)
            return
        with SessionLocal.begin() as db:
            task = GenerationTaskRepository(db).leased_task(task_id, lease_token)
            if task is None:
                return
            owner_id = task.owner_id
            project_id = task.project_id
            task_kind = task.kind
            stage = "resolve_provider"
            try:
                model_id = UUID(task.model)
                provider = ModelCatalogService(db).resolve_provider_configuration(owner_id, model_id)
            except (ValueError, ResourceNotFoundError) as error:
                raise ProviderGenerationError("模型已停用或 API Key 已删除") from error
            if task_kind != "image" or (provider.get("metadata") or {}).get("protocol") != "openai_images":
                raise ProviderGenerationError("当前模型尚未配置可用的图片生成协议")
            try:
                provider["api_key"] = validate_http_api_key(provider["api_key"])
            except DomainValidationError as error:
                raise ProviderGenerationError(
                    "当前 API Key 格式无效，请在模型 API Key 页面重新配置"
                ) from error
            supplied_parameters, input_references = split_generation_parameters(task.parameters)
            # Parameters are merged, resolved and validated before the task is committed.
            # Reusing the persisted snapshot keeps queued work stable when an admin later
            # changes the model's defaults, schema or image-size matrix.
            parameters = frozen_generation_parameters(supplied_parameters, provider)
            validate_provider_input_support(provider, input_references)
            effective_prompt = effective_generation_prompt(task_kind, task.prompt, input_references)
            validate_required_provider_prompt(provider, effective_prompt, input_references)
            image_references = [
                reference for reference in input_references if reference.get("kind") == "image"
            ]
            if image_references:
                form_parameters = multipart_scalar_parameters(parameters)
                _, image_field_name = image_edit_transport(provider, len(image_references))
                stage = "read_input_asset"
                staged_images = []
                remaining_bytes = settings.generation_input_images_max_total_bytes
                for ordinal, reference in enumerate(image_references):
                    staged = _stage_edit_image(
                        db,
                        task,
                        reference,
                        Path(input_temp_dir.name),
                        remaining_bytes,
                        ordinal,
                    )
                    staged_images.append(staged[:3])
                    remaining_bytes -= staged[3]
            else:
                provider_prompt("", input_references)
                form_parameters = {}
                image_field_name = "image"
                staged_images = []
            task.parameters = stored_generation_parameters(parameters, input_references)
            task.prompt = effective_prompt

        stage = "resolve_storage"
        storage = generation_asset_storage()
        is_edit = bool(image_references)
        endpoint = (
            image_edit_endpoint(provider["base_url"], provider["metadata"])
            if is_edit
            else image_generation_endpoint(provider["base_url"], provider["metadata"])
        )
        timeout = httpx.Timeout(
            settings.provider_read_timeout_seconds,
            connect=settings.provider_connect_timeout_seconds,
        )
        headers = {"Authorization": f"Bearer {provider['api_key']}"}
        if not mark_provider_started(task_id, lease_token):
            return
        provider_started = True
        stage = "provider_request"
        with lease_heartbeat(task_id, lease_token) as lease_lost:
            with provider_http_client(timeout) as client:
                with ExitStack() as file_stack:
                    request_kwargs = (
                        {
                            "headers": headers,
                            "data": {
                                "model": provider["model_name"],
                                "prompt": effective_prompt,
                                **form_parameters,
                            },
                            "files": [
                                (
                                    image_field_name,
                                    (
                                        filename,
                                        file_stack.enter_context(path.open("rb")),
                                        mime_type,
                                    ),
                                )
                                for path, mime_type, filename in staged_images
                            ],
                        }
                        if is_edit
                        else {
                            "headers": {**headers, "Content-Type": "application/json"},
                            "json": {
                                "model": provider["model_name"],
                                "prompt": effective_prompt,
                                **parameters,
                            },
                        }
                    )
                    payload = _provider_json_response(client, endpoint, **request_kwargs)
                provider_responded = True
                items = payload.get("data")
                if not isinstance(items, list) or not items:
                    raise ProviderGenerationError("供应商响应中没有图片结果")
                if len(items) > settings.provider_max_outputs:
                    raise ProviderGenerationError("供应商返回的图片数量超过限制")
                width, height = _parse_size(parameters)
                prepared_outputs = []
                stage = "decode_output"
                for ordinal, item in enumerate(items):
                    if not isinstance(item, dict):
                        raise ProviderGenerationError("供应商图片结果格式无效")
                    if isinstance(item.get("b64_json"), str):
                        data = _decode_base64_image(item["b64_json"])
                        declared_type = None
                    elif isinstance(item.get("url"), str):
                        data, declared_type = _download_image(client, item["url"])
                    else:
                        raise ProviderGenerationError("供应商图片结果缺少 url 或 b64_json")
                    mime_type, extension = _image_format(data, declared_type)
                    relative_key = (
                        f"{owner_id}/{project_id}/{task_id}/{lease_token}-{ordinal}.{extension}"
                    )
                    storage_key = storage.object_key(relative_key)
                    stage = "write_asset"
                    storage.put_object(storage_key, data, mime_type)
                    written_objects.append((storage, storage_key))
                    prepared_outputs.append(
                        (ordinal, item, data, mime_type, extension, storage_key)
                    )
                if lease_lost.is_set():
                    _cleanup_written_objects(written_objects)
                    return

                stage = "db_commit"
                with SessionLocal.begin() as db:
                    task = GenerationTaskRepository(db).leased_task(
                        task_id, lease_token, lock=True
                    )
                    if task is None:
                        _cleanup_written_objects(written_objects)
                        return
                    for ordinal, item, data, mime_type, extension, storage_key in prepared_outputs:
                        asset = Asset(
                            owner_id=owner_id,
                            project_id=project_id,
                            kind="image",
                            source="generation",
                            text_content=None,
                            original_name=f"{provider['model_name']}-{ordinal}.{extension}",
                            storage_key=storage_key,
                            mime_type=mime_type,
                            byte_size=len(data),
                            width=width,
                            height=height,
                            checksum_sha256=hashlib.sha256(data).hexdigest(),
                            status="ready",
                            asset_metadata={
                                **storage_metadata(storage),
                                "model_id": str(provider["model_id"]),
                                "model_name": provider["model_name"],
                                "revised_prompt": item.get("revised_prompt"),
                                "generation_lease_token": str(lease_token),
                            },
                        )
                        db.add(asset)
                        db.flush()
                        db.add(
                            GenerationOutput(
                                generation_task_id=task.id,
                                asset_id=asset.id,
                                ordinal=ordinal,
                            )
                        )
                    task.status = "succeeded"
                    task.result = _safe_result(payload, len(items))
                    task.completed_at = datetime.now(timezone.utc)
                    task.lease_token = None
                    task.lease_owner = None
                    task.lease_expires_at = None
    except ProviderHTTPError as error:
        _cleanup_written_objects(written_objects)
        if error.retryable:
            retry_task(task_id, lease_token, str(error))
        else:
            fail_task(task_id, lease_token, str(error))
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _cleanup_written_objects(written_objects)
        retry_task(task_id, lease_token, "连接模型供应商失败")
    except httpx.HTTPError:
        _cleanup_written_objects(written_objects)
        fail_task(
            task_id,
            lease_token,
            "供应商连接中断，结果未知，请重新发起",
            outcome_unknown=provider_started,
        )
    except (DomainValidationError, ProviderGenerationError) as error:
        _cleanup_written_objects(written_objects)
        fail_task(task_id, lease_token, str(error))
    except AssetStorageError:
        _cleanup_written_objects(written_objects)
        logger.exception("Generation task %s could not persist its output", task_id)
        if provider_responded:
            fail_task(
                task_id,
                lease_token,
                "生成结果保存失败，供应商结果未知，请重新发起",
                outcome_unknown=True,
            )
        else:
            retry_task(task_id, lease_token, "生成资源存储暂时不可用")
    except Exception as error:
        _cleanup_written_objects(written_objects)
        logger.exception(
            "Generation task %s failed unexpectedly at stage %s (%s)",
            task_id,
            stage,
            type(error).__name__,
        )
        fail_task(
            task_id,
            lease_token,
            "生成服务发生内部错误",
            outcome_unknown=provider_started,
        )
    finally:
        input_temp_dir.cleanup()


def run_generation_task(task_id: UUID) -> None:
    """Compatibility entry point; production workers claim from the durable queue."""
    stage = "load_task"
    try:
        task = claim_specific_task(task_id, "compatibility-runner")
        if task is None or task.lease_token is None:
            return
        process_claimed_generation_task(task.id, task.lease_token)
    except Exception as error:
        logger.exception(
            "Generation task %s failed unexpectedly at stage %s (%s)",
            task_id,
            stage,
            type(error).__name__,
        )
        _mark_failed(task_id, "生成服务发生内部错误")
