from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AdminLogin(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value):
        return value.strip().lower() if isinstance(value, str) else value


class AdminAccountRead(BaseModel):
    id: UUID
    email: str
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class AdminAuthResponse(BaseModel):
    admin: AdminAccountRead


class AdminUserRead(BaseModel):
    id: UUID
    email: str
    is_active: bool
    disabled_at: datetime | None
    disabled_by_admin_id: UUID | None
    created_at: datetime
    project_count: int
    folder_count: int
    asset_count: int


class AdminUserList(BaseModel):
    items: list[AdminUserRead]
    total: int
    page: int
    page_size: int


class AdminUserStatusUpdate(BaseModel):
    is_active: bool
    model_config = ConfigDict(extra="forbid")


ModelKind = Literal["text", "image", "video", "audio"]
ModelStatus = Literal["draft", "active", "disabled", "archived"]
WritableModelStatus = Literal["draft", "active", "disabled"]


class AdminModelGroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=10000)
    status: WritableModelStatus = "draft"
    display_order: int = Field(default=0, ge=0)
    model_config = ConfigDict(extra="forbid")

    @field_validator("name", "description")
    @classmethod
    def trim_strings(cls, value: str) -> str:
        return value.strip()


class AdminModelGroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=10000)
    status: WritableModelStatus | None = None
    display_order: int | None = Field(default=None, ge=0)
    model_config = ConfigDict(extra="forbid")

    @field_validator("name", "description")
    @classmethod
    def trim_optional_strings(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def require_changes(self):
        if not self.model_fields_set:
            raise ValueError("At least one field is required")
        return self


class AdminModelGroupRead(BaseModel):
    id: UUID
    name: str
    description: str
    status: ModelStatus
    display_order: int
    created_by_admin_id: UUID | None
    created_at: datetime
    updated_at: datetime
    model_count: int = 0
    model_config = ConfigDict(from_attributes=True)


class AdminModelGroupList(BaseModel):
    items: list[AdminModelGroupRead]


class AdminModelCreate(BaseModel):
    group_id: UUID
    model_name: str = Field(min_length=1, max_length=160)
    base_url: str = Field(default="", max_length=512)
    display_name: str = Field(min_length=1, max_length=160)
    kind: ModelKind
    description: str = Field(default="", max_length=10000)
    status: WritableModelStatus = "draft"
    display_order: int = Field(default=0, ge=0)
    config_schema: dict = Field(default_factory=dict)
    default_parameters: dict = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid")

    @field_validator("model_name", "base_url", "display_name", "description")
    @classmethod
    def trim_strings(cls, value: str) -> str:
        return value.strip()


class AdminModelUpdate(BaseModel):
    group_id: UUID | None = None
    model_name: str | None = Field(default=None, min_length=1, max_length=160)
    base_url: str | None = Field(default=None, max_length=512)
    display_name: str | None = Field(default=None, min_length=1, max_length=160)
    kind: ModelKind | None = None
    description: str | None = Field(default=None, max_length=10000)
    status: WritableModelStatus | None = None
    display_order: int | None = Field(default=None, ge=0)
    config_schema: dict | None = None
    default_parameters: dict | None = None
    metadata: dict | None = None
    model_config = ConfigDict(extra="forbid")

    @field_validator("model_name", "base_url", "display_name", "description")
    @classmethod
    def trim_optional_strings(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def require_changes(self):
        if not self.model_fields_set:
            raise ValueError("At least one field is required")
        return self


class AdminModelRead(BaseModel):
    id: UUID
    group_id: UUID
    model_name: str
    base_url: str
    display_name: str
    kind: ModelKind
    description: str
    status: ModelStatus
    display_order: int
    config_schema: dict
    default_parameters: dict
    metadata: Annotated[dict, Field(validation_alias="model_metadata")]
    created_by_admin_id: UUID | None
    created_at: datetime
    updated_at: datetime
    credential_configured: bool = False
    api_key_last_four: str | None = None
    protocol: Literal["chat_completions"] | None = None
    model_config = ConfigDict(from_attributes=True)


class AdminModelList(BaseModel):
    items: list[AdminModelRead]


class AdminTextModelCreate(BaseModel):
    group_id: UUID
    base_url: str = Field(min_length=1, max_length=512)
    model_name: str = Field(min_length=1, max_length=160)
    model_config = ConfigDict(extra="forbid")

    @field_validator("base_url", "model_name")
    @classmethod
    def trim_required_strings(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Field cannot be blank")
        return value


class AdminTextModelUpdate(AdminTextModelCreate):
    pass
