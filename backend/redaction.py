"""Маскирование секретов и персональных данных в логах и трассах (S4)."""
from __future__ import annotations

import logging
import re
from typing import Any

from core.phone import mask_phone, normalize_phone

_SECRET_PATTERNS = [
    # Authorization: Bearer xxx / Basic xxx
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer|basic|token)?\s*[\w\-\.=:/+]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\bbearer\s+[\w\-\.=:/+]{8,}"), "Bearer [REDACTED]"),
    # key=..., api_key: ..., token=..., password=...
    (re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret|password|token|x-api-key)(\"?\s*[:=]\s*\"?)[^\s\"',;]+"),
     r"\1\2[REDACTED]"),
    # OpenAI-like ключи sk-..., ключи вида xxxx-xxxx длиннее 24 символов
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}"), "[REDACTED_KEY]"),
]
_EMAIL = re.compile(r"\b[\w.+\-]+@[\w\-]+\.[\w.\-]+\b")
_PHONE = re.compile(r"\+?\d[\d\s\-\(\)]{8,18}\d")

SENSITIVE_HEADERS = {"authorization", "x-api-key", "x-client-token", "x-archive-token", "cookie", "set-cookie"}


def _mask_phone_match(m: re.Match) -> str:
    raw = m.group(0)
    normalized = normalize_phone(raw)
    return mask_phone(normalized) if normalized else raw


def redact_text(text: str) -> str:
    if not text:
        return text
    out = text
    for pat, repl in _SECRET_PATTERNS:
        out = pat.sub(repl, out)
    out = _EMAIL.sub("[EMAIL]", out)
    out = _PHONE.sub(_mask_phone_match, out)
    return out


def redact(obj: Any) -> Any:
    """Рекурсивно замаскировать строки в dict/list; значения чувствительных
    заголовков/ключей заменяются целиком."""
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    if isinstance(obj, tuple):
        return tuple(redact(x) for x in obj)
    if isinstance(obj, dict):
        res = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if lk in SENSITIVE_HEADERS or lk in {"api_key", "password", "client_token", "token"}:
                res[k] = "[REDACTED]"
            elif lk == "phone" and isinstance(v, str):
                res[k] = mask_phone(normalize_phone(v) or v)
            else:
                res[k] = redact(v)
        return res
    return obj


class RedactingFilter(logging.Filter):
    """Фильтр для логгеров: маскирует сообщение и аргументы записи."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Структуру args не меняем: форматтеры (например, uvicorn.access) распаковывают
        # их по позициям. Маскируем каждый строковый аргумент и шаблон сообщения.
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact_text(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: redact_text(v) if isinstance(v, str) else v for k, v in record.args.items()}
        if record.exc_info:  # текст исключения может содержать ключ/телефон
            record.exc_text = redact_text(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def install_log_redaction() -> None:
    f = RedactingFilter()
    root = logging.getLogger()
    for h in root.handlers:
        h.addFilter(f)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "casemtc", "httpx"):
        logging.getLogger(name).addFilter(f)
