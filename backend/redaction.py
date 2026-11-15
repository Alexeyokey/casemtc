"""Маскирование секретов и персональных данных в логах и трассах (S4)."""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from core.phone import mask_phone, normalize_phone

SENSITIVE_HEADERS = {
    "authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key",
    "x-client-token", "x-archive-token", "x-auth-token", "x-csrf-token", "x-xsrf-token",
}
_SENSITIVE_FIELDS = {
    "api_key", "api-key", "apikey", "access_token", "access-token", "accesstoken",
    "refresh_token", "refresh-token", "client_token", "archive_token", "secret", "password", "token",
}
_SENSITIVE_KEYS = SENSITIVE_HEADERS | _SENSITIVE_FIELDS
_HEADER_NAMES = "|".join(sorted(SENSITIVE_HEADERS))
_KEY_NAMES = "|".join(sorted(_SENSITIVE_KEYS))
_REDACTED = "[REDACTED]"

# Строковое представление словарей и пар заголовков, в том числе в traceback.
_QUOTED_SECRET = re.compile(
    rf"""(?i)(?P<prefix>(?P<pair>\()?b?(?P<key_quote>["'])(?:{_KEY_NAMES})(?P=key_quote)"""
    r"""\s*(?(pair),|:)\s*b?(?P<value_quote>["']))"""
    r"""(?:\\[^\r\n]|(?!(?P=value_quote))[^\\\r\n])*(?P=value_quote)"""
)
# Cookie может содержать несколько значений. Маскируем строку заголовка целиком.
_PLAIN_HEADER = re.compile(
    rf"(?i)(?:(?<![\w-])|(?<=\\[rn]))((?:(?:{_HEADER_NAMES})[^\S\r\n]*:|"
    r"(?:authorization|proxy-authorization|cookie|set-cookie)[^\S\r\n]*=)[^\S\r\n]*)[^\r\n]+"
)
_SECRET_PATTERNS = [
    (_QUOTED_SECRET, r"\g<prefix>[REDACTED]\g<value_quote>"),
    (_PLAIN_HEADER, r"\1[REDACTED]"),
    (re.compile(r"(?i)\bbearer\s+[\w.\-~=/+]+"), "Bearer [REDACTED]"),
    # Оставляем соседние параметры URL и формат кавычек.
    (re.compile(rf"""(?i)(?<![\w-])((?:{_KEY_NAMES})["']?\s*[:=]\s*)(["']?)[^\s"'&,;]+"""),
     r"\1\2[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}"), "[REDACTED_KEY]"),
]
_EMAIL = re.compile(r"\b[\w.+\-]+@[\w\-]+\.[\w.\-]+\b")
_PHONE = re.compile(r"\+?\d[\d\s\-\(\)]{8,18}\d")

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


def _key_name(key: Any) -> str:
    return (key.decode("ascii", errors="replace") if isinstance(key, bytes) else str(key)).lower()


def _redact_value(key: Any, value: Any) -> Any:
    name = _key_name(key)
    if name in _SENSITIVE_KEYS:
        return _REDACTED.encode() if isinstance(value, bytes) else _REDACTED
    if name == "phone" and isinstance(value, str):
        return mask_phone(normalize_phone(value) or value)
    return redact(value)


def redact(obj: Any) -> Any:
    """Замаскировать текст и вложенные данные, включая пары HTTP-заголовков."""
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, bytes):
        return redact_text(obj.decode("latin-1")).encode("latin-1")
    if isinstance(obj, Mapping):
        return {key: _redact_value(key, value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        if len(obj) == 2 and isinstance(obj[0], (str, bytes)) and _key_name(obj[0]) in _SENSITIVE_KEYS:
            values = [obj[0], _redact_value(obj[0], obj[1])]
        else:
            values = [redact(value) for value in obj]
        return tuple(values) if isinstance(obj, tuple) else values
    return obj


class RedactingFilter(logging.Filter):
    """Фильтр для логгеров: маскирует сообщение и аргументы записи."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.args = redact(record.args)
        if record.name == "uvicorn.access":
            # AccessFormatter распаковывает пять исходных аргументов, включая int статуса.
            record.msg = redact(record.msg)
        else:
            # Секрет может быть разделён между шаблоном и аргументом ("Cookie: %s").
            record.msg = _render_message(redact(record.msg) if not isinstance(record.msg, str) else record.msg,
                                         record.args)
        if "color_message" in record.__dict__:
            record.color_message = (redact(record.color_message) if record.name == "uvicorn.access"
                                    else _render_message(record.color_message, record.args))
        if record.name != "uvicorn.access":
            record.args = ()
        if record.exc_info:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info)
        return True


def _render_message(msg: Any, args: Any) -> str:
    try:
        rendered = str(msg) % args if args else str(msg)
    except (TypeError, ValueError, KeyError):
        # Например, значение секрета для числового %d заменено строкой.
        return "[REDACTED LOG MESSAGE]"
    return redact_text(rendered)


_LOG_FILTER = RedactingFilter()


def install_log_redaction() -> None:
    for handler in logging.getLogger().handlers:
        handler.addFilter(_LOG_FILTER)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "casemtc", "httpx"):
        logger = logging.getLogger(name)
        logger.addFilter(_LOG_FILTER)
        for handler in logger.handlers:
            handler.addFilter(_LOG_FILTER)
