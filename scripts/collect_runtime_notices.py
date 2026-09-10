"""Preserve installed Python dependency metadata and license texts with the release."""

from __future__ import annotations

import importlib.metadata
import json
import shutil
from pathlib import Path


def main() -> None:
    target = Path(__file__).resolve().parents[1] / ".data" / "release-runtime" / "notices"
    target.mkdir(parents=True, exist_ok=True)
    inventory = []
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name", "unknown")
        inventory.append({"name": name, "version": distribution.version})
        for relative in distribution.files or ():
            if not any(word in Path(str(relative)).name.lower() for word in ("license", "copying")):
                continue
            source = Path(distribution.locate_file(relative))
            if source.is_file():
                package = target / name
                package.mkdir(exist_ok=True)
                shutil.copyfile(source, package / source.name)
    (target / "python-inventory.json").write_text(
        json.dumps(inventory, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
