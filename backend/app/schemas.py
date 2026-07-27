from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


class AuthCredentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value):
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, value: str) -> str:
        if not any(character.isupper() for character in value):
            raise ValueError("Password must contain an uppercase letter")
        if not any(character.islower() for character in value):
            raise ValueError("Password must contain a lowercase letter")
        if not any(character.isdigit() for character in value):
            raise ValueError("Password must contain a number")
        return value


class UserRead(BaseModel):
    id: UUID
    email: str
    model_config = ConfigDict(from_attributes=True)


class AuthResponse(BaseModel):
    user: UserRead


class FolderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Folder name cannot be blank")
        return value


class FolderUpdate(FolderCreate):
    pass


class FolderRead(BaseModel):
    id: UUID
    name: str
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)


class FolderList(BaseModel):
    items: list[FolderRead]


class Viewport(BaseModel):
    x: float
    y: float
    zoom: float = Field(ge=0.1, le=4)
    model_config = ConfigDict(extra="forbid")


class Position(BaseModel):
    x: float
    y: float
    model_config = ConfigDict(extra="forbid")


class NodeSize(BaseModel):
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    model_config = ConfigDict(extra="forbid")


class NodeMessage(BaseModel):
    id: UUID
    role: Literal["user", "assistant", "system"]
    content: str
    model_id: str | None = None
    generation_task_id: UUID | None = None
    created_at: datetime
    model_config = ConfigDict(extra="forbid")


class NodeData(BaseModel):
    title: str = ""
    prompt: str = ""
    draft: str = ""
    model_id: str | None = None
    parameters: dict = Field(default_factory=dict)
    asset_id: UUID | None = None
    generation_task_id: UUID | None = None
    messages: list[NodeMessage] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid")


class WorkflowNode(BaseModel):
    id: UUID
    type: Literal["prompt", "image", "video", "audio"]
    position: Position
    size: NodeSize
    data: NodeData = Field(default_factory=NodeData)
    model_config = ConfigDict(extra="forbid")


class WorkflowEdge(BaseModel):
    id: UUID
    source_node_id: UUID
    target_node_id: UUID
    source_handle: str | None = None
    target_handle: str | None = None
    data: dict = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid")


WORKFLOW_INPUT_KINDS = {
    "text": set(),
    "image": {"text", "image"},
    "video": {"text", "image", "video", "audio"},
    "audio": {"text"},
}


class Workflow(BaseModel):
    schema_version: Literal[1] = 1
    viewport: Viewport = Field(default_factory=lambda: Viewport(x=0, y=0, zoom=1))
    nodes: list[WorkflowNode] = Field(default_factory=list)
    edges: list[WorkflowEdge] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_graph(self):
        node_ids = [node.id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("Node ids must be unique")
        edge_ids = [edge.id for edge in self.edges]
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("Edge ids must be unique")
        known_nodes = set(node_ids)
        nodes_by_id = {node.id: node for node in self.nodes}
        edge_pairs = set()
        outgoing = {node_id: set() for node_id in node_ids}
        indegree = {node_id: 0 for node_id in node_ids}
        for edge in self.edges:
            if edge.source_node_id not in known_nodes or edge.target_node_id not in known_nodes:
                raise ValueError("Every edge endpoint must reference an existing node")
            if edge.source_node_id == edge.target_node_id:
                raise ValueError("Workflow edges cannot reference the same node at both ends")
            pair = (edge.source_node_id, edge.target_node_id)
            if pair in edge_pairs:
                raise ValueError("Workflow cannot contain duplicate source and target edges")
            edge_pairs.add(pair)
            source_type = nodes_by_id[edge.source_node_id].type
            target_type = nodes_by_id[edge.target_node_id].type
            source_kind = "text" if source_type == "prompt" else source_type
            target_kind = "text" if target_type == "prompt" else target_type
            if source_kind not in WORKFLOW_INPUT_KINDS[target_kind]:
                raise ValueError(
                    f"Workflow edge type {source_kind} is not compatible with {target_kind}"
                )
            outgoing[edge.source_node_id].add(edge.target_node_id)
            indegree[edge.target_node_id] += 1
        pending = [node_id for node_id, count in indegree.items() if count == 0]
        visited = 0
        while pending:
            current = pending.pop()
            visited += 1
            for target in outgoing[current]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    pending.append(target)
        if visited != len(node_ids):
            raise ValueError("Workflow graph cannot contain a cycle")
        return self


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    folder_id: UUID | None = None
    project_type: Literal["image", "video", "mixed"] = "mixed"
    workflow: Workflow | None = None

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Project name cannot be blank")
        return value


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    folder_id: UUID | None = None
    project_type: Literal["image", "video", "mixed"] | None = None
    cover_asset_id: UUID | None = None

    @field_validator("name")
    @classmethod
    def trim_optional_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Project name cannot be blank")
        return value


class ProjectSummary(BaseModel):
    id: UUID
    folder_id: UUID | None
    name: str
    project_type: str
    cover_asset_id: UUID | None
    current_revision: int
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)


class ProjectListItem(ProjectSummary):
    preview_asset_id: UUID | None = None


class ProjectRead(ProjectSummary):
    workflow: Workflow
    workflow_schema_version: int


class ProjectList(BaseModel):
    items: list[ProjectListItem]
    next_cursor: str | None = None


VersionReason = Literal["initial", "interval", "generation_completed", "leave", "manual"]


class WorkflowSave(BaseModel):
    base_revision: int = Field(ge=0)
    workflow: Workflow
    reason: VersionReason | None = None
    model_config = ConfigDict(extra="forbid")


class WorkflowRead(BaseModel):
    workflow: Workflow
    revision: int
    updated_at: datetime


class VersionRead(BaseModel):
    id: UUID
    version_no: int
    schema_version: int
    reason: str
    restored_from_version_id: UUID | None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class VersionList(BaseModel):
    items: list[VersionRead]


AssetKind = Literal["text", "image", "video", "audio"]
AssetSource = Literal["user_input", "upload", "generation"]
AssetStatus = Literal["uploading", "ready", "failed", "deleting"]


class AssetCreate(BaseModel):
    project_id: UUID
    kind: AssetKind
    source: AssetSource
    text_content: str | None = None
    original_name: str | None = Field(default=None, max_length=255)
    storage_key: str | None = Field(default=None, max_length=512)
    mime_type: str | None = Field(default=None, max_length=120)
    byte_size: int | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    duration_ms: int | None = Field(default=None, ge=0)
    checksum_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    status: AssetStatus | None = None
    metadata: dict = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_location(self):
        if self.kind == "text":
            if self.text_content is None or self.storage_key is not None:
                raise ValueError("Text assets require text_content and cannot have storage_key")
        elif self.text_content is not None or not self.storage_key:
            raise ValueError("Media assets require storage_key and cannot have text_content")
        return self


class AssetUpdate(BaseModel):
    text_content: str | None = None
    original_name: str | None = Field(default=None, max_length=255)
    status: AssetStatus | None = None
    metadata: dict | None = None
    model_config = ConfigDict(extra="forbid")


class AssetRead(BaseModel):
    id: UUID
    project_id: UUID
    kind: str
    source: str
    text_content: str | None
    original_name: str | None
    storage_key: str | None
    mime_type: str
    byte_size: int
    width: int | None
    height: int | None
    duration_ms: int | None
    checksum_sha256: str | None
    status: str
    metadata: Annotated[dict, Field(validation_alias="asset_metadata")]
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)


class AssetList(BaseModel):
    items: list[AssetRead]


class ImageEditRead(BaseModel):
    asset: AssetRead
    workflow: Workflow
    revision: int
    updated_at: datetime
    replayed: bool = False


class GenerationCreate(BaseModel):
    project_id: UUID
    node_id: UUID
    input_node_ids: list[UUID] = Field(default_factory=list)
    input_revision: int = Field(ge=0)
    kind: AssetKind
    model: str = Field(min_length=1, max_length=80)
    prompt: str = Field(default="", max_length=4000)
    parameters: dict = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid")

    @field_validator("input_node_ids")
    @classmethod
    def reject_duplicate_input_nodes(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("Generation input_node_ids cannot contain duplicates")
        return value


class GenerationOutputRead(BaseModel):
    ordinal: int
    asset: AssetRead


class GenerationRead(BaseModel):
    id: UUID
    project_id: UUID
    node_id: UUID
    input_revision: int
    kind: str
    model: str
    prompt: str
    parameters: dict
    status: str
    result: dict
    error: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    outputs: list[GenerationOutputRead] = Field(default_factory=list)
    model_config = ConfigDict(from_attributes=True)


class PromptOptimizationCreate(BaseModel):
    model_id: UUID
    target_kind: Literal["image", "video"]
    input: str = Field(min_length=1, max_length=8000)
    model_config = ConfigDict(extra="forbid")

    @field_validator("input")
    @classmethod
    def trim_prompt_input(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Prompt input cannot be blank")
        return value


class PromptOptimizationRead(BaseModel):
    model_id: UUID
    target_kind: Literal["image", "video"]
    optimized_prompt: str


class ImageProjectGenerationCreate(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    image_model_id: UUID
    parameters: dict = Field(default_factory=dict)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    folder_id: UUID | None = None
    model_config = ConfigDict(extra="forbid")

    @field_validator("prompt")
    @classmethod
    def trim_image_prompt(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Prompt cannot be blank")
        return value

    @field_validator("name")
    @classmethod
    def trim_image_project_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Project name cannot be blank")
        return value


class ImageProjectGenerationRead(BaseModel):
    project: ProjectRead
    generation: GenerationRead
    prompt_node_id: UUID
    image_node_id: UUID


class ProjectGenerationList(BaseModel):
    items: list[GenerationRead]
