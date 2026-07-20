"""Bodies pydantic de las requests/responses (auth + users)."""

from pydantic import BaseModel, Field

# ---- auth ----

class LoginBody(BaseModel):
    username: str
    password: str


class RefreshBody(BaseModel):
    refresh: str


class ChangePasswordBody(BaseModel):
    # current es opcional: en el primer ingreso (must_change_pw) se permite sin el viejo
    current: str | None = None
    newPassword: str = Field(min_length=6)


# ---- users (admin) ----

class CreateUserBody(BaseModel):
    username: str = Field(min_length=1)
    role: str = "viewer"          # admin | editor | viewer


class SetRoleBody(BaseModel):
    role: str                     # admin | editor | viewer


class SetAclBody(BaseModel):
    folderId: str
    permission: str               # none | read | write


# ---- folders / projects (namespace canónico) ----

class CreateFolderBody(BaseModel):
    name: str = Field(min_length=1)


class CreateProjectBody(BaseModel):
    folderId: str
    name: str = Field(min_length=1)


class IdBody(BaseModel):
    id: str


# ---- versionado (git) ----

class CommitBody(BaseModel):
    id: str
    message: str | None = None


class RollbackBody(BaseModel):
    id: str
    commit: str


class GithubConnectBody(BaseModel):
    remoteUrl: str
    token: str
    branch: str = "main"


# ---- modo editor (doc 27) ----

class EditorTargetBody(BaseModel):
    projectId: str
    path: str


class FsWriteBody(BaseModel):
    projectId: str
    path: str
    content: str


class FsPathBody(BaseModel):
    projectId: str
    path: str


class FsRenameBody(BaseModel):
    # `from` es keyword de Python → campo `from_` con alias (el wire manda "from")
    projectId: str
    from_: str = Field(alias="from")
    to: str


class FsExecBody(BaseModel):
    projectId: str
    cmd: str


class DocsHashBody(BaseModel):
    projectId: str
    hash: str


class DocsGcBody(BaseModel):
    projectId: str
    keep: list[str] = []         # hashes que el manifiesto sigue referenciando


class SvSaveBody(BaseModel):
    projectId: str
    note: str | None = None
    author: str | None = None    # anotación (p.ej. "IA"); el autor real es el del token


class SvRestoreBody(BaseModel):
    projectId: str
    id: str
    author: str | None = None


class GhConnectBody(BaseModel):
    projectId: str
    remoteUrl: str
    token: str | None = None
    branch: str | None = None


class GhProjectBody(BaseModel):
    projectId: str


class GhPushBody(BaseModel):
    projectId: str
    message: str | None = None
    author: str | None = None    # "IA" → el commit queda anotado como hecho por la IA


class GhPullBody(BaseModel):
    projectId: str
    ref: str | None = None
    author: str | None = None


# ---- MCP por carpeta (doc 26 §6) ----

class McpTokenCreateBody(BaseModel):
    folderId: str
    userId: int | None = None    # admin puede emitir para otro usuario; default: uno mismo
    name: str = ""


class McpTokenRevokeBody(BaseModel):
    id: int
