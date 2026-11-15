"""Нормализация и проверка телефона (S3). Без внешних зависимостей."""
from __future__ import annotations

import re
from typing import Optional

_PHONE_CANDIDATE = re.compile(r"(?:\+?\d[\d\s\-\(\)]{8,18}\d)")


def normalize_phone(raw: Optional[str]) -> Optional[str]:
    """Вернуть телефон в E.164 или None, если номер невалиден.

    Поддерживаются российские номера (+7/8 и 10 цифр, начиная с 3/4/8/9)
    и международные номера с явным '+' (11–15 цифр).
    """
    if not raw:
        return None
    s = raw.strip()
    has_plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if len(digits) == 11 and digits[0] in "78":
        body = digits[1:]
        return "+7" + body if body[0] in "3489" else None
    if len(digits) == 10 and digits[0] in "3489" and not has_plus:
        return "+7" + digits
    if has_plus and 11 <= len(digits) <= 15 and not digits.startswith("7"):
        return "+" + digits
    return None


def extract_phone(text: str) -> Optional[str]:
    """Найти первый валидный телефон в свободном тексте."""
    for m in _PHONE_CANDIDATE.finditer(text or ""):
        p = normalize_phone(m.group(0))
        if p:
            return p
    return None


def mask_phone(phone: Optional[str]) -> Optional[str]:
    """+79161234567 -> +7916***4567 (для логов и debug)."""
    if not phone:
        return phone
    if len(phone) <= 7:
        return "***"
    return phone[:5] + "***" + phone[-4:]
