"""Fetch the pinned EDB Windows archive and verify before extracting runtime files."""

from __future__ import annotations

import hashlib
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

URL = "https://get.enterprisedb.com/postgresql/postgresql-17.11-3-windows-x64-binaries.zip"
SHA256 = "4b8db0930c38f6ef845db919551dedda3b6b845aeb0927b3d79a6e8e9e4537cf"
SIZE = 341325378
CHUNK = 4 * 1024 * 1024
ROOT = Path(__file__).resolve().parents[1] / ".data" / "release-runtime"


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    archive = ROOT / "postgresql-17.11.zip"
    if not archive.exists():
        chunks = ROOT / "chunks"
        chunks.mkdir(exist_ok=True)

        def download(start: int) -> Path:
            end = min(SIZE, start + CHUNK) - 1
            target = chunks / str(start)
            if target.exists() and target.stat().st_size == end - start + 1:
                return target
            for attempt in range(6):
                try:
                    request = urllib.request.Request(
                        URL, headers={"Range": f"bytes={start}-{end}"}
                    )
                    with urllib.request.urlopen(request, timeout=30) as response:
                        if response.status != 206:
                            raise RuntimeError("download server did not honor byte range")
                        data = response.read()
                    if len(data) != end - start + 1:
                        raise RuntimeError("download chunk truncated")
                    target.write_bytes(data)
                    return target
                except (OSError, RuntimeError):
                    if attempt == 5:
                        raise
            raise RuntimeError("download failed")

        with ThreadPoolExecutor(max_workers=8) as pool:
            parts = list(pool.map(download, range(0, SIZE, CHUNK)))
        with archive.open("wb") as output:
            for part in parts:
                output.write(part.read_bytes())
    with archive.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != SHA256:
            raise RuntimeError("PostgreSQL archive checksum mismatch; refusing extraction")
    with zipfile.ZipFile(archive) as source:
        for member in source.infolist():
            if member.filename.startswith(("pgsql/bin/", "pgsql/lib/", "pgsql/share/")) or (
                member.filename.count("/") == 1 and "license" in member.filename.lower()
            ):
                if not (ROOT / member.filename).resolve().is_relative_to(ROOT.resolve()):
                    raise RuntimeError("unsafe archive member")
                source.extract(member, ROOT)
    print("Pinned PostgreSQL runtime verified and extracted.")


if __name__ == "__main__":
    main()
