"""The MCP server registry (Spec 19 §2): `mcp_servers.toml`, validated at startup."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from knappy.config import ConfigError

AuthMode = Literal["oauth_dcr", "oauth_static", "api_key", "service"]
DEFAULT_PATH = Path(__file__).resolve().parents[2] / "mcp_servers.toml"


class ToolFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allow: tuple[str, ...] = ("*",)
    deny: tuple[str, ...] = ()


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title: str
    url: str = Field(pattern=r"^https?://")
    auth: AuthMode
    auth_group: str
    scopes: tuple[str, ...] = ()
    client_id_env: str | None = None
    client_secret_env: str | None = None
    token_env: str | None = None
    tools: ToolFilter = ToolFilter()
    read: tuple[str, ...] = ()
    write: tuple[str, ...] = ()
    body_field: dict[str, str] = {}

    @field_validator("name")
    @classmethod
    def _no_separator(cls, name: str) -> str:
        # Spec 20 names tools `<server>__<tool>`; a `__` inside the name would make that ambiguous.
        if "__" in name:
            raise ValueError("name must not contain '__'")
        return name

    @model_validator(mode="before")
    @classmethod
    def _default_group(cls, data: object) -> object:
        if isinstance(data, dict) and not data.get("auth_group") and isinstance(data.get("name"), str):
            return {**data, "auth_group": data["name"]}
        return data

    @model_validator(mode="after")
    def _mode_fields(self) -> ServerConfig:
        if self.auth == "oauth_static" and not (self.client_id_env and self.client_secret_env):
            raise ValueError("oauth_static needs client_id_env and client_secret_env")
        if self.auth == "service" and not self.token_env:
            raise ValueError("service needs token_env")
        return self

    def required_env(self) -> tuple[str, ...]:
        if self.auth == "oauth_static":
            return (self.client_id_env or "", self.client_secret_env or "")
        if self.auth == "service":
            return (self.token_env or "",)
        return ()

    def missing_env(self, env: Mapping[str, str]) -> list[str]:
        return [name for name in self.required_env() if not env.get(name)]

    def exposes(self, tool: str) -> bool:
        return _matches(self.tools.allow, tool) and not _matches(self.tools.deny, tool)

    def globs(self) -> tuple[str, ...]:
        return (*self.tools.allow, *self.tools.deny, *self.read, *self.write, *self.body_field)

    def body_field_for(self, tool: str) -> str | None:
        return next((field for glob, field in self.body_field.items() if fnmatchcase(tool, glob)), None)


def _matches(globs: tuple[str, ...], tool: str) -> bool:
    return any(fnmatchcase(tool, glob) for glob in globs)


def classify(server: ServerConfig, tool: str, read_only_hint: bool | None) -> Literal["read", "write"]:
    """Read only on an explicit `read` glob, or a readOnlyHint no `write` glob overrides. Everything else writes."""
    if _matches(server.read, tool):
        return "read"
    if read_only_hint is True and not _matches(server.write, tool):
        return "read"
    return "write"


def parse_servers(data: Mapping[str, object]) -> tuple[ServerConfig, ...]:
    entries = data.get("server", [])
    if not isinstance(entries, list):
        raise ConfigError("mcp_servers.toml: 'server' must be an array of tables ([[server]])")
    servers: list[ServerConfig] = []
    for index, entry in enumerate(entries):
        label = entry.get("name", f"#{index + 1}") if isinstance(entry, dict) else f"#{index + 1}"
        try:
            servers.append(ServerConfig.model_validate(entry))
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(map(str, err['loc'])) or 'entry'}: {err['msg']}" for err in exc.errors())
            raise ConfigError(f"mcp_servers.toml entry {label!r}: {problems}") from None
    _check_registry(servers)
    return tuple(servers)


def _check_registry(servers: list[ServerConfig]) -> None:
    seen: set[str] = set()
    groups: dict[str, ServerConfig] = {}
    for server in servers:
        if server.name in seen:
            raise ConfigError(f"mcp_servers.toml entry {server.name!r}: duplicate name")
        seen.add(server.name)
        first = groups.setdefault(server.auth_group, server)
        shared = (first.auth, first.client_id_env, first.client_secret_env, first.token_env)
        if (server.auth, server.client_id_env, server.client_secret_env, server.token_env) != shared:
            raise ConfigError(
                f"mcp_servers.toml entry {server.name!r}: auth_group {server.auth_group!r} must share auth and env vars "
                f"with {first.name!r}"
            )


def load_servers(path: Path = DEFAULT_PATH) -> tuple[ServerConfig, ...]:
    if not path.is_file():
        return ()
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name}: {exc}") from None
    return parse_servers(data)
