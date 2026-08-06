"""Central configuration for the SeekAndDestroy AI service.

Every tunable lives here. In particular the SQL Server connection is assembled
in exactly one place (:meth:`DatabaseSettings.odbc_connection_string`) so no
class anywhere else in the platform needs to know how to build one.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
AI_SERVICE_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = AI_SERVICE_ROOT / ".env"


class _Base(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )


class DatabaseSettings(_Base):
    """SQL Server connection settings.

    Defaults mirror the connection string supplied with the specification::

        Data Source=LAPTOP-R6U8H616;Initial Catalog=PraveenDB;
        Integrated Security=True;Persist Security Info=False;Pooling=False;
        MultipleActiveResultSets=False;Encrypt=False;
        TrustServerCertificate=False;Application Name=SeekAndDestroy;
        Command Timeout=0;
    """

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_prefix="SAD_DB__",
        extra="ignore",
        case_sensitive=False,
    )

    server: str = "LAPTOP-R6U8H616"
    database: str = "PraveenDB"
    schema_name: str = Field(default="sad", alias="schema")
    integrated_security: bool = True
    username: str = ""
    password: str = ""
    encrypt: bool = False
    trust_server_certificate: bool = False
    pooling: bool = False
    multiple_active_result_sets: bool = False
    persist_security_info: bool = False
    application_name: str = "SeekAndDestroy"
    command_timeout: int = 0
    odbc_driver: str = "ODBC Driver 17 for SQL Server"

    @field_validator("schema_name")
    @classmethod
    def _validate_schema(cls, value: str) -> str:
        # The schema name is interpolated into DDL/queries as an identifier, so
        # it can never come from anywhere untrusted and must look like one.
        if not value.replace("_", "").isalnum():
            raise ValueError(f"invalid schema identifier: {value!r}")
        return value

    @property
    def odbc_connection_string(self) -> str:
        """Raw ODBC connection string (also used by the MCP server)."""
        parts = [
            f"DRIVER={{{self.odbc_driver}}}",
            f"SERVER={self.server}",
            f"DATABASE={self.database}",
            f"Encrypt={'yes' if self.encrypt else 'no'}",
            f"TrustServerCertificate={'yes' if self.trust_server_certificate else 'no'}",
            f"MARS_Connection={'yes' if self.multiple_active_result_sets else 'no'}",
            f"APP={self.application_name}",
        ]
        if self.integrated_security:
            parts.append("Trusted_Connection=yes")
        else:
            parts.append(f"UID={self.username}")
            parts.append(f"PWD={self.password}")
        return ";".join(parts) + ";"

    @property
    def sqlalchemy_url(self) -> str:
        return "mssql+pyodbc:///?odbc_connect=" + quote_plus(self.odbc_connection_string)

    @property
    def dotnet_connection_string(self) -> str:
        """The equivalent SqlClient connection string, for documentation/tooling."""
        parts = [
            f"Data Source={self.server}",
            f"Initial Catalog={self.database}",
            f"Integrated Security={self.integrated_security}",
            f"Persist Security Info={self.persist_security_info}",
            f"Pooling={self.pooling}",
            f"MultipleActiveResultSets={self.multiple_active_result_sets}",
            f"Encrypt={self.encrypt}",
            f"TrustServerCertificate={self.trust_server_certificate}",
            f"Application Name={self.application_name}",
            f"Command Timeout={self.command_timeout}",
        ]
        return ";".join(parts) + ";"


class LlmSettings(_Base):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE), env_prefix="SAD_LLM__", extra="ignore", case_sensitive=False
    )

    provider: Literal["mock", "openai", "azure-openai", "ollama"] = "mock"
    model: str = "seek-and-destroy-mock"
    temperature: float = 0.0
    max_output_tokens: int = 2048
    timeout_seconds: int = 60
    base_url: str = ""
    api_key: str = ""
    azure_endpoint: str = ""
    azure_deployment: str = ""
    azure_api_version: str = "2024-10-21"
    ollama_base_url: str = "http://localhost:11434"
    max_input_chars: int = 24_000
    # Ordered, comma-separated list of additional providers to try if `provider`
    # (the primary) raises - e.g. "azure-openai,ollama". Each listed provider
    # reuses this same settings object's provider-specific fields (azure_endpoint/
    # azure_deployment for azure-openai, ollama_base_url for ollama, etc.), so
    # they must already be filled in for any fallback actually to work.
    fallback_providers: str = ""
    # Spend/abuse control for real providers: max real (non-mock) chat-model calls
    # allowed per UTC calendar day, process-wide (Redis-backed and thus shared
    # across workers when SAD_CACHE__BACKEND=redis; per-process otherwise). 0 (the
    # default) means unlimited - mock mode is always unlimited regardless of this
    # value, since it costs nothing and calls nothing external.
    daily_call_budget: int = 0

    @property
    def fallback_provider_list(self) -> list[str]:
        return [p.strip() for p in self.fallback_providers.split(",") if p.strip()]


class RetrievalSettings(_Base):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE), env_prefix="SAD_RETRIEVAL__", extra="ignore", case_sensitive=False
    )

    backend: Literal["qdrant", "memory"] = "memory"
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    collection: str = "seekanddestroy"
    embedding_provider: Literal["hash", "sentence-transformers", "api", "gemini"] = "hash"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_dimensions: int = 384
    # "api" provider: any OpenAI-compatible /v1/embeddings endpoint (OpenAI, Azure
    # OpenAI, Ollama). embedding_api_version set => Azure-style auth (api-key header +
    # ?api-version= query param) instead of Authorization: Bearer.
    # "gemini" provider: Google's native embedContent/batchEmbedContents API - a
    # different wire format from the OpenAI-compatible providers, so it's a separate
    # client (app/retrieval/gemini_embedder.py), not just different config on the same
    # HttpEmbedder class. Reuses embedding_api_key/embedding_base_url/embedding_model.
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_api_version: str = ""
    embedding_batch_size: int = 64
    embedding_timeout_seconds: int = 30
    # Same spend-control pattern as SAD_LLM__DAILY_CALL_BUDGET, for real (api/gemini)
    # embedding providers. Counts embed_documents/embed_query invocations, not texts -
    # a batched upsert of 200 documents is one call. 0 = unlimited (hash/sentence-
    # transformers are always unlimited - only real, billed providers are budgeted).
    embedding_daily_call_budget: int = 0
    # Pause between consecutive batch calls within one embed_documents() run - a full
    # reindex fires many batches back-to-back, which can burst past a provider's
    # requests-per-minute quota even though each individual call already retries with
    # backoff on a 429 (see app.utils.http_retry). Per-request backoff alone can't fix
    # a sustained per-minute quota; this is what actually paces bulk operations under
    # a strict free-tier limit. 0 (default) = no artificial delay.
    embedding_batch_delay_seconds: float = 0.0
    top_k: int = 8
    memory_store_path: str = ".state/vectors.json"


class PolicySettings(_Base):
    """Capacity, headroom and right-sizing policy. Drives the hard rules."""

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE), env_prefix="SAD_POLICY__", extra="ignore", case_sensitive=False
    )

    cpu_threshold_percent: float = 75.0
    memory_threshold_percent: float = 80.0
    storage_threshold_percent: float = 85.0
    safety_margin_percent: float = 10.0
    utilization_window_days: int = 30
    growth_horizon_years: int = 1

    overprovision_cpu_percent: float = 32.0
    overprovision_memory_percent: float = 32.0
    underprovision_cpu_percent: float = 80.0
    underprovision_memory_percent: float = 85.0

    min_nodes_tier1: int = 3
    min_nodes_tier2: int = 2
    node_failure_tolerance: int = 1


class ScoringSettings(_Base):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE), env_prefix="SAD_SCORING__", extra="ignore", case_sensitive=False
    )

    weight_capacity: float = 0.30
    weight_compatibility: float = 0.15
    weight_resiliency: float = 0.15
    weight_cost: float = 0.15
    weight_dependency: float = 0.10
    weight_historical: float = 0.10
    weight_risk: float = 0.05
    min_confident_score: float = 55.0

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> "ScoringSettings":
        total = (
            self.weight_capacity
            + self.weight_compatibility
            + self.weight_resiliency
            + self.weight_cost
            + self.weight_dependency
            + self.weight_historical
            + self.weight_risk
        )
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"scoring weights must sum to 1.0, got {total}")
        return self

    def as_dict(self) -> dict[str, float]:
        return {
            "capacity": self.weight_capacity,
            "compatibility": self.weight_compatibility,
            "resiliency": self.weight_resiliency,
            "cost": self.weight_cost,
            "dependency": self.weight_dependency,
            "historical": self.weight_historical,
            "risk": self.weight_risk,
        }


class ForecastSettings(_Base):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE), env_prefix="SAD_FORECAST__", extra="ignore", case_sensitive=False
    )

    default_horizon_days: int = 90
    min_history_days: int = 14
    confidence_z: float = 1.96
    supported_horizons: tuple[int, ...] = (30, 60, 90, 180)


class AuthSettings(_Base):
    """JWT authentication. ``local`` (default) is a self-contained dev/demo
    mode: this service issues its own HMAC-signed tokens (``POST
    /api/auth/dev-token``) against a real Employee row - not a real login
    flow, but not a rubber stamp either. ``oidc`` validates tokens issued by
    a real external identity provider (Azure AD/Entra, Okta, ...) via
    standard JWKS/RS256 - this service never issues tokens in that mode, and
    the dev-token endpoint is disabled (404).

    The .NET gateway and the MCP server's write tools validate the exact
    same tokens - in ``local`` mode they need the same ``local_signing_key``
    configured; in ``oidc`` mode they need the same ``oidc_authority``/
    ``oidc_audience``.
    """

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE), env_prefix="SAD_AUTH__", extra="ignore", case_sensitive=False
    )

    mode: Literal["local", "oidc"] = "local"
    local_signing_key: str = "dev-only-insecure-signing-key-change-me"
    local_token_ttl_minutes: int = 60
    algorithm_local: str = "HS256"
    oidc_authority: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""  # optional override; derived from oidc_authority if blank


class CacheSettings(_Base):
    """LLM-narration and capacity-snapshot caching. ``memory`` (default) keeps
    everything in-process, no server needed; ``redis`` talks to a real Redis
    instance so the cache survives process restarts and is shared across
    multiple ai-service workers.
    """

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE), env_prefix="SAD_CACHE__", extra="ignore", case_sensitive=False
    )

    backend: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://localhost:6379/0"
    default_ttl_seconds: int = 300


class ServiceSettings(_Base):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE), env_prefix="SAD_SERVICE__", extra="ignore", case_sensitive=False
    )

    host: str = "127.0.0.1"
    port: int = 8088
    log_level: str = "INFO"
    log_json: bool = False
    checkpoint_path: str = ".state/checkpoints.db"
    max_query_chars: int = 2000
    max_rows: int = 500

    @property
    def checkpoint_file(self) -> Path:
        path = Path(self.checkpoint_path)
        if not path.is_absolute():
            path = REPO_ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


class Settings:
    """Aggregate settings object handed to every component."""

    def __init__(self) -> None:
        self.db = DatabaseSettings()
        self.llm = LlmSettings()
        self.retrieval = RetrievalSettings()
        self.policy = PolicySettings()
        self.scoring = ScoringSettings()
        self.forecast = ForecastSettings()
        self.service = ServiceSettings()
        self.cache = CacheSettings()
        self.auth = AuthSettings()
        self.repo_root = REPO_ROOT

    @property
    def langsmith_enabled(self) -> bool:
        return os.getenv("LANGSMITH_TRACING", "false").strip().lower() in {"1", "true", "yes"}

    def memory_store_file(self) -> Path | None:
        raw = self.retrieval.memory_store_path.strip()
        if not raw:
            return None
        path = Path(raw)
        if not path.is_absolute():
            path = REPO_ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Used by tests that patch environment variables."""
    get_settings.cache_clear()
