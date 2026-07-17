from __future__ import annotations

from typing import Any

from sheets.sheet_manager import SheetManager


class ErrorLogger:
    def __init__(self, sheet_manager: SheetManager) -> None:
        self.sheet_manager = sheet_manager

    def log_model_error(
        self,
        *,
        run_id: str,
        input_id: str,
        model_name: str,
        result: dict[str, Any],
    ) -> None:
        if result.get("status") != "ERROR":
            return

        self.sheet_manager.append_error_log(
            run_id=run_id,
            input_id=input_id,
            model_name=model_name,
            error_type=str(result.get("error_type", "MODEL_ERROR") or "MODEL_ERROR"),
            error_message=str(result.get("error", "")),
            raw_response_preview=str(result.get("raw_response", "")),
        )
