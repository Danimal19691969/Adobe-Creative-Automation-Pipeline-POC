"""Storage adapter abstraction. Local-only impl for the PoC; cloud adapters
(S3/GCS/Azure/Dropbox) would slot in here behind the same interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class StorageAdapter(ABC):
    @abstractmethod
    def write(self, path: str, content: bytes) -> str: ...

    @abstractmethod
    def read(self, path: str) -> bytes: ...

    @abstractmethod
    def exists(self, path: str) -> bool: ...


class LocalStorageAdapter(StorageAdapter):
    """Filesystem-backed adapter."""

    def write(self, path: str, content: bytes) -> str:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return str(p)

    def read(self, path: str) -> bytes:
        return Path(path).read_bytes()

    def exists(self, path: str) -> bool:
        return Path(path).exists()
