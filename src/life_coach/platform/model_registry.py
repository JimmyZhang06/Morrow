"""Explicit model imports used to populate the shared SQLAlchemy metadata."""

from __future__ import annotations

from importlib import import_module

# Feature branches extend this tuple with modules that declare mappings on shared Base.
MODEL_MODULES: tuple[str, ...] = ()


def load_model_registry() -> None:
    """Import every explicitly registered model module exactly once per interpreter."""

    for module_name in MODEL_MODULES:
        import_module(module_name)
