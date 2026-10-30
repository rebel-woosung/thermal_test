"""TARGET laws, one per file: the FAN_TARGET_CONTROL name runs the TargetControl subclass in <name>.py."""

from __future__ import annotations

import importlib
import pkgutil
from abc import ABC, abstractmethod
from typing import Any

_CONTROLS: dict[str, type[TargetControl]] = {}


class TargetControl(ABC):
    """Base class of a TARGET law; each subclass registers under its file name."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        _CONTROLS[cls.__module__.rpartition(".")[2]] = cls

    @abstractmethod
    def reset(self, now: float, duty: int) -> None:
        """Start a TARGET phase at ``duty``."""

    @abstractmethod
    def update(
        self, now: float, temps: dict[str, float], targets: dict[str, float], duty: int
    ) -> int:
        """Next NPU duty; called each poll while some card has both a Card Temp and a target."""


def available() -> list[str]:
    return sorted(module.name for module in pkgutil.iter_modules(__path__))


def create(name: str) -> TargetControl:
    if name not in available():
        raise ValueError(f"FAN_TARGET_CONTROL {name!r} is not one of {available()}")
    importlib.import_module(f"{__name__}.{name}")
    return _CONTROLS[name]()


def validate(names: list[str]) -> None:
    """Load every named law now, so a typo or a broken law file stops the run before it starts."""
    for name in names:
        create(name)
