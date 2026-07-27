import base64
import binascii
import hashlib
import json
import logging
import re
import tempfile
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

import httpx

from ..config import settings
from ..database import SessionLocal
from ..exceptions import DomainValidationError, ResourceNotFoundError
from ..models import Asset, GenerationOutput, GenerationTask
from ..repositories.generation_tasks import GenerationTaskRepository
from .asset_storage import (
    AssetStorageError,
    generation_asset_storage,
    storage_for_asset,
    storage_metadata,
)
from .model_catalog import ModelCatalogService, validate_http_api_key
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


logger = logging.getLogger(__name__)

INPUT_REFERENCES_PARAMETER = "_input_references"
OPENAI_IMAGE_EDIT_PROTOCOL = "openai_images_multipart"
PROTOCOLS_REQUIRING_PROMPT = {"openai_images"}
IMAGE_REFERENCE_TOKEN_RE = re.compile(
    r"@\{image:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\}"
)


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
    if kind != "image":
        compatible_client_prompt = client_prompt if kind == "text" else ""
        return resolved_generation_prompt(compatible_client_prompt, input_references)
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
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ProviderGenerationError("供应商返回了无效的图片地址")
    with client.stream("GET", url, follow_redirects=True) as response:
        data = _limited_response_bytes(
            response, settings.generated_asset_max_bytes, "供应商返回的图片"
        )
        if not response.is_success:
            raise ProviderGenerationError(f"下载供应商图片失败 ({response.status_code})")
        declared = response.headers.get("content-type", "").split(";", 1)[0]
        return data, declared


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
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as error:
        raise ProviderGenerationError("供应商返回的不是 JSON") from error
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


def process_claimed_generation_task(task_id: UUID, lease_token: UUID) -> None:
    written_objects: list[tuple[object, str]] = []
    input_temp_dir = tempfile.TemporaryDirectory(prefix=f"thinktv-input-{task_id}-")
    stage = "load_task"
    provider_started = False
    provider_responded = False
    try:
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
