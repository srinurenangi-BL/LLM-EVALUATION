from __future__ import annotations

from typing import Any


EvaluationResult = dict[str, Any]


def done_result(parsed: Any, raw_response: str, attempts_used: int) -> EvaluationResult:
    return {
        "status": "DONE",
        "parsed": parsed,
        "raw_response": raw_response,
        "error": "",
        "error_type": "",
        "attempts_used": attempts_used,
    }


def error_result(
    *,
    error_message: str,
    error_type: str,
    raw_response: str = "",
    attempts_used: int = 0,
) -> EvaluationResult:
    return {
        "status": "ERROR",
        "parsed": None,
        "raw_response": raw_response,
        "error": error_message,
        "error_type": error_type,
        "attempts_used": attempts_used,
    }
