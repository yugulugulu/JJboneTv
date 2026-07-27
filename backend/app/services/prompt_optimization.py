from urllib.parse import urlparse
from uuid import UUID

import httpx
from sqlalchemy.orm import Session

from ..config import settings
from ..exceptions import DomainValidationError, ResourceNotFoundError
from .model_catalog import ModelCatalogService, validate_http_api_key


SYSTEM_PROMPTS = {
    "image": (
        "你是图片生成提示词优化器。把用户输入改写为一段完整、具体、可直接用于图片生成模型的提示词。"
        "只输出最终提示词正文，不要标题、解释、多个方案、代码块、寒暄或追问；不得回答与图片提示词优化无关的问题。"
    ),
    "video": (
        "你是视频生成提示词优化器。把用户输入改写为一段完整、具体、可直接用于视频生成模型的提示词，"
        "明确主体、动作、场景、镜头、光线和节奏。只输出最终提示词正文，不要标题、解释、多个方案、"
        "代码块、寒暄或追问；不得回答与视频提示词优化无关的问题。"
    ),
}


class PromptOptimizationProviderError(Exception):
    def __init__(self, message: str, *, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def chat_completions_endpoint(base_url: str) -> str:
    value = base_url.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise DomainValidationError("文本模型 Base URL 必须是有效的 HTTP(S) 地址")
    if parsed.path.rstrip("/").endswith("/chat/completions"):
        return value
    return f"{value}/chat/completions"


def prompt_http_client(timeout: httpx.Timeout) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=False)


class PromptOptimizationService:
    def __init__(self, db: Session):
        self.db = db

    def optimize(
        self, user_id: UUID, model_id: UUID, target_kind: str, user_input: str
    ) -> str:
        if target_kind not in SYSTEM_PROMPTS:
            raise DomainValidationError("仅支持图片或视频提示词优化")
        try:
            provider = ModelCatalogService(self.db).resolve_provider_configuration(
                user_id, model_id
            )
        except ResourceNotFoundError:
            raise ResourceNotFoundError from None
        if provider["kind"] != "text":
            raise DomainValidationError("所选模型不是文本模型")
        if (provider.get("metadata") or {}).get("protocol", "chat_completions") != "chat_completions":
            raise DomainValidationError("所选文本模型不支持 Chat Completions API")

        endpoint = chat_completions_endpoint(provider["base_url"])
        api_key = validate_http_api_key(provider["api_key"])
        timeout = httpx.Timeout(
            settings.provider_read_timeout_seconds,
            connect=settings.provider_connect_timeout_seconds,
        )
        body = {
            "model": provider["model_name"],
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPTS[target_kind]},
                {"role": "user", "content": user_input.strip()},
            ],
        }
        try:
            with prompt_http_client(timeout) as client:
                response = client.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
        except httpx.TimeoutException:
            raise PromptOptimizationProviderError("文本模型响应超时", status_code=504) from None
        except httpx.HTTPError:
            raise PromptOptimizationProviderError("暂时无法连接文本模型服务") from None

        if response.status_code >= 400:
            raise PromptOptimizationProviderError(
                f"文本模型服务调用失败（HTTP {response.status_code}）"
            )
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise PromptOptimizationProviderError("文本模型返回了无法识别的响应") from None
        if not isinstance(content, str) or not content.strip():
            raise PromptOptimizationProviderError("文本模型未返回有效提示词")
        return content.strip()
