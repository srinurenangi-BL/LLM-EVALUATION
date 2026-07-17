from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import gspread

from config import AppConfig
from sheets.google_sheets_client import GoogleSheetsClient


OUTPUT_TAB_NAME = "Qwen_Evaluation"
AUDIT_TAB_NAME = "Audit_Report"
ERROR_TAB_NAME = "Error_Log"
CONFIG_TAB_NAME = "Config"
GOOGLE_SHEETS_CELL_LIMIT = 50_000
TRUNCATED_SUFFIX = "\n...[TRUNCATED FOR GOOGLE SHEETS CELL LIMIT]"

REQUIRED_BASE_INPUT_COLUMNS = ["QSN No", "User ID", "Question"]
SUPPORTED_STUDENT_CODE_COLUMNS = ["Student code", "Student Code", "Actual Code"]

QWEN_HEADERS = [
    "input_id",
    "QSN No",
    "User ID",
    "Question",
    "Student code",
    "Code Logic",
    "Completeness",
    "Code Quality",
    "Approach Taken",
    "Overall",
    "Overall Score",
    "Quality Label",
    "Common_Errors",
    "Strengths",
    "Weaknesses",
    "Recommendations",
    "Correctness_Feedback",
    "Corrected_Code",
    "Final_Row_Status",
]

DONE_REQUIRED_HEADERS = [
    "input_id",
    "QSN No",
    "User ID",
    "Question",
    "Student code",
    "Code Logic",
    "Completeness",
    "Code Quality",
    "Approach Taken",
    "Overall",
    "Overall Score",
    "Quality Label",
    "Common_Errors",
    "Strengths",
    "Weaknesses",
    "Recommendations",
    "Correctness_Feedback",
    "Corrected_Code",
    "Final_Row_Status",
]

AUDIT_HEADERS = [
    "run_id",
    "active_model",
    "prompt_version",
    "total_input_rows",
    "total_output_rows",
    "done_rows",
    "error_rows",
    "incomplete_done_rows",
    "missing_output_rows",
    "duplicate_input_ids",
    "qwen_done",
    "qwen_error",
    "final_status",
    "audit_time",
    "output_sheet_url",
]

ERROR_HEADERS = [
    "timestamp",
    "run_id",
    "input_id",
    "model_name",
    "error_type",
    "error_message",
    "raw_response_preview",
]

CONFIG_HEADERS = ["key", "value", "updated_at"]


class SheetManager:
    """Google Sheets access, Qwen output setup, and resume-safe writes."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.google = GoogleSheetsClient(config)
        self.input_spreadsheet = self.google.open_by_url(config.input_sheet_url)
        self.input_worksheet = self.input_spreadsheet.worksheet(config.input_tab_name)
        self.output_spreadsheet = self._open_or_create_output_spreadsheet()
        self.student_code_source_column = ""
        self.duplicate_input_ids_count = 0

        self.output_ws = self._ensure_worksheet(OUTPUT_TAB_NAME, rows=1000, cols=len(QWEN_HEADERS) + 4)
        self.audit_ws = self._ensure_worksheet(AUDIT_TAB_NAME, rows=50, cols=len(AUDIT_HEADERS) + 4)
        self.error_ws = self._ensure_worksheet(ERROR_TAB_NAME, rows=1000, cols=len(ERROR_HEADERS) + 2)
        self.config_ws = self._ensure_worksheet(CONFIG_TAB_NAME, rows=50, cols=len(CONFIG_HEADERS) + 2)

        self._ensure_qwen_headers()
        self._ensure_headers(self.audit_ws, AUDIT_HEADERS)
        self._ensure_headers(self.error_ws, ERROR_HEADERS)
        self._ensure_headers(self.config_ws, CONFIG_HEADERS)
        self.style_all_tabs()

    @property
    def output_sheet_url(self) -> str:
        return self.output_spreadsheet.url

    def get_input_rows(self, *, allow_duplicates: bool = False) -> list[dict[str, str]]:
        values = self.input_worksheet.get_all_values()
        if not values:
            raise ValueError("Input worksheet is empty")

        stripped_headers = [str(header).strip() for header in values[0]]
        missing = [column for column in REQUIRED_BASE_INPUT_COLUMNS if column not in stripped_headers]
        if missing:
            raise ValueError(
                "Input sheet is missing required columns after stripping spaces: "
                f"{', '.join(missing)}"
            )

        code_column = _first_available(stripped_headers, SUPPORTED_STUDENT_CODE_COLUMNS)
        if not code_column:
            raise ValueError(
                "Input sheet is missing a supported student code column. Expected one of: "
                f"{', '.join(SUPPORTED_STUDENT_CODE_COLUMNS)}"
            )
        self.student_code_source_column = code_column

        header_indexes = {
            "QSN No": stripped_headers.index("QSN No"),
            "User ID": stripped_headers.index("User ID"),
            "Question": stripped_headers.index("Question"),
            code_column: stripped_headers.index(code_column),
        }

        input_rows: list[dict[str, str]] = []
        seen_input_ids: dict[str, int] = {}
        duplicate_count = 0

        for row_number, row in enumerate(values[1:], start=2):
            qsn_no = _safe_cell(row, header_indexes["QSN No"])
            user_id = _safe_cell(row, header_indexes["User ID"])
            question = _safe_cell(row, header_indexes["Question"])
            student_code = _safe_cell(row, header_indexes[code_column])

            if all(_is_empty_value(value) for value in [qsn_no, user_id, question, student_code]):
                continue

            missing_fields = [
                field_name
                for field_name, value in {
                    "QSN No": qsn_no,
                    "User ID": user_id,
                    "Question": question,
                    code_column: student_code,
                }.items()
                if _is_empty_value(value)
            ]
            if missing_fields:
                raise ValueError(
                    f"Input row {row_number} has empty required field(s): "
                    f"{', '.join(missing_fields)}"
                )

            input_id = build_input_id(qsn_no, user_id)
            if input_id in seen_input_ids:
                duplicate_count += 1
                if not allow_duplicates:
                    first_row = seen_input_ids[input_id]
                    raise ValueError(
                        "Duplicate input_id found in input sheet after building "
                        f"QSN/User key: {input_id!r}. Rows {first_row} and {row_number} "
                        "would overwrite each other in the output sheet."
                    )
                continue

            seen_input_ids[input_id] = row_number
            input_rows.append(
                {
                    "input_id": input_id,
                    "qsn_no": qsn_no,
                    "user_id": user_id,
                    "question": question,
                    "student_code": student_code,
                }
            )

        self.duplicate_input_ids_count = duplicate_count
        return input_rows

    def initialize_output_sheet_with_inputs(self, input_rows: list[dict[str, str]]) -> None:
        self._ensure_qwen_headers()
        rows_to_write = []
        for r in input_rows:
            row = [
                r["input_id"],
                r["qsn_no"],
                r["user_id"],
                r["question"],
                r["student_code"],
            ] + [""] * 14 + ["PENDING"]
            rows_to_write.append(row)
        if rows_to_write:
            self.output_ws.append_rows(rows_to_write, value_input_option="RAW")
            self._style_qwen_tab()
            print(f"Pre-loaded {len(input_rows)} student submissions into the output sheet.")

    def clear_output_rows(self) -> None:
        try:
            self.output_ws.clear()
            self.output_ws.update(range_name="A1", values=[QWEN_HEADERS], value_input_option="USER_ENTERED")
            print("Cleared all output evaluated rows.")
        except Exception as exc:
            print(f"Warning: could not clear output evaluated rows: {exc}")

    def get_existing_output_map(self) -> dict[str, dict[str, Any]]:
        values = self.output_ws.get_all_values()
        if len(values) < 2:
            return {}

        header = values[0]
        try:
            input_id_index = header.index("input_id")
            status_index = header.index("Final_Row_Status")
        except ValueError:
            return {}

        output_map: dict[str, dict[str, Any]] = {}
        for row_number, row in enumerate(values[1:], start=2):
            input_id = _safe_cell(row, input_id_index)
            if not input_id:
                continue
            record = {column_name: _safe_cell(row, index) for index, column_name in enumerate(header)}
            final_row_status = _safe_cell(row, status_index)
            completion_issues = done_record_completion_issues(record)
            missing_required_fields = (
                [
                    field
                    for field in DONE_REQUIRED_HEADERS
                    if not str(record.get(field, "")).strip()
                ]
                if final_row_status == "DONE"
                else []
            )
            output_map[input_id] = {
                "row_number": row_number,
                "qwen_status": final_row_status,
                "final_row_status": final_row_status,
                "created_at": "",
                "missing_required_fields": missing_required_fields,
                "completion_issues": completion_issues,
                "is_complete_done": final_row_status == "DONE" and not completion_issues,
                "record": record,
            }
        return output_map

    def get_config_value(self, key: str) -> str:
        values = self.config_ws.get_all_values()
        for row in values:
            if row and row[0] == key:
                return row[1] if len(row) > 1 else ""
        return ""

    def set_config_value(self, key: str, value: str) -> None:
        values = self.config_ws.get_all_values()
        now_str = utc_now_iso()
        for idx, row in enumerate(values, start=1):
            if row and row[0] == key:
                self.config_ws.update(range_name=f"B{idx}:C{idx}", values=[[value, now_str]], value_input_option="USER_ENTERED")
                return
        self.config_ws.append_row([key, value, now_str], value_input_option="USER_ENTERED")


    def get_output_records(self) -> list[dict[str, str]]:
        values = self.output_ws.get_all_values()
        if len(values) < 2:
            return []
        headers = values[0]
        records: list[dict[str, str]] = []
        for row in values[1:]:
            record = {header: _safe_cell(row, index) for index, header in enumerate(headers)}
            if record.get("input_id"):
                records.append(record)
        return records

    def write_or_update_output_row(self, row_values: list[Any], input_id: str) -> int:
        row_values = [_safe_sheet_value(value) for value in _fit_row_to_headers(row_values, QWEN_HEADERS)]
        existing = self.get_existing_output_map().get(input_id)
        if existing:
            row_number = int(existing["row_number"])
            self.output_ws.update(
                range_name=f"A{row_number}",
                values=[row_values],
                value_input_option="USER_ENTERED",
            )
            return row_number

        self.output_ws.append_row(row_values, value_input_option="USER_ENTERED")
        return len(self.output_ws.get_all_values())

    def append_error_log(
        self,
        *,
        run_id: str,
        input_id: str,
        model_name: str,
        error_type: str,
        error_message: str,
        raw_response_preview: str,
    ) -> None:
        self.error_ws.append_row(
            [
                utc_now_iso(),
                run_id,
                input_id,
                model_name,
                error_type,
                _safe_sheet_value(error_message, max_length=10_000),
                _safe_sheet_value(raw_response_preview, max_length=10_000),
            ],
            value_input_option="USER_ENTERED",
        )

    def write_audit_report(self, audit_values: dict[str, Any]) -> None:
        row = [audit_values.get(header, "") for header in AUDIT_HEADERS]
        self.audit_ws.clear()
        self.audit_ws.update(
            range_name="A1",
            values=[AUDIT_HEADERS, row],
            value_input_option="USER_ENTERED",
        )

    def write_config(self, run_id: str, total_clean_input_rows: int) -> None:
        now = utc_now_iso()
        rows = [
            CONFIG_HEADERS,
            ["run_id", run_id, now],
            ["active_model", self.config.qwen_model_name, now],
            ["ollama_chat_url", self.config.ollama_chat_url, now],
            ["prompt_version", self.config.prompt_version, now],
            ["input_sheet_url", self.config.input_sheet_url, now],
            ["input_tab_name", self.config.input_tab_name, now],
            ["output_sheet_url", self.output_sheet_url, now],
            ["output_tab_name", OUTPUT_TAB_NAME, now],
            ["student_code_source_column", self.student_code_source_column, now],
            ["total_clean_input_rows", total_clean_input_rows, now],
            ["last_run_at", now, now],
        ]
        self.config_ws.clear()
        self.config_ws.update(
            range_name="A1",
            values=rows,
            value_input_option="USER_ENTERED",
        )

    def style_all_tabs(self) -> None:
        requests: list[dict[str, Any]] = []
        requests.extend(_qwen_tab_style_requests(self.output_ws.id))
        requests.extend(_simple_tab_style_requests(self.audit_ws.id, len(AUDIT_HEADERS)))
        requests.extend(_simple_tab_style_requests(self.error_ws.id, len(ERROR_HEADERS)))
        requests.extend(_simple_tab_style_requests(self.config_ws.id, len(CONFIG_HEADERS)))

        try:
            requests.extend(_conditional_format_reset_requests(self.output_spreadsheet, self.output_ws.id))
        except Exception as exc:
            print(f"Warning: could not prepare conditional formatting for {OUTPUT_TAB_NAME}: {exc}")

        self._batch_update_style_requests(requests, "style workbook")

    def _open_or_create_output_spreadsheet(self):
        if self.config.output_sheet_url:
            return self.google.open_by_url(self.config.output_sheet_url)
        spreadsheet = self.google.create(self.config.output_sheet_title)
        _persist_output_sheet_url(spreadsheet.url)
        return spreadsheet

    def _ensure_worksheet(self, title: str, rows: int, cols: int):
        try:
            return self.output_spreadsheet.worksheet(title)
        except gspread.WorksheetNotFound:
            return self.output_spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)

    def _ensure_qwen_headers(self) -> None:
        values = self.output_ws.get_all_values()
        current_header = _trim_trailing_empty(values[0]) if values else []
        if current_header and current_header[0] == "":
            current_header[0] = "input_id"
        if current_header == QWEN_HEADERS:
            return

        has_existing_data = any(any(str(cell).strip() for cell in row) for row in values[1:])
        if has_existing_data:
            raise ValueError(
                f"{OUTPUT_TAB_NAME} already contains data but its header does not match "
                "the Qwen-only output schema. Refusing to overwrite existing evaluated data."
            )

        self._clear_first_row(self.output_ws)
        self.output_ws.update(range_name="A1", values=[QWEN_HEADERS], value_input_option="USER_ENTERED")

    def _ensure_headers(self, worksheet: Any, headers: list[str]) -> None:
        values = worksheet.get_all_values()
        current_header = _trim_trailing_empty(values[0]) if values else []
        if current_header == headers:
            return
        self._clear_first_row(worksheet)
        worksheet.update(range_name="A1", values=[headers], value_input_option="USER_ENTERED")

    def _style_qwen_tab(self) -> None:
        requests = _qwen_tab_style_requests(self.output_ws.id)
        try:
            requests.extend(_conditional_format_reset_requests(self.output_spreadsheet, self.output_ws.id))
        except Exception as exc:
            print(f"Warning: could not prepare conditional formatting for {OUTPUT_TAB_NAME}: {exc}")
        self._batch_update_style_requests(requests, f"style {OUTPUT_TAB_NAME}")

    def _style_simple_tab(self, worksheet: Any, header_count: int) -> None:
        self._batch_update_style_requests(
            _simple_tab_style_requests(worksheet.id, header_count),
            "style worksheet",
        )

    def _batch_update_style_requests(self, requests: list[dict[str, Any]], action: str) -> None:
        if not requests:
            return
        try:
            self.output_spreadsheet.batch_update({"requests": requests})
        except Exception as exc:
            print(f"Warning: could not {action}: {exc}")

    @staticmethod
    def _clear_first_row(worksheet: Any) -> None:
        try:
            worksheet.batch_clear(["1:1"])
        except Exception as exc:
            print(f"Warning: could not clear existing header row: {exc}")


def build_input_id(qsn_no: str, user_id: str) -> str:
    return f"QSN_{str(qsn_no).strip()}_USER_{str(user_id).strip()}"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def list_to_sheet_json(values: list[str]) -> str:
    return json.dumps(values, ensure_ascii=False)


def _safe_cell(row: list[str], index: int) -> str:
    if index >= len(row):
        return ""
    return str(row[index]).strip()


def _is_empty_value(value: str) -> bool:
    text = str(value).strip()
    return not text or text.lower() in {"nan", "none", "null"}


def _first_available(headers: list[str], candidates: list[str]) -> str:
    for candidate in candidates:
        if candidate in headers:
            return candidate
    return ""


def _fit_row_to_headers(row_values: list[Any], headers: list[str]) -> list[Any]:
    values = list(row_values)
    if len(values) < len(headers):
        values.extend([""] * (len(headers) - len(values)))
    return values[: len(headers)]


def _trim_trailing_empty(values: list[str]) -> list[str]:
    trimmed = list(values)
    while trimmed and not str(trimmed[-1]).strip():
        trimmed.pop()
    return trimmed


def _safe_sheet_value(value: Any, *, max_length: int = GOOGLE_SHEETS_CELL_LIMIT) -> Any:
    if not isinstance(value, str):
        return value
    if len(value) <= max_length:
        return value
    allowed = max(0, max_length - len(TRUNCATED_SUFFIX))
    return value[:allowed] + TRUNCATED_SUFFIX


def done_record_completion_issues(record: dict[str, Any]) -> list[str]:
    if str(record.get("Final_Row_Status", "")).strip() != "DONE":
        return []

    issues: list[str] = []
    for field in DONE_REQUIRED_HEADERS:
        if not str(record.get(field, "")).strip():
            issues.append(f"missing required output field {field!r}")

    corrected_code = str(record.get("Corrected_Code", "")).strip()
    if "[No corrected code provided by evaluator]" in corrected_code or "// Student submission is incorrect" in corrected_code:
        issues.append("Corrected_Code contains temporary placeholder text")

    return issues



def _header_format(background_color: dict[str, float]) -> dict[str, Any]:
    return {
        "backgroundColor": background_color,
        "horizontalAlignment": "CENTER",
        "verticalAlignment": "MIDDLE",
        "wrapStrategy": "WRAP",
        "textFormat": {
            "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
            "bold": True,
        },
    }


def _feedback_cell_format() -> dict[str, Any]:
    return {
        "horizontalAlignment": "LEFT",
        "verticalAlignment": "TOP",
        "wrapStrategy": "WRAP",
    }


def _code_cell_format() -> dict[str, Any]:
    return {
        "horizontalAlignment": "LEFT",
        "verticalAlignment": "TOP",
        "wrapStrategy": "WRAP",
        "textFormat": {
            "fontFamily": "Roboto Mono",
            "fontSize": 9,
        },
    }


def _qwen_tab_style_requests(sheet_id: int) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = [
        _freeze_request(sheet_id, frozen_rows=1, frozen_columns=5),
        _basic_filter_request(sheet_id, end_column_index=len(QWEN_HEADERS)),
        _repeat_cell_request(
            sheet_id,
            start_row=0,
            end_row=1,
            start_col=0,
            end_col=5,
            cell_format=_header_format({"red": 0.0, "green": 0.12, "blue": 0.32}),
        ),
        _repeat_cell_request(
            sheet_id,
            start_row=0,
            end_row=1,
            start_col=5,
            end_col=19,
            cell_format=_header_format({"red": 0.0, "green": 0.38, "blue": 0.34}),
        ),
        _repeat_cell_request(
            sheet_id,
            start_row=0,
            end_row=1,
            start_col=19,
            end_col=len(QWEN_HEADERS),
            cell_format=_header_format({"red": 0.12, "green": 0.12, "blue": 0.14}),
        ),
        _repeat_cell_request(
            sheet_id,
            start_row=1,
            end_row=1000,
            start_col=0,
            end_col=len(QWEN_HEADERS),
            cell_format={
                "wrapStrategy": "WRAP",
                "verticalAlignment": "TOP",
                "borders": _light_borders(),
            },
        ),
    ]

    centered_format = {
        "horizontalAlignment": "CENTER",
        "verticalAlignment": "MIDDLE",
    }
    for start_col, end_col in [(0, 3), (6, 9), (10, 12), (19, 20)]:
        requests.append(
            _repeat_cell_request(
                sheet_id,
                start_row=1,
                end_row=1000,
                start_col=start_col,
                end_col=end_col,
                cell_format=centered_format,
            )
        )

    for start_col, end_col in [(3, 4), (5, 6), (9, 10), (12, 18)]:
        requests.append(
            _repeat_cell_request(
                sheet_id,
                start_row=1,
                end_row=1000,
                start_col=start_col,
                end_col=end_col,
                cell_format=_feedback_cell_format(),
            )
        )

    for start_col, end_col in [(4, 5), (18, 19)]:
        requests.append(
            _repeat_cell_request(
                sheet_id,
                start_row=1,
                end_row=1000,
                start_col=start_col,
                end_col=end_col,
                cell_format=_code_cell_format(),
            )
        )

    for start_col, end_col in [(6, 9), (10, 11)]:
        requests.append(
            _repeat_cell_request(
                sheet_id,
                start_row=1,
                end_row=1000,
                start_col=start_col,
                end_col=end_col,
                cell_format={"numberFormat": {"type": "NUMBER", "pattern": "0.##"}},
            )
        )

    requests.append(_row_height_request(sheet_id, row_index=0, pixel_size=68))
    for start_col, end_col, pixel_size in [
        (0, 1, 210),
        (1, 3, 120),
        (3, 4, 360),
        (4, 5, 420),
        (5, 6, 320),
        (6, 9, 95),
        (9, 10, 280),
        (10, 12, 115),
        (12, 18, 260),
        (18, 19, 520),
        (19, len(QWEN_HEADERS), 150),
    ]:
        requests.append(_column_width_request(sheet_id, start_col, end_col, pixel_size))

    return requests


def _simple_tab_style_requests(sheet_id: int, header_count: int) -> list[dict[str, Any]]:
    return [
        _freeze_request(sheet_id, frozen_rows=1, frozen_columns=0),
        _basic_filter_request(sheet_id, end_column_index=header_count),
        _repeat_cell_request(
            sheet_id,
            start_row=0,
            end_row=1,
            start_col=0,
            end_col=header_count,
            cell_format=_header_format({"red": 0.12, "green": 0.12, "blue": 0.14}),
        ),
        _repeat_cell_request(
            sheet_id,
            start_row=1,
            end_row=1000,
            start_col=0,
            end_col=header_count,
            cell_format={"wrapStrategy": "WRAP", "borders": _light_borders()},
        ),
    ]


def _conditional_format_reset_requests(spreadsheet: Any, sheet_id: int) -> list[dict[str, Any]]:
    metadata = spreadsheet.fetch_sheet_metadata()
    rule_count = 0
    for sheet in metadata.get("sheets", []):
        if sheet.get("properties", {}).get("sheetId") == sheet_id:
            rule_count = len(sheet.get("conditionalFormats", []))
            break

    requests = [
        {"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": index}}
        for index in reversed(range(rule_count))
    ]
    requests.extend(_conditional_format_requests(sheet_id))
    return requests


def _freeze_request(sheet_id: int, *, frozen_rows: int, frozen_columns: int) -> dict[str, Any]:
    return {
        "updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id,
                "gridProperties": {
                    "frozenRowCount": frozen_rows,
                    "frozenColumnCount": frozen_columns,
                },
            },
            "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
        }
    }


def _basic_filter_request(sheet_id: int, *, end_column_index: int) -> dict[str, Any]:
    return {
        "setBasicFilter": {
            "filter": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "startColumnIndex": 0,
                    "endColumnIndex": end_column_index,
                }
            }
        }
    }


def _repeat_cell_request(
    sheet_id: int,
    *,
    start_row: int,
    end_row: int,
    start_col: int,
    end_col: int,
    cell_format: dict[str, Any],
) -> dict[str, Any]:
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": start_row,
                "endRowIndex": end_row,
                "startColumnIndex": start_col,
                "endColumnIndex": end_col,
            },
            "cell": {"userEnteredFormat": cell_format},
            "fields": _format_fields(cell_format),
        }
    }


def _row_height_request(sheet_id: int, *, row_index: int, pixel_size: int) -> dict[str, Any]:
    return _dimension_request(
        sheet_id,
        dimension="ROWS",
        start_index=row_index,
        end_index=row_index + 1,
        pixel_size=pixel_size,
    )


def _column_width_request(sheet_id: int, start_col: int, end_col: int, pixel_size: int) -> dict[str, Any]:
    return _dimension_request(
        sheet_id,
        dimension="COLUMNS",
        start_index=start_col,
        end_index=end_col,
        pixel_size=pixel_size,
    )


def _dimension_request(
    sheet_id: int,
    *,
    dimension: str,
    start_index: int,
    end_index: int,
    pixel_size: int,
) -> dict[str, Any]:
    return {
        "updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": dimension,
                "startIndex": start_index,
                "endIndex": end_index,
            },
            "properties": {"pixelSize": pixel_size},
            "fields": "pixelSize",
        }
    }


def _format_fields(cell_format: dict[str, Any]) -> str:
    return ",".join(f"userEnteredFormat.{field}" for field in cell_format)


def _light_borders() -> dict[str, Any]:
    border = {
        "style": "SOLID",
        "width": 1,
        "color": {"red": 0.82, "green": 0.86, "blue": 0.9},
    }
    return {"top": border, "bottom": border, "left": border, "right": border}


def _conditional_format_requests(sheet_id: int) -> list[dict[str, Any]]:
    return [
        _text_condition(sheet_id, 19, "DONE", {"red": 0.82, "green": 0.94, "blue": 0.84}),
        _text_condition(sheet_id, 19, "ERROR", {"red": 0.98, "green": 0.82, "blue": 0.82}),
        _text_condition(sheet_id, 11, "Excellent", {"red": 0.56, "green": 0.82, "blue": 0.58}),
        _text_condition(sheet_id, 11, "Good", {"red": 0.76, "green": 0.9, "blue": 0.88}),
        _text_condition(sheet_id, 11, "Average", {"red": 1.0, "green": 0.91, "blue": 0.62}),
        _text_condition(sheet_id, 11, "Poor", {"red": 1.0, "green": 0.8, "blue": 0.55}),
        _text_condition(sheet_id, 11, "Very Poor", {"red": 0.96, "green": 0.62, "blue": 0.62}),
    ]


def _text_condition(sheet_id: int, column_index: int, text: str, color: dict[str, float]) -> dict[str, Any]:
    return {
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [
                    {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "startColumnIndex": column_index,
                        "endColumnIndex": column_index + 1,
                    }
                ],
                "booleanRule": {
                    "condition": {
                        "type": "TEXT_EQ",
                        "values": [{"userEnteredValue": text}],
                    },
                    "format": {"backgroundColor": color},
                },
            },
            "index": 0,
        }
    }


def _persist_output_sheet_url(output_sheet_url: str) -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
        updated_lines: list[str] = []
        found_key = False
        for line in lines:
            if line.strip().startswith("OUTPUT_SHEET_URL="):
                updated_lines.append(f"OUTPUT_SHEET_URL={output_sheet_url}")
                found_key = True
            else:
                updated_lines.append(line)
        if not found_key:
            updated_lines.append(f"OUTPUT_SHEET_URL={output_sheet_url}")
        env_path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
    except OSError:
        print(
            "Warning: created an output sheet but could not persist OUTPUT_SHEET_URL "
            "to .env. Add the printed output URL to .env before rerunning."
        )
