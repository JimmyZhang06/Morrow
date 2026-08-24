"""Tests for Alembic configuration and explicit model registration."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

import pytest

from life_coach.platform import model_registry

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_model_registry_imports_each_explicit_module(monkeypatch: pytest.MonkeyPatch) -> None:
    imported_modules: list[str] = []

    def record_import(module_name: str) -> ModuleType:
        imported_modules.append(module_name)
        return ModuleType(module_name)

    monkeypatch.setattr(model_registry, "MODEL_MODULES", ("example.first", "example.second"))
    monkeypatch.setattr(model_registry, "import_module", record_import)

    model_registry.load_model_registry()

    assert imported_modules == ["example.first", "example.second"]


def run_alembic(environment: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    process_environment = os.environ.copy()
    existing_python_path = process_environment.get("PYTHONPATH")
    source_path = str(PROJECT_ROOT / "src")
    process_environment["PYTHONPATH"] = (
        f"{source_path}{os.pathsep}{existing_python_path}" if existing_python_path else source_path
    )
    process_environment.update(environment)
    process_environment["APP_OBJECT_STORE_ENDPOINT"] = "https://objects.internal"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "alembic/alembic.ini",
            "upgrade",
            "head",
            "--sql",
        ],
        cwd=PROJECT_ROOT,
        env=process_environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_alembic_uses_validated_app_database_url() -> None:
    result = run_alembic(
        {
            "APP_ENV": "production",
            "APP_DATABASE_URL": ("postgresql+asyncpg://db.internal/life_coach?ssl=verify-full"),
        }
    )

    assert result.returncode == 0, result.stderr
    assert "BEGIN;" in result.stdout
    assert result.stdout.index("CREATE TABLE principal") < result.stdout.index(
        'ALTER TABLE "public"."vault_membership" ENABLE ROW LEVEL SECURITY'
    )
    assert (
        "ALTER TABLE claim_version ADD CONSTRAINT fk_claim_version_vault_model_run "
        "FOREIGN KEY(vault_id, model_run_id) REFERENCES model_run (vault_id, id) "
        "ON DELETE RESTRICT NOT VALID" in result.stdout
    )
    assert (
        "ALTER TABLE evidence_link ADD CONSTRAINT fk_evidence_link_vault_model_run "
        "FOREIGN KEY(vault_id, model_run_id) REFERENCES model_run (vault_id, id) "
        "ON DELETE RESTRICT NOT VALID" in result.stdout
    )


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+asyncpg://db.internal/life_coach",
        "postgresql+asyncpg://user:ALEMBIC_SECRET@localhost/life_coach?ssl=verify-full",
        (
            "postgresql+asyncpg://user:ALEMBIC_SECRET@db.internal/life_coach"
            "?ssl=verify-full&host=localhost"
        ),
        (
            "postgresql+asyncpg://user:ALEMBIC_SECRET@db.internal/life_coach"
            "?ssl=verify-full&hostaddr=127.0.0.1"
        ),
    ],
)
def test_alembic_rejects_unsafe_production_database_url(database_url: str) -> None:
    result = run_alembic(
        {
            "APP_ENV": "production",
            "APP_DATABASE_URL": database_url,
        }
    )

    combined_output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "ALEMBIC_SECRET" not in combined_output
