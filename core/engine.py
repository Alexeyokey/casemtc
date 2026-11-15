"""Интерфейс ядра консультации и загрузка реализации.

Реализацию пишет C1–C4 (Максим) с поиском R3 (Влад). Backend и solution.py
работают с любым объектом, удовлетворяющим ConsultationEngine.
"""
from __future__ import annotations

import importlib
import os
from typing import Protocol, runtime_checkable

from .contracts import ConsultationRequest, ConsultationResult

DEFAULT_ENGINE = "core.stub_engine:StubEngine"


@runtime_checkable
class ConsultationEngine(Protocol):
    def consult(self, request: ConsultationRequest) -> ConsultationResult:
        """Один ход. Чистая функция от request: никакого скрытого глобального
        состояния между сессиями, никакого чтения архива."""
        ...


def load_engine(spec: str | None = None) -> ConsultationEngine:
    """Загрузить engine по строке 'package.module:FactoryOrClass'.

    По умолчанию — env CONSULTATION_ENGINE, иначе синтетический StubEngine.
    """
    spec = spec or os.getenv("CONSULTATION_ENGINE") or DEFAULT_ENGINE
    module_name, _, attr = spec.partition(":")
    if not attr:
        raise ValueError(f"engine spec must look like 'module:Factory', got {spec!r}")
    factory = getattr(importlib.import_module(module_name), attr)
    engine = factory()
    if not isinstance(engine, ConsultationEngine):
        raise TypeError(f"{spec} does not implement ConsultationEngine.consult")
    return engine
