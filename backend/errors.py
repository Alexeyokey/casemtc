from __future__ import annotations

from typing import Optional


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, *,
                 request_id: Optional[str] = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.request_id = request_id
        self.retryable = retryable

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message,
                          "request_id": self.request_id, "retryable": self.retryable}}
