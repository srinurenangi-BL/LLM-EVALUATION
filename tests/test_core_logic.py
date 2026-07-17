from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from unittest.mock import Mock, patch

import main
from config import ACTIVE_MODEL_NAME, DEFAULT_QWEN_MODEL_NAME, AppConfig
from evaluators.qwen_evaluator import QwenEvaluator
from schemas import calculate_avg_score, clamp_score, normalize_score, quality_label
from services.audit_service import AuditService
from services.evaluation_runner import EvaluationRunner, _output_validation_errors, _safe_evaluate
from services.json_parser import parse_evaluation_json
from sheets.sheet_manager import QWEN_HEADERS, SheetManager, build_input_id
from prompts import build_user_prompt


VALID_RESPONSE = {
    "code_logic": "Uses loops correctly",
    "correctness": "8.5 out of 10",
    "code_quality": "9/10",
    "efficiency": 7,
    "overall": "Mostly correct",
    "common_errors": ["No major errors found"],
    "strengths": ["Readable"],
    "weaknesses": ["Could handle more edge cases"],
    "recommendations": ["Add tests"],
    "correctness_feedback": "Works for normal cases",
    "improvement_suggestions": ["Check null input"],
    "corrected_code": "class Main { public static void main(String[] args) { } }",
}


class ParserAndSchemaTests(unittest.TestCase):
    def test_parses_direct_json(self) -> None:
        parsed = parse_evaluation_json(json.dumps(VALID_RESPONSE))
        self.assertEqual(parsed.correctness, 8.5)
        self.assertEqual(parsed.code_quality, 9.0)

    def test_parses_markdown_fenced_json(self) -> None:
        raw = "```json\n" + json.dumps(VALID_RESPONSE) + "\n```"
        parsed = parse_evaluation_json(raw)
        self.assertEqual(parsed.efficiency, 7.0)

    def test_parses_json_with_surrounding_text(self) -> None:
        raw = "extra text " + json.dumps(VALID_RESPONSE) + " trailing text"
        parsed = parse_evaluation_json(raw)
        self.assertEqual(parsed.overall, "Mostly correct")

    def test_alias_keys_are_normalized(self) -> None:
        data = dict(VALID_RESPONSE)
        data["correctness_score"] = data.pop("correctness")
        data["code-quality-score"] = data.pop("code_quality")
        data["efficiency score"] = data.pop("efficiency")
        data["done_well"] = data.pop("strengths")
        data["suggestions"] = data.pop("improvement_suggestions")
        data["fixed_code"] = data.pop("corrected_code")

        parsed = parse_evaluation_json(json.dumps(data))

        self.assertEqual(parsed.correctness, 8.5)
        self.assertEqual(parsed.strengths, ["Readable"])
        self.assertEqual(parsed.improvement_suggestions, ["Check null input"])

    def test_string_list_conversion_and_empty_fallback(self) -> None:
        data = dict(VALID_RESPONSE)
        data["common_errors"] = ""
        data["recommendations"] = "Use clearer names"
        parsed = parse_evaluation_json(json.dumps(data))
        self.assertEqual(parsed.common_errors, ["No major errors found"])
        self.assertEqual(parsed.recommendations, ["Use clearer names"])

    def test_invalid_json_raises_clear_error(self) -> None:
        with self.assertRaises(ValueError):
            parse_evaluation_json("not json")

    def test_empty_response_raises_clear_error(self) -> None:
        with self.assertRaises(ValueError):
            parse_evaluation_json("")

    def test_corrected_code_is_required(self) -> None:
        data = dict(VALID_RESPONSE)
        data["corrected_code"] = ""
        parsed = parse_evaluation_json(json.dumps(data))
        self.assertEqual(parsed.corrected_code, "[No corrected code provided by evaluator]")


    def test_score_conversion_and_clamping(self) -> None:
        self.assertEqual(normalize_score("8.5 out of 10"), 8.5)
        self.assertEqual(normalize_score("8/10"), 8.0)
        self.assertEqual(clamp_score(12), 10.0)
        self.assertEqual(clamp_score(-1), 0.0)

    def test_avg_score_and_quality_label_are_deterministic(self) -> None:
        avg = calculate_avg_score(8, 7, 6)
        self.assertEqual(avg, 7.0)
        self.assertEqual(quality_label(avg), "Good")
        self.assertEqual(quality_label(9.2), "Excellent")
        self.assertEqual(quality_label(2.99), "Very Poor")

    def test_prompt_includes_all_four_input_values(self) -> None:
        prompt = build_user_prompt("Question text", "class Main {}", qsn_no="5", user_id="student-1")
        self.assertIn("QSN No: 5", prompt)
        self.assertIn("User ID: student-1", prompt)
        self.assertIn("Question text", prompt)
        self.assertIn("class Main {}", prompt)


class ConfigTests(unittest.TestCase):
    def test_active_model_is_qwen_only(self) -> None:
        self.assertEqual(ACTIVE_MODEL_NAME, "qwen")

    def test_qwen_run_does_not_require_gemini_key(self) -> None:
        _valid_config().validate_for_run()

    def test_audit_uses_sheets_validation_only(self) -> None:
        replace(_valid_config(), qwen_model_name="", ollama_chat_url="").validate_for_sheets()

    def test_rejects_hosted_qwen_model(self) -> None:
        with self.assertRaises(ValueError):
            replace(_valid_config(), qwen_model_name="qwen-plus").validate_for_run()

    def test_rejects_non_local_ollama_url(self) -> None:
        with self.assertRaises(ValueError):
            replace(_valid_config(), ollama_chat_url="https://example.com/api/chat").validate_for_run()

    def test_cli_rejects_gemini_model_choice(self) -> None:
        parser = main.build_parser()
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                parser.parse_args(["--model", "gemini", "--check"])


class SheetInputTests(unittest.TestCase):
    def test_student_code_column_detection_and_header_trimming(self) -> None:
        manager = SheetManager.__new__(SheetManager)
        manager.input_worksheet = FakeInputWorksheet(
            [
                ["QSN No", "User ID", "Question ", "Student Code", "Old Score"],
                ["1", "u1", "Write Java", "class Main {}", "ignore"],
                ["", "", "", "", ""],
            ]
        )
        rows = SheetManager.get_input_rows(manager)
        self.assertEqual(manager.student_code_source_column, "Student Code")
        self.assertEqual(rows[0]["input_id"], "QSN_1_USER_u1")
        self.assertEqual(rows[0]["student_code"], "class Main {}")

    def test_actual_code_column_is_supported(self) -> None:
        manager = SheetManager.__new__(SheetManager)
        manager.input_worksheet = FakeInputWorksheet(
            [["QSN No", "User ID", "Question", "Actual Code"], ["2", "u2", "Q", "code"]]
        )
        rows = SheetManager.get_input_rows(manager)
        self.assertEqual(manager.student_code_source_column, "Actual Code")
        self.assertEqual(rows[0]["input_id"], "QSN_2_USER_u2")

    def test_nan_and_blank_required_values_are_rejected(self) -> None:
        manager = SheetManager.__new__(SheetManager)
        manager.input_worksheet = FakeInputWorksheet(
            [["QSN No", "User ID", "Question", "Student code"], ["nan", "u1", "Q", "code"]]
        )
        with self.assertRaises(ValueError):
            SheetManager.get_input_rows(manager)

    def test_duplicate_input_ids_fail_fast(self) -> None:
        manager = SheetManager.__new__(SheetManager)
        manager.input_worksheet = FakeInputWorksheet(
            [
                ["QSN No", "User ID", "Question", "Student code"],
                ["1", "u1", "Q1", "code 1"],
                ["1", "u1", "Q2", "code 2"],
            ]
        )
        with self.assertRaises(ValueError):
            SheetManager.get_input_rows(manager)

    def test_input_id_format(self) -> None:
        self.assertEqual(build_input_id(" 7 ", " user-1 "), "QSN_7_USER_user-1")

    def test_qwen_headers_have_no_gemini_columns(self) -> None:
        self.assertIn("Corrected_Code", QWEN_HEADERS)
        self.assertFalse(any(header.startswith("Gemini") for header in QWEN_HEADERS))


class SheetStylingTests(unittest.TestCase):
    def test_qwen_style_uses_batch_requests_for_professional_layout(self) -> None:
        class FakeStyleWorksheet:
            id = 123

        class FakeSpreadsheet:
            def __init__(self) -> None:
                self.batch_updates: list[dict[str, object]] = []

            def fetch_sheet_metadata(self) -> dict[str, object]:
                return {"sheets": [{"properties": {"sheetId": 123}, "conditionalFormats": [{}, {}]}]}

            def batch_update(self, body: dict[str, object]) -> None:
                self.batch_updates.append(body)

        worksheet = FakeStyleWorksheet()
        spreadsheet = FakeSpreadsheet()
        manager = object.__new__(SheetManager)
        manager.output_ws = worksheet
        manager.output_spreadsheet = spreadsheet

        manager._style_qwen_tab()

        self.assertEqual(len(spreadsheet.batch_updates), 1)
        requests = spreadsheet.batch_updates[0]["requests"]
        self.assertTrue(any("setBasicFilter" in request for request in requests))
        self.assertTrue(
            any(
                request.get("updateDimensionProperties", {}).get("range", {}).get("dimension") == "ROWS"
                for request in requests
            )
        )
        self.assertTrue(
            any(
                request.get("updateDimensionProperties", {}).get("range", {}).get("startIndex") == 18
                and request.get("updateDimensionProperties", {}).get("properties", {}).get("pixelSize") == 520
                for request in requests
            )
        )
        self.assertTrue(
            any(
                request.get("repeatCell", {})
                .get("cell", {})
                .get("userEnteredFormat", {})
                .get("textFormat", {})
                .get("fontFamily")
                == "Roboto Mono"
                for request in requests
            )
        )
        self.assertTrue(any("deleteConditionalFormatRule" in request for request in requests))


class ResumeAndOutputTests(unittest.TestCase):
    def test_done_rows_are_skipped_and_error_rows_are_retried(self) -> None:
        runner = EvaluationRunner.__new__(EvaluationRunner)
        input_rows = [{"input_id": "done"}, {"input_id": "error"}, {"input_id": "new"}]
        existing = {
            "done": {"final_row_status": "DONE"},
            "error": {"final_row_status": "ERROR"},
        }
        selected = EvaluationRunner._select_rows(runner, input_rows, existing, None, True)
        self.assertEqual([row["input_id"] for row in selected], ["error", "new"])

    def test_incomplete_done_rows_are_retried(self) -> None:
        runner = EvaluationRunner.__new__(EvaluationRunner)
        input_rows = [{"input_id": "complete"}, {"input_id": "incomplete"}]
        existing = {
            "complete": {"final_row_status": "DONE", "missing_required_fields": []},
            "incomplete": {"final_row_status": "DONE", "missing_required_fields": ["Corrected_Code"]},
        }
        selected = EvaluationRunner._select_rows(runner, input_rows, existing, None, True)
        self.assertEqual([row["input_id"] for row in selected], ["incomplete"])

    def test_existing_output_map_marks_complete_done_rows(self) -> None:
        manager = SheetManager.__new__(SheetManager)
        manager.output_ws = FakeOutputWorksheet()
        manager.output_ws.rows = [QWEN_HEADERS, _record_to_row(_done_output_record("id-1"))]

        output_map = SheetManager.get_existing_output_map(manager)

        self.assertTrue(output_map["id-1"]["is_complete_done"])
        self.assertEqual(output_map["id-1"]["missing_required_fields"], [])

    def test_existing_row_is_updated_instead_of_duplicated(self) -> None:
        manager = SheetManager.__new__(SheetManager)
        manager.output_ws = FakeOutputWorksheet()
        row = ["id-1"] + [""] * (len(QWEN_HEADERS) - 1)
        row[QWEN_HEADERS.index("Final_Row_Status")] = "DONE"
        written_row = SheetManager.write_or_update_output_row(manager, row, "id-1")
        self.assertEqual(written_row, 2)
        self.assertEqual(manager.output_ws.updated_row_number, 2)
        self.assertEqual(len(manager.output_ws.rows), 2)

    def test_qwen_output_row_marks_done(self) -> None:
        runner = EvaluationRunner.__new__(EvaluationRunner)
        runner.config = _valid_config()
        row = EvaluationRunner._build_output_row(
            runner,
            input_row={
                "input_id": "id-1",
                "qsn_no": "1",
                "user_id": "u1",
                "question": "Q",
                "student_code": "code",
            },
            qwen_result={"status": "DONE", "parsed": parse_evaluation_json(json.dumps(VALID_RESPONSE))},
            created_at="created",
            evaluated_at="evaluated",
            run_id="run",
        )
        self.assertEqual(row[QWEN_HEADERS.index("Final_Row_Status")], "DONE")


class JavaCodeValidationTests(unittest.TestCase):
    def test_is_java_code_detection(self) -> None:
        from services.evaluation_runner import is_java_code
        self.assertTrue(is_java_code("public class Solution { }"))
        self.assertTrue(is_java_code("int x = 5;"))
        self.assertTrue(is_java_code("System.out.println(\"test\");"))
        self.assertTrue(is_java_code("/* comment */ public void run() {}"))
        self.assertTrue(is_java_code("// some comment\npublic class Test { }"))

        self.assertFalse(is_java_code("def add(x, y):\n    return x + y"))
        self.assertFalse(is_java_code("function add(x, y) { return x + y; }"))
        self.assertFalse(is_java_code("I could not solve this problem, sorry."))
        self.assertFalse(is_java_code("SELECT * FROM students;"))
        self.assertFalse(is_java_code("<html><body>Hello</body></html>"))
        self.assertFalse(is_java_code("#include <stdio.h>\nint main() { printf(\"hello\"); return 0; }"))
        self.assertFalse(is_java_code("int main() { std::cout << \"hello\"; return 0; }"))
        self.assertFalse(is_java_code(""))


    def test_runner_calls_evaluator_for_non_java_code(self) -> None:
        from services.evaluation_runner import EvaluationRunner

        mock_evaluator = Mock()
        parsed_response = parse_evaluation_json(json.dumps(VALID_RESPONSE))
        mock_evaluator.evaluate.return_value = {
            "status": "DONE",
            "parsed": parsed_response,
            "raw_response": json.dumps(VALID_RESPONSE),
        }
        runner = EvaluationRunner.__new__(EvaluationRunner)
        runner.config = _valid_config()
        runner.qwen_evaluator = mock_evaluator

        input_row = {
            "input_id": "id-1",
            "qsn_no": "1",
            "user_id": "u1",
            "question": "Write a Java program",
            "student_code": "def add(x, y): return x + y",
        }

        qwen_result, row_values = runner._evaluate_and_build_validated_row(
            input_row=input_row,
            created_at="created",
            evaluated_at="evaluated",
            run_id="run",
        )

        mock_evaluator.evaluate.assert_called_once()
        self.assertEqual(qwen_result["status"], "DONE")
        self.assertEqual(row_values[QWEN_HEADERS.index("Correctness")], 8.5)
        self.assertEqual(row_values[QWEN_HEADERS.index("Code Quality")], 9.0)
        self.assertEqual(row_values[QWEN_HEADERS.index("Efficiency")], 7.0)
        self.assertEqual(row_values[QWEN_HEADERS.index("Avg Score")], 8.17)
        self.assertEqual(row_values[QWEN_HEADERS.index("Quality Label")], "Good")
        self.assertEqual(row_values[QWEN_HEADERS.index("Final_Row_Status")], "DONE")


class QwenEvaluatorTests(unittest.TestCase):
    def test_ollama_unavailable_raises_clear_error(self) -> None:
        evaluator = QwenEvaluator(_valid_config())
        evaluator.session.get = Mock(side_effect=Exception("down"))
        with self.assertRaises(RuntimeError):
            evaluator.ensure_model_available()

    def test_qwen_model_not_installed_raises_pull_command(self) -> None:
        evaluator = QwenEvaluator(_valid_config())
        response = Mock()
        response.json.return_value = {"models": [{"name": "other"}]}
        response.raise_for_status.return_value = None
        evaluator.session.get = Mock(return_value=response)
        with self.assertRaisesRegex(RuntimeError, f"ollama pull {DEFAULT_QWEN_MODEL_NAME}"):
            evaluator.ensure_model_available()

    def test_qwen_timeout_returns_error_result(self) -> None:
        evaluator = QwenEvaluator(replace(_valid_config(), max_retries=1))
        evaluator.session.post = Mock(side_effect=TimeoutError("timeout"))
        result = evaluator.evaluate("Q", "code")
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("timeout", result["error"])

    def test_qwen_valid_response_returns_done(self) -> None:
        evaluator = QwenEvaluator(replace(_valid_config(), max_retries=1))
        response = Mock()
        response.json.return_value = {"message": {"content": json.dumps(VALID_RESPONSE)}}
        response.raise_for_status.return_value = None
        evaluator.session.post = Mock(return_value=response)
        result = evaluator.evaluate("Q", "code", qsn_no="1", user_id="u1")
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["parsed"].correctness, 8.5)
        payload = evaluator.session.post.call_args.kwargs["json"]
        user_prompt = payload["messages"][1]["content"]
        self.assertIn("QSN No: 1", user_prompt)
        self.assertIn("User ID: u1", user_prompt)


class SafeEvaluateTests(unittest.TestCase):
    def test_safe_evaluate_converts_unexpected_exception(self) -> None:
        class BrokenEvaluator:
            def evaluate(self, question: str, student_code: str, *, qsn_no: str = "", user_id: str = "", warning_msg: str = ""):
                raise RuntimeError("boom")

        result = _safe_evaluate(BrokenEvaluator(), "Q", "code")
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("boom", result["error"])

    def test_done_output_validation_detects_missing_required_sheet_field(self) -> None:
        runner = EvaluationRunner.__new__(EvaluationRunner)
        runner.config = _valid_config()
        row = EvaluationRunner._build_output_row(
            runner,
            input_row={
                "input_id": "id-1",
                "qsn_no": "1",
                "user_id": "u1",
                "question": "Q",
                "student_code": "code",
            },
            qwen_result={"status": "DONE", "parsed": parse_evaluation_json(json.dumps(VALID_RESPONSE))},
            created_at="created",
            evaluated_at="evaluated",
            run_id="run",
        )
        row[QWEN_HEADERS.index("Overall")] = ""
        self.assertIn("missing required output field 'Overall'", _output_validation_errors(row))

    def test_done_output_validation_requires_corrected_code(self) -> None:
        runner = EvaluationRunner.__new__(EvaluationRunner)
        runner.config = _valid_config()
        row = EvaluationRunner._build_output_row(
            runner,
            input_row={
                "input_id": "id-1",
                "qsn_no": "1",
                "user_id": "u1",
                "question": "Q",
                "student_code": "code",
            },
            qwen_result={"status": "DONE", "parsed": parse_evaluation_json(json.dumps(VALID_RESPONSE))},
            created_at="created",
            evaluated_at="evaluated",
            run_id="run",
        )
        row[QWEN_HEADERS.index("Corrected_Code")] = ""
        self.assertIn("missing required output field 'Corrected_Code'", _output_validation_errors(row))



    def test_runner_retries_when_done_output_fields_are_missing(self) -> None:
        parsed = parse_evaluation_json(json.dumps(VALID_RESPONSE))

        class RetryEvaluator:
            def __init__(self) -> None:
                self.calls = 0

            def evaluate(self, question: str, student_code: str, *, qsn_no: str = "", user_id: str = "", warning_msg: str = ""):
                self.calls += 1
                return {
                    "status": "DONE",
                    "parsed": parsed,
                    "raw_response": json.dumps(VALID_RESPONSE),
                    "error": "",
                    "error_type": "",
                    "attempts_used": 1,
                }

        runner = EvaluationRunner.__new__(EvaluationRunner)
        runner.config = replace(_valid_config(), max_retries=2)
        runner.qwen_evaluator = RetryEvaluator()
        input_row = {
            "input_id": "id-1",
            "qsn_no": "1",
            "user_id": "u1",
            "question": "Q",
            "student_code": "public class Solution {}",
        }


        original_build_row = EvaluationRunner._build_output_row

        def build_row_with_first_missing_overall(self, **kwargs):
            row = original_build_row(self, **kwargs)
            if self.qwen_evaluator.calls == 1:
                row[QWEN_HEADERS.index("Overall")] = ""
            return row

        with patch.object(EvaluationRunner, "_build_output_row", build_row_with_first_missing_overall):
            result, row = EvaluationRunner._evaluate_and_build_validated_row(
                runner,
                input_row=input_row,
                created_at="created",
                evaluated_at="evaluated",
                run_id="run",
            )

        self.assertEqual(runner.qwen_evaluator.calls, 2)
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(row[QWEN_HEADERS.index("Overall")], "Mostly correct")


class AuditTests(unittest.TestCase):
    def test_audit_complete_when_every_input_has_one_done_output(self) -> None:
        manager = FakeAuditManager(
            input_rows=[{"input_id": "id-1"}],
            output_records=[_done_output_record("id-1")],
        )
        audit = AuditService(_valid_config(), manager).generate("run-1")
        self.assertEqual(audit["final_status"], "COMPLETE")
        self.assertEqual(manager.last_audit["qwen_done"], 1)

    def test_audit_needs_review_for_missing_output(self) -> None:
        manager = FakeAuditManager(input_rows=[{"input_id": "id-1"}], output_records=[])
        audit = AuditService(_valid_config(), manager).generate("run-1")
        self.assertEqual(audit["final_status"], "NEEDS_REVIEW")
        self.assertEqual(audit["missing_output_rows"], 1)

    def test_audit_needs_review_for_done_row_missing_required_fields(self) -> None:
        incomplete = _done_output_record("id-1")
        incomplete["Overall"] = ""
        manager = FakeAuditManager(
            input_rows=[{"input_id": "id-1"}],
            output_records=[incomplete],
        )
        audit = AuditService(_valid_config(), manager).generate("run-1")
        self.assertEqual(audit["final_status"], "NEEDS_REVIEW")
        self.assertEqual(audit["incomplete_done_rows"], 1)

    def test_audit_cli_does_not_build_qwen_evaluator(self) -> None:
        manager = FakeAuditManager(input_rows=[], output_records=[])

        class FakeSheetManager:
            def __init__(self, config: AppConfig) -> None:
                self.output_sheet_url = manager.output_sheet_url
                self.duplicate_input_ids_count = 0

            def get_input_rows(self, allow_duplicates: bool = False):
                return []

            def get_output_records(self):
                return []

            def write_audit_report(self, audit):
                manager.write_audit_report(audit)

        with patch.object(sys, "argv", ["main.py", "--audit"]):
            with patch("main.AppConfig.from_env", return_value=_valid_config()):
                with patch("sheets.sheet_manager.SheetManager", FakeSheetManager):
                    with patch("evaluators.qwen_evaluator.QwenEvaluator") as qwen_evaluator:
                        with redirect_stdout(io.StringIO()):
                            self.assertEqual(main.main(), 0)
                        qwen_evaluator.assert_not_called()


class FakeInputWorksheet:
    def __init__(self, rows: list[list[str]]) -> None:
        self.rows = rows

    def get_all_values(self) -> list[list[str]]:
        return self.rows


class FakeOutputWorksheet:
    def __init__(self) -> None:
        self.rows = [
            QWEN_HEADERS,
            ["id-1"] + [""] * (len(QWEN_HEADERS) - 1),
        ]
        self.rows[1][QWEN_HEADERS.index("Final_Row_Status")] = "ERROR"
        self.updated_row_number = None

    def get_all_values(self) -> list[list[str]]:
        return self.rows

    def update(self, *, range_name: str, values: list[list[str]], value_input_option: str) -> None:
        self.updated_row_number = int(range_name[1:])
        self.rows[self.updated_row_number - 1] = values[0]

    def append_row(self, row_values: list[str], value_input_option: str) -> None:
        self.rows.append(row_values)


class FakeAuditManager:
    def __init__(self, input_rows: list[dict[str, str]], output_records: list[dict[str, str]]) -> None:
        self.input_rows = input_rows
        self.output_records = output_records
        self.duplicate_input_ids_count = 0
        self.output_sheet_url = "https://docs.google.com/spreadsheets/d/output/edit"
        self.last_audit: dict[str, object] = {}

    def get_input_rows(self, *, allow_duplicates: bool = False) -> list[dict[str, str]]:
        return self.input_rows

    def get_output_records(self) -> list[dict[str, str]]:
        return self.output_records

    def write_audit_report(self, audit_values: dict[str, object]) -> None:
        self.last_audit = audit_values


def _done_output_record(input_id: str) -> dict[str, str]:
    return {
        "input_id": input_id,
        "QSN No": "1",
        "User ID": "u1",
        "Question": "Q",
        "Student code": "code",
        "Qwen_Model": DEFAULT_QWEN_MODEL_NAME,
        "Qwen_Status": "DONE",
        "Code Logic": "Uses loops correctly",
        "Correctness": "8.5",
        "Code Quality": "9",
        "Efficiency": "7",
        "Overall": "Mostly correct",
        "Avg Score": "8.17",
        "Quality Label": "Good",
        "Common_Errors": '["No major errors found"]',
        "Strengths": '["Readable"]',
        "Weaknesses": '["Could handle more edge cases"]',
        "Recommendations": '["Add tests"]',
        "Correctness_Feedback": "Works for normal cases",
        "Improvement_Suggestions": '["Check null input"]',
        "Corrected_Code": "class Main { public static void main(String[] args) { } }",
        "Qwen_Raw_Response": json.dumps(VALID_RESPONSE),
        "Final_Row_Status": "DONE",
        "created_at": "created",
        "evaluated_at": "evaluated",
        "run_id": "run",
        "prompt_version": "test",
    }


def _valid_config() -> AppConfig:
    return AppConfig(
        input_sheet_url="https://docs.google.com/spreadsheets/d/sheet-id/edit",
        input_tab_name="API-Testing Report",
        output_sheet_url="https://docs.google.com/spreadsheets/d/output/edit",
        output_sheet_title="Output",
        google_service_account_file=__file__,
        qwen_model_name=DEFAULT_QWEN_MODEL_NAME,
        ollama_chat_url="http://localhost:11434/api/chat",
        prompt_version="test",
        max_retries=1,
        sleep_between_calls=0,
        model_timeout_seconds=1,
    )


def _record_to_row(record: dict[str, str]) -> list[str]:
    return [record.get(header, "") for header in QWEN_HEADERS]


if __name__ == "__main__":
    unittest.main()

