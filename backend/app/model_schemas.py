from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


ModelKind = Literal["text", "image", "video", "audio"]


class UserModelRead(BaseModel):
    id: UUID
    group_id: UUID
    model_name: str
    display_name: str
    name: str
    kind: ModelKind
    description: str
    display_order: int
    config_schema: dict
    default_parameters: dict
    metadata: dict
    enabled: bool = True


class UserModelList(BaseModel):
    items: list[UserModelRead]


class UserModelGroupRead(BaseModel):
    id: UUID
    name: str
    description: str
    display_order: int
    credential_configured: bool
    api_key_last_four: str | None = None
    credential_updated_at: datetime | None = None
    models: list[UserModelRead]


class UserModelGroupList(BaseModel):
    items: list[UserModelGroupRead]


class UserModelCredentialWrite(BaseModel):
    api_key: str = Field(min_length=4, max_length=4096)
    model_config = ConfigDict(extra="forbid")

    @field_validator("api_key")
    @classmethod
    def trim_api_key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("API Key cannot be blank")
        return value


class UserModelCredentialRead(BaseModel):
    group_id: UUID
    credential_configured: bool
    api_key_last_four: str | None = None
    credential_updated_at: datetime | None = None
