from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Objective(BaseModel):
    id: str
    text: str
    weight: float = 1.0


class GoalConfig(BaseModel):
    profile: str = "default"
    prose: str = ""
    objectives: list[Objective] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    style: dict[str, Any] = Field(default_factory=dict)

    def render_system_block(self) -> str:
        parts: list[str] = []
        if self.prose:
            parts.append(self.prose.strip())
        if self.objectives:
            parts.append("\n## Objectives")
            for o in self.objectives:
                parts.append(f"- ({o.weight:.1f}) [{o.id}] {o.text}")
        if self.constraints:
            parts.append("\n## Constraints")
            for c in self.constraints:
                parts.append(f"- {c}")
        if self.success_criteria:
            parts.append("\n## Success criteria")
            for s in self.success_criteria:
                parts.append(f"- {s}")
        if self.style:
            parts.append("\n## Style")
            for k, v in self.style.items():
                parts.append(f"- {k}: {v}")
        return "\n".join(parts).strip()


class ToolPolicy(BaseModel):
    mode: str = "allow"
    scope: str | None = None
    timeout_sec: int | None = None
    mem_mb: int | None = None
    feature_flag: str | None = None
    max_result_bytes: int | None = None


class ToolsetSpec(BaseModel):
    """Named bundle of tools sharing a single allow/deny + feature_flag.

    Loose binding: tools don't know which toolset they belong to. Policy
    resolution order in :meth:`Policy.for_tool` is per-name → toolset → default.
    """

    tools: list[str] = Field(default_factory=list)
    mode: str = "allow"
    feature_flag: str | None = None


class BudgetTurn(BaseModel):
    tool_calls: int = 32
    wall_clock_sec: int = 300


class BudgetSession(BaseModel):
    tokens: int = 2_000_000
    usd: float = 10.0


class Budgets(BaseModel):
    per_turn: BudgetTurn = Field(default_factory=BudgetTurn)
    per_session: BudgetSession = Field(default_factory=BudgetSession)


class Policy(BaseModel):
    version: int = 1
    default: str = "allow"
    auto_approve: bool = True
    default_max_result_bytes: int = 10_000
    tools: dict[str, ToolPolicy] = Field(default_factory=dict)
    toolsets: dict[str, ToolsetSpec] = Field(default_factory=dict)
    budgets: Budgets = Field(default_factory=Budgets)

    def toolset_for_tool(self, name: str) -> ToolsetSpec | None:
        matches = [(ts_name, ts) for ts_name, ts in self.toolsets.items() if name in ts.tools]
        if len(matches) > 1:
            ids = sorted(m[0] for m in matches)
            raise ValueError(
                f"tool '{name}' belongs to multiple toolsets {ids}; policy "
                "resolution would be YAML-order dependent. Move the tool into "
                "exactly one toolset, or split it out into its own per-tool entry."
            )
        return matches[0][1] if matches else None

    def for_tool(self, name: str) -> ToolPolicy:
        if name in self.tools:
            return self.tools[name]
        ts = self.toolset_for_tool(name)
        if ts is not None:
            return ToolPolicy(mode=ts.mode, feature_flag=ts.feature_flag)
        return ToolPolicy(mode=self.default)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # LLM
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-5"
    anthropic_max_tokens: int = 16000

    # Embeddings
    # provider: "auto" cascades voyage -> ollama -> hash-stub.
    # Pin to one of "voyage" / "ollama" / "hash" to disable the cascade.
    voyage_api_key: str = ""
    embedding_provider: str = "auto"
    embedding_model: str = "voyage-3"
    embedding_dim: int = 1024

    # Ollama (local embeddings backend, used by "auto" and "ollama")
    ollama_url: str = "http://localhost:11434"
    ollama_embedding_model: str = "mxbai-embed-large"
    ollama_probe_timeout_s: float = 0.75

    # Database (local Postgres, peer/trust auth as current user)
    database_url: str = "postgresql+asyncpg:///jazz_guru"
    database_url_sync: str = "postgresql+psycopg:///jazz_guru"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Workspace
    jg_workspace_dir: Path = Path("./workspace")
    jg_state_dir: Path = Path("./workspace/state")
    jg_trace_dir: Path = Path("./workspace/traces")
    jg_data_dir: Path = Path("./data")

    # Goal
    jg_goal_file: Path = Path("./config/goal.md")
    jg_goal_yaml: Path = Path("./config/goal.yaml")
    jg_policy_file: Path = Path("./config/policy.yaml")

    # Instruments / rendering presets
    jg_instruments_file: Path = Path("./data/instruments.yaml")
    jg_instruments_root: Path = Path.home() / ".local/share/jazz-guru/instruments"

    # Sandbox
    jg_safe_extra_paths: list[Path] = Field(default_factory=list)
    jg_os_sandbox: int = 0

    # Web search
    tavily_api_key: str = ""

    # Music / audio
    fluidsynth_soundfont: str = ""

    # Feature flags
    feature_tts: int = 0
    feature_audio_ml: int = 0

    # Server
    jg_host: str = "127.0.0.1"
    jg_port: int = 8000

    # Telemetry
    otel_service_name: str = "jazz-guru"
    otel_exporter_otlp_endpoint: str = ""

    # Tier-2 tool improvement loop (plan §B.7)
    jg_improver_threshold: int = 2  # per-tool default; overridable in tool meta
    jg_improver_max_per_run: int = 3  # max maybe_improve calls per reflexion

    def ensure_dirs(self) -> None:
        for d in (self.jg_workspace_dir, self.jg_state_dir, self.jg_trace_dir):
            d.mkdir(parents=True, exist_ok=True)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_goal(settings: Settings) -> GoalConfig:
    prose = ""
    if settings.jg_goal_file.exists():
        prose = settings.jg_goal_file.read_text(encoding="utf-8")
    data = _load_yaml(settings.jg_goal_yaml)
    return GoalConfig(prose=prose, **data)


def load_policy(settings: Settings) -> Policy:
    data = _load_yaml(settings.jg_policy_file)
    return Policy(**data) if data else Policy()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


@lru_cache(maxsize=1)
def get_goal() -> GoalConfig:
    return load_goal(get_settings())


@lru_cache(maxsize=1)
def get_policy() -> Policy:
    return load_policy(get_settings())


def clear_config_caches() -> None:
    """Drop all config caches so a long-running process re-reads .env + YAML.

    Settings / goal / policy / presets are cached independently; clearing
    only one leaves the others stale. Use this from anything that mutates
    ``.env`` or the YAML files in a live server/worker.
    """
    get_settings.cache_clear()
    get_goal.cache_clear()
    get_policy.cache_clear()
    # Local import to avoid a circular dep at module-load time.
    from jazz_guru.presets import clear_presets_cache

    clear_presets_cache()
