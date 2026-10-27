"""All configuration comes from the environment (docker-compose passes .env through)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"missing environment variable {name}")
    return value


@dataclass(frozen=True)
class Settings:
    pg_user: str = field(default_factory=lambda: _env("POSTGRES_USER", "lakehouse"))
    pg_password: str = field(default_factory=lambda: _env("POSTGRES_PASSWORD", ""))
    pg_host: str = field(default_factory=lambda: _env("POSTGRES_HOST", "postgres"))

    s3_endpoint: str = field(default_factory=lambda: _env("S3_ENDPOINT", "http://seaweedfs:8333"))
    s3_access_key: str = field(default_factory=lambda: _env("S3_ACCESS_KEY", ""))
    s3_secret_key: str = field(default_factory=lambda: _env("S3_SECRET_KEY", ""))
    bucket: str = field(default_factory=lambda: _env("S3_BUCKET", "lakehouse"))

    catalog_uri: str = field(default_factory=lambda: _env("ICEBERG_CATALOG_URI", "http://lakekeeper:8181/catalog"))
    warehouse: str = field(default_factory=lambda: _env("ICEBERG_WAREHOUSE", "lakehouse"))
    spark_remote: str = field(default_factory=lambda: _env("SPARK_REMOTE", "sc://spark:15002"))

    trino_host: str = field(default_factory=lambda: _env("TRINO_HOST", "trino"))
    trino_port: int = field(default_factory=lambda: int(_env("TRINO_PORT", "8080")))

    kafka_bootstrap: str = field(default_factory=lambda: _env("KAFKA_BOOTSTRAP", "kafka:9092"))

    ollama_host: str = field(default_factory=lambda: _env("OLLAMA_HOST", "http://ollama:11434"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "qwen2.5:7b"))
    embed_model: str = field(default_factory=lambda: _env("EMBED_MODEL", "nomic-embed-text"))

    api_keys_json: str = field(default_factory=lambda: _env("API_KEYS_JSON", "{}"))

    def pg_dsn(self, database: str) -> str:
        return f"postgresql://{self.pg_user}:{self.pg_password}@{self.pg_host}:5432/{database}"

    @property
    def api_keys(self) -> dict[str, str]:
        """API key -> Trino user."""
        return json.loads(self.api_keys_json)

    def s3_uri(self, *parts: str) -> str:
        return "s3://" + "/".join([self.bucket, *[p.strip("/") for p in parts]])


@lru_cache
def settings() -> Settings:
    return Settings()
