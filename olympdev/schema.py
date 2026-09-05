from __future__ import annotations

from typing import Any, Self

from pydantic import BaseModel, ConfigDict

from olympdev.storage.abstract import AbstractStorage


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls.model_validate(data)


class FrozenRuntimeModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
    )


class StorageModel(FrozenModel):
    def upload(self, storage: AbstractStorage) -> str:
        return storage.upload_dictionary(self.to_dict())

    @classmethod
    def from_storage(
        cls,
        storage: AbstractStorage,
        digest: str,
    ) -> Self | None:
        data = storage.download_dictionary(digest)
        if data is None:
            return None
        return cls.from_dict(data)

    @classmethod
    def require_from_storage(
        cls,
        storage: AbstractStorage,
        digest: str,
        *,
        missing: str = "missing input",
    ) -> Self:
        loaded = cls.from_storage(storage, digest)
        if loaded is None:
            raise ValueError(missing)
        return loaded
