from __future__ import annotations

from typing import Any

from config import AppConfig
from sheets.sheet_manager import SheetManager, done_record_completion_issues, utc_now_iso


class AuditService:
    """Builds a read-only consistency report from input rows and Qwen output rows."""

    def __init__(self, config: AppConfig, sheet_manager: SheetManager) -> None:
        self.config = config
        self.sheet_manager = sheet_manager

    def generate(self, run_id: str) -> dict[str, Any]:
        input_rows = self.sheet_manager.get_input_rows(allow_duplicates=True)
        output_records = self.sheet_manager.get_output_records()
        input_ids = [row["input_id"] for row in input_rows]
        output_by_input_id: dict[str, list[dict[str, str]]] = {}
        for record in output_records:
            if record.get("input_id") in input_ids:
                output_by_input_id.setdefault(record["input_id"], []).append(record)

        missing_output_rows = len([input_id for input_id in input_ids if input_id not in output_by_input_id])
        duplicate_output_rows = sum(max(0, len(records) - 1) for records in output_by_input_id.values())
        duplicate_input_ids = self.sheet_manager.duplicate_input_ids_count + duplicate_output_rows
        incomplete_done_rows = sum(1 for record in output_records if _done_record_has_missing_fields(record))

        all_done = (
            len(input_ids) > 0
            and missing_output_rows == 0
            and duplicate_input_ids == 0
            and incomplete_done_rows == 0
            and all(
                len(output_by_input_id.get(input_id, [])) == 1
                and output_by_input_id[input_id][0].get("Final_Row_Status") == "DONE"
                for input_id in input_ids
            )
        )

        audit = {
            "run_id": run_id,
            "active_model": self.config.qwen_model_name,
            "prompt_version": self.config.prompt_version,
            "total_input_rows": len(input_rows),
            "total_output_rows": len(output_records),
            "done_rows": _count_records(output_records, "Final_Row_Status", "DONE"),
            "error_rows": _count_records(output_records, "Final_Row_Status", "ERROR"),
            "incomplete_done_rows": incomplete_done_rows,
            "missing_output_rows": missing_output_rows,
            "duplicate_input_ids": duplicate_input_ids,
            "qwen_done": _count_records(output_records, "Qwen_Status", "DONE"),
            "qwen_error": _count_records(output_records, "Qwen_Status", "ERROR"),
            "final_status": "COMPLETE" if all_done else "NEEDS_REVIEW",
            "audit_time": utc_now_iso(),
            "output_sheet_url": self.sheet_manager.output_sheet_url,
        }
        self.sheet_manager.write_audit_report(audit)
        return audit


def _count_records(records: list[dict[str, str]], field: str, value: str) -> int:
    return sum(1 for record in records if str(record.get(field, "")).strip() == value)


def _done_record_has_missing_fields(record: dict[str, str]) -> bool:
    return bool(done_record_completion_issues(record))


# Gemini reference only. Previous audit reports counted Gemini_Status.
# This Qwen-only build audits Qwen_Status and Final_Row_Status only.
