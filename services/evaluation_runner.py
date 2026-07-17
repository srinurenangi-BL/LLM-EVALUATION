from __future__ import annotations

import re
import time
import uuid
from typing import Any, Protocol


from config import AppConfig
from schemas import EvaluationResponse, calculate_overall_score, evaluation_response_to_dict, quality_label
from services.audit_service import AuditService
from services.error_logger import ErrorLogger
from sheets.sheet_manager import QWEN_HEADERS, SheetManager, done_record_completion_issues, list_to_sheet_json, utc_now_iso


class QwenEvaluatorProtocol(Protocol):
    def evaluate(
        self,
        question: str,
        student_code: str,
        *,
        qsn_no: str = "",
        user_id: str = "",
        warning_msg: str = "",
    ) -> dict[str, Any]:
        ...

    def review(
        self,
        question: str,
        student_code: str,
        existing_eval: dict[str, Any],
        *,
        qsn_no: str = "",
        user_id: str = "",
        warning_msg: str = "",
    ) -> dict[str, Any]:
        ...


class EvaluationRunner:
    """Coordinates Qwen-only row evaluation, output writes, and audit generation."""

    def __init__(
        self,
        config: AppConfig,
        sheet_manager: SheetManager,
        qwen_evaluator: QwenEvaluatorProtocol,
    ) -> None:
        self.config = config
        self.sheet_manager = sheet_manager
        self.qwen_evaluator = qwen_evaluator
        self.error_logger = ErrorLogger(sheet_manager)
        self.audit_service = AuditService(config, sheet_manager)

    def run(self, *, test_limit: int | None = None, skip_done: bool = True) -> dict[str, Any]:
        ensure_model_available = getattr(self.qwen_evaluator, "ensure_model_available", None)
        if callable(ensure_model_available):
            ensure_model_available()

        run_id = self.sheet_manager.get_config_value("run_id")
        if not run_id:
            run_id = str(uuid.uuid4())
            self.sheet_manager.set_config_value("run_id", run_id)

        input_rows = self.sheet_manager.get_input_rows()
        self.sheet_manager.write_config(run_id, total_clean_input_rows=len(input_rows))

        # Check current pass number from Config
        current_pass_str = self.sheet_manager.get_config_value("current_pass")
        if not current_pass_str:
            current_pass = 1
            self.sheet_manager.set_config_value("current_pass", "1")
        else:
            if current_pass_str == "COMPLETED":
                current_pass = 4
            else:
                try:
                    current_pass = int(current_pass_str)
                except Exception:
                    current_pass = 1

        print(f"Run ID: {run_id}")
        print("Evaluation mode: qwen")
        print(f"Active model: {self.config.qwen_model_name}")
        print(f"Output sheet: {self.sheet_manager.output_sheet_url}")
        print(f"Input rows found: {len(input_rows)}")
        print(f"Active pass loaded from Config: Pass {current_pass}")

        processed = 0
        selected = 0

        # --- PASS 1 (First Evaluation) ---
        if current_pass == 1:
            existing_map = self.sheet_manager.get_existing_output_map()
            done_count = sum(1 for entry in existing_map.values() if entry.get("is_complete_done"))

            if len(existing_map) > 0 and done_count < 5:
                print(f"Only {done_count} completed rows found (less than threshold of 5). Clearing output sheet and restarting fresh...")
                self.sheet_manager.set_config_value("current_pass", "1")
                self.sheet_manager.set_config_value("pass2_processed_ids", "[]")
                self.sheet_manager.set_config_value("pass3_processed_ids", "[]")
                self.sheet_manager.clear_output_rows()
                existing_map = {}

            if not existing_map:
                print("Output sheet is empty. Pre-loading all input rows with blank evaluation fields...")
                self.sheet_manager.initialize_output_sheet_with_inputs(input_rows)
                existing_map = self.sheet_manager.get_existing_output_map()
            rows_to_process = self._select_rows(input_rows, existing_map, test_limit, skip_done)
            skipped_complete_rows = _count_complete_existing_outputs(input_rows, existing_map) if skip_done else 0

            print(f"Completed rows skipped in Pass 1: {skipped_complete_rows}")
            print(f"Rows selected for Pass 1 evaluation: {len(rows_to_process)}")

            selected += len(rows_to_process)

            for index, input_row in enumerate(rows_to_process, start=1):
                print(f"[Pass 1 - {index}/{len(rows_to_process)}] Evaluating {input_row['input_id']}...")
                previous = existing_map.get(input_row["input_id"], {})
                created_at = previous.get("created_at") or utc_now_iso()
                qwen_result, row_values = self._evaluate_and_build_validated_row(
                    input_row=input_row,
                    created_at=created_at,
                    evaluated_at=utc_now_iso(),
                    run_id=run_id,
                )
                self.error_logger.log_model_error(
                    run_id=run_id,
                    input_id=input_row["input_id"],
                    model_name=self.config.qwen_model_name,
                    result=qwen_result,
                )

                self.sheet_manager.write_or_update_output_row(row_values, input_row["input_id"])
                processed += 1
                final_status = row_values[QWEN_HEADERS.index("Final_Row_Status")]
                existing_map[input_row["input_id"]] = {
                    "row_number": "",
                    "qwen_status": final_status,
                    "final_row_status": final_status,
                    "created_at": created_at,
                    "is_complete_done": final_status == "DONE" and not _output_validation_errors(row_values),
                }
                print(f"Saved {input_row['input_id']} as {final_status}")
                self._sleep_between_rows()

            # Check if Pass 1 is finished (all rows DONE)
            existing_map = self.sheet_manager.get_existing_output_map()
            all_done = all(existing_map.get(row["input_id"], {}).get("is_complete_done") for row in input_rows)
            if all_done:
                print("Pass 1 completed successfully! Advancing to Pass 2...")
                current_pass = 2
                self.sheet_manager.set_config_value("current_pass", "2")
            else:
                print("Pass 1 has incomplete/errored rows. Please resume/re-run to complete Pass 1.")
                return {
                    "run_id": run_id,
                    "processed": processed,
                    "selected": selected,
                    "status": "PASS_1_INCOMPLETE",
                    "output_sheet_url": self.sheet_manager.output_sheet_url,
                }

        # --- PASS 2 (Second Evaluation / Review) ---
        if current_pass == 2:
            self.run_review_pass(2, input_rows, run_id)
            print("Pass 2 completed successfully! Advancing to Pass 3...")
            current_pass = 3
            self.sheet_manager.set_config_value("current_pass", "3")

        # --- PASS 3 (Third Evaluation / Final review) ---
        if current_pass == 3:
            self.run_review_pass(3, input_rows, run_id)
            print("Pass 3 completed successfully! All passes finished.")
            current_pass = 4
            self.sheet_manager.set_config_value("current_pass", "COMPLETED")

        audit = self.audit_service.generate(run_id)
        print(f"Audit final status: {audit['final_status']}")
        return {
            "run_id": run_id,
            "processed": processed,
            "selected": selected,
            "audit": audit,
            "output_sheet_url": self.sheet_manager.output_sheet_url,
            "status": "ALL_PASSES_COMPLETED",
        }

    def run_review_pass(self, pass_num: int, input_rows: list[dict[str, str]], run_id: str) -> None:
        import json
        processed_key = f"pass{pass_num}_processed_ids"
        processed_ids_str = self.sheet_manager.get_config_value(processed_key)
        if processed_ids_str:
            try:
                processed_ids = json.loads(processed_ids_str)
            except Exception:
                processed_ids = []
        else:
            processed_ids = []

        print(f"\n--- STARTING PASS {pass_num} REVIEW ---")
        print(f"Already reviewed in Pass {pass_num}: {len(processed_ids)}/{len(input_rows)}")

        for index, input_row in enumerate(input_rows, start=1):
            input_id = input_row["input_id"]
            if input_id in processed_ids:
                continue

            print(f"[Pass {pass_num} - {index}/{len(input_rows)}] Auditing {input_id}...")

            existing_map = self.sheet_manager.get_existing_output_map()
            entry = existing_map.get(input_id, {})
            record = entry.get("record", {})

            if not entry or not entry.get("is_complete_done") or not record:
                print(f"Row {input_id} is incomplete/errored in Pass {pass_num}. Re-running evaluation fresh...")
                qwen_result, row_values = self._evaluate_and_build_validated_row(
                    input_row=input_row,
                    created_at=utc_now_iso(),
                    evaluated_at=utc_now_iso(),
                    run_id=run_id,
                )
                self.error_logger.log_model_error(
                    run_id=run_id,
                    input_id=input_id,
                    model_name=self.config.qwen_model_name,
                    result=qwen_result,
                )
                self.sheet_manager.write_or_update_output_row(row_values, input_id)
                self._sleep_between_rows()

                processed_ids.append(input_id)
                self.sheet_manager.set_config_value(processed_key, json.dumps(processed_ids))
                continue

            # Build existing evaluation fields dict
            existing_eval = {
                "code_logic": record.get("Code Logic", ""),
                "completeness_score": _safe_float(record.get("Completeness", 0.0)),
                "code_quality_score": _safe_float(record.get("Code Quality", 0.0)),
                "approach_taken_score": _safe_float(record.get("Approach Taken", 0.0)),
                "overall": record.get("Overall", ""),
                "overall_score": _safe_float(record.get("Overall Score", 0.0)),
                "quality_label": record.get("Quality Label", ""),
                "common_errors": _safe_load_list(record.get("Common_Errors", "[]")),
                "strengths": _safe_load_list(record.get("Strengths", "[]")),
                "weaknesses": _safe_load_list(record.get("Weaknesses", "[]")),
                "recommendations": _safe_load_list(record.get("Recommendations", "[]")),
                "correctness_feedback": record.get("Correctness_Feedback", ""),
                "improvement_suggestions": _safe_load_list(record.get("Improvement_Suggestions", "[]")),
                "corrected_code": record.get("Corrected_Code", ""),
            }

            review_result = self.qwen_evaluator.review(
                question=input_row["question"],
                student_code=input_row["student_code"],
                existing_eval=existing_eval,
                qsn_no=input_row["qsn_no"],
                user_id=input_row["user_id"],
            )

            if review_result.get("status") == "DONE" and review_result.get("parsed"):
                review_parsed = review_result["parsed"]
                if review_parsed.needs_revision:
                    print(f"Revision needed for {input_id}. Reason: {review_parsed.reason}")
                    try:
                        revised_eval = EvaluationResponse(
                            code_logic=review_parsed.code_logic,
                            completeness_score=review_parsed.completeness_score,
                            code_quality_score=review_parsed.code_quality_score,
                            approach_taken_score=review_parsed.approach_taken_score,
                            overall=review_parsed.overall,
                            common_errors=review_parsed.common_errors,
                            strengths=review_parsed.strengths,
                            weaknesses=review_parsed.weaknesses,
                            recommendations=review_parsed.recommendations,
                            correctness_feedback=review_parsed.correctness_feedback,
                            improvement_suggestions=review_parsed.improvement_suggestions,
                            corrected_code=review_parsed.corrected_code,
                        )

                        if revised_eval.corrected_code == "[No corrected code provided by evaluator]" or not revised_eval.corrected_code.strip():
                            if revised_eval.completeness_score >= 9.5:
                                revised_eval.corrected_code = input_row["student_code"]
                            else:
                                print(f"  Empty corrected_code in revised evaluation. Retrying review with warning...")
                                retry_review_result = self.qwen_evaluator.review(
                                    question=input_row["question"],
                                    student_code=input_row["student_code"],
                                    existing_eval=existing_eval,
                                    qsn_no=input_row["qsn_no"],
                                    user_id=input_row["user_id"],
                                    warning_msg="In your previous revision, you left 'corrected_code' empty. You MUST generate a complete, working Java class/method block solving this question. Do not leave it empty."
                                )
                                if retry_review_result.get("status") == "DONE" and retry_review_result.get("parsed"):
                                    retry_parsed = retry_review_result["parsed"]
                                    revised_eval = EvaluationResponse(
                                        code_logic=retry_parsed.code_logic,
                                        completeness_score=retry_parsed.completeness_score,
                                        code_quality_score=retry_parsed.code_quality_score,
                                        approach_taken_score=retry_parsed.approach_taken_score,
                                        overall=retry_parsed.overall,
                                        common_errors=retry_parsed.common_errors,
                                        strengths=retry_parsed.strengths,
                                        weaknesses=retry_parsed.weaknesses,
                                        recommendations=retry_parsed.recommendations,
                                        correctness_feedback=retry_parsed.correctness_feedback,
                                        improvement_suggestions=retry_parsed.improvement_suggestions,
                                        corrected_code=retry_parsed.corrected_code,
                                    )
                                if revised_eval.corrected_code == "[No corrected code provided by evaluator]" or not revised_eval.corrected_code.strip():
                                    raise ValueError("Model failed to provide corrected code for incorrect revised submission.")

                        qwen_result = {
                            "status": "DONE",
                            "parsed": revised_eval,
                            "raw_response": review_result.get("raw_response", ""),
                        }
                    except Exception as err:
                        qwen_result = {
                            "status": "ERROR",
                            "parsed": None,
                            "raw_response": review_result.get("raw_response", ""),
                            "error": f"Revised evaluation validation failed: {err}",
                            "error_type": "REVISED_VALIDATION_ERROR",
                        }

                    row_values = self._build_output_row(
                        input_row=input_row,
                        qwen_result=qwen_result,
                        created_at=record.get("created_at") or utc_now_iso(),
                        evaluated_at=utc_now_iso(),
                        run_id=run_id,
                    )
                    self.error_logger.log_model_error(
                        run_id=run_id,
                        input_id=input_id,
                        model_name=self.config.qwen_model_name,
                        result=qwen_result,
                    )
                    self.sheet_manager.write_or_update_output_row(row_values, input_id)
                    print(f"Revision applied to {input_id}!")
                else:
                    print(f"No revision needed for {input_id}.")
            else:
                self.error_logger.log_model_error(
                    run_id=run_id,
                    input_id=input_id,
                    model_name=self.config.qwen_model_name,
                    result=review_result,
                )
                print(f"Warning: review pass failed for {input_id}: {review_result.get('error')}")

            processed_ids.append(input_id)
            self.sheet_manager.set_config_value(processed_key, json.dumps(processed_ids))
            self._sleep_between_rows()


    def _select_rows(
        self,
        input_rows: list[dict[str, str]],
        existing_map: dict[str, dict[str, Any]],
        test_limit: int | None,
        skip_done: bool,
    ) -> list[dict[str, str]]:
        selected: list[dict[str, str]] = []
        for row in input_rows:
            existing = existing_map.get(row["input_id"], {})
            if skip_done and _existing_output_is_complete(existing):
                continue
            selected.append(row)
            if test_limit is not None and len(selected) >= test_limit:
                break
        return selected

    def _evaluate_and_build_validated_row(
        self,
        *,
        input_row: dict[str, str],
        created_at: str,
        evaluated_at: str,
        run_id: str,
    ) -> tuple[dict[str, Any], list[Any]]:
        last_result: dict[str, Any] = {}
        last_row_values: list[Any] = []
        last_validation_errors: list[str] = []

        for attempt in range(1, self.config.max_retries + 1):
            qwen_result = _safe_evaluate(
                self.qwen_evaluator,
                input_row["question"],
                input_row["student_code"],
                qsn_no=input_row["qsn_no"],
                user_id=input_row["user_id"],
            )
            if qwen_result.get("status") == "DONE" and qwen_result.get("parsed"):
                parsed = qwen_result["parsed"]
                if parsed.corrected_code == "[No corrected code provided by evaluator]" or not parsed.corrected_code.strip():
                    if parsed.completeness_score >= 9.5:
                        parsed.corrected_code = input_row["student_code"]
                    else:
                        if attempt < self.config.max_retries:
                            print(f"  Empty corrected_code detected for incorrect student code. Retrying with warning...")
                            warning_qwen_result = _safe_evaluate(
                                self.qwen_evaluator,
                                input_row["question"],
                                input_row["student_code"],
                                qsn_no=input_row["qsn_no"],
                                user_id=input_row["user_id"],
                                warning_msg="In your previous response, you left 'corrected_code' empty. You MUST generate a complete, working Java class/method block solving this question. Do not leave it empty."
                            )
                            if warning_qwen_result.get("status") == "DONE" and warning_qwen_result.get("parsed"):
                                warning_parsed = warning_qwen_result["parsed"]
                                if warning_parsed.corrected_code and warning_parsed.corrected_code != "[No corrected code provided by evaluator]" and warning_parsed.corrected_code.strip():
                                    qwen_result = warning_qwen_result
                        
                        # Verify final corrected code state
                        parsed = qwen_result.get("parsed")
                        if parsed and (parsed.corrected_code == "[No corrected code provided by evaluator]" or not parsed.corrected_code.strip()):
                            qwen_result["status"] = "ERROR"
                            qwen_result["error"] = "Model failed to provide corrected code for incorrect student submission."


            row_values = self._build_output_row(
                input_row=input_row,
                qwen_result=qwen_result,
                created_at=created_at,
                evaluated_at=evaluated_at,
                run_id=run_id,
            )
            last_result = qwen_result
            last_row_values = row_values

            if qwen_result.get("status") != "DONE":
                return qwen_result, row_values

            last_validation_errors = _output_validation_errors(row_values)
            if not last_validation_errors:
                return qwen_result, row_values

            if attempt < self.config.max_retries and self.config.sleep_between_calls > 0:
                time.sleep(self.config.sleep_between_calls * attempt)

        error_result = {
            "status": "ERROR",
            "parsed": None,
            "raw_response": last_result.get("raw_response", ""),
            "error": "Qwen output validation failed: " + ", ".join(last_validation_errors),
            "error_type": "QWEN_OUTPUT_VALIDATION_ERROR",
            "attempts_used": self.config.max_retries,
        }
        error_row = self._build_output_row(
            input_row=input_row,
            qwen_result=error_result,
            created_at=created_at,
            evaluated_at=evaluated_at,
            run_id=run_id,
        )
        return error_result, error_row

    def _build_output_row(
        self,
        *,
        input_row: dict[str, str],
        qwen_result: dict[str, Any],
        created_at: str,
        evaluated_at: str,
        run_id: str,
    ) -> list[Any]:
        parsed = qwen_result.get("parsed")
        qwen_status = str(qwen_result.get("status", "ERROR") or "ERROR")
        final_row_status = "DONE" if qwen_status == "DONE" else "ERROR"
        fields = _evaluation_fields(parsed) if isinstance(parsed, EvaluationResponse) else _empty_evaluation_fields()

        return [
            input_row["input_id"],
            input_row["qsn_no"],
            input_row["user_id"],
            input_row["question"],
            input_row["student_code"],
            fields["code_logic"],
            fields["completeness_score"],
            fields["code_quality_score"],
            fields["approach_taken_score"],
            fields["overall"],
            fields["overall_score"],
            fields["quality_label"],
            fields["common_errors"],
            fields["strengths"],
            fields["weaknesses"],
            fields["recommendations"],
            fields["correctness_feedback"],
            fields["corrected_code"],
            final_row_status,
        ]


    def _sleep_between_rows(self) -> None:
        if self.config.sleep_between_calls > 0:
            time.sleep(self.config.sleep_between_calls)


def _evaluation_fields(parsed: EvaluationResponse) -> dict[str, Any]:
    parsed_dict = evaluation_response_to_dict(parsed)
    overall_score = calculate_overall_score(parsed.completeness_score, parsed.code_quality_score, parsed.approach_taken_score)
    return {
        "code_logic": parsed_dict["code_logic"],
        "completeness_score": parsed.completeness_score,
        "code_quality_score": parsed.code_quality_score,
        "approach_taken_score": parsed.approach_taken_score,
        "overall": parsed_dict["overall"],
        "overall_score": overall_score,
        "quality_label": quality_label(overall_score),
        "common_errors": list_to_sheet_json(parsed_dict["common_errors"]),
        "strengths": list_to_sheet_json(parsed_dict["strengths"]),
        "weaknesses": list_to_sheet_json(parsed_dict["weaknesses"]),
        "recommendations": list_to_sheet_json(parsed_dict["recommendations"]),
        "correctness_feedback": parsed_dict["correctness_feedback"],
        "corrected_code": parsed_dict["corrected_code"],
    }


def _empty_evaluation_fields() -> dict[str, Any]:
    return {
        "code_logic": "",
        "completeness_score": "",
        "code_quality_score": "",
        "approach_taken_score": "",
        "overall": "",
        "overall_score": "",
        "quality_label": "",
        "common_errors": "",
        "strengths": "",
        "weaknesses": "",
        "recommendations": "",
        "correctness_feedback": "",
        "corrected_code": "",
    }


def _output_validation_errors(row_values: list[Any]) -> list[str]:
    row = {
        header: row_values[index] if index < len(row_values) else ""
        for index, header in enumerate(QWEN_HEADERS)
    }
    return done_record_completion_issues(row)


def _existing_output_is_complete(existing: dict[str, Any]) -> bool:
    if not existing:
        return False

    explicit_status = existing.get("is_complete_done")
    if explicit_status is not None:
        return bool(explicit_status)

    if str(existing.get("final_row_status", "")).strip() != "DONE":
        return False

    qwen_status = str(existing.get("qwen_status", "")).strip()
    if qwen_status and qwen_status != "DONE":
        return False

    completion_issues = existing.get("completion_issues") or []
    missing_required_fields = existing.get("missing_required_fields") or []
    return not completion_issues and not missing_required_fields


def _count_complete_existing_outputs(
    input_rows: list[dict[str, str]],
    existing_map: dict[str, dict[str, Any]],
) -> int:
    return sum(
        1
        for row in input_rows
        if _existing_output_is_complete(existing_map.get(row["input_id"], {}))
    )


def _safe_evaluate(
    evaluator: QwenEvaluatorProtocol,
    question: str,
    student_code: str,
    *,
    qsn_no: str = "",
    user_id: str = "",
    warning_msg: str = "",
) -> dict[str, Any]:
    try:
        result = evaluator.evaluate(question, student_code, qsn_no=qsn_no, user_id=user_id, warning_msg=warning_msg)
    except Exception as exc:
        return {
            "status": "ERROR",
            "parsed": None,
            "raw_response": "",
            "error": f"Unexpected evaluator exception: {exc}",
            "error_type": "UNEXPECTED_EVALUATOR_EXCEPTION",
            "attempts_used": 0,
        }

    if not isinstance(result, dict):
        return {
            "status": "ERROR",
            "parsed": None,
            "raw_response": str(result),
            "error": "Evaluator returned a non-dict result",
            "error_type": "INVALID_EVALUATOR_RESULT",
            "attempts_used": 0,
        }
    return result


def is_java_code(code: str) -> bool:
    if not code:
        return False

    # Strip single-line and multi-line comments
    code_no_comments = re.sub(r"/\*.*?\*/", "", code, flags=re.DOTALL)
    code_no_comments = re.sub(r"//.*", "", code_no_comments)

    code_stripped = code_no_comments.strip()
    if not code_stripped:
        return False

    code_lower = code_stripped.lower()

    # 1. Java code must contain at least a semicolon ';' or a curly brace '{' or '}'
    if ";" not in code_stripped and "{" not in code_stripped and "}" not in code_stripped:
        return False

    # 2. Tokenize to look for whole word keywords
    words = set(re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b", code_lower))

    # Java keywords/types/structures
    java_keywords = {
        "public", "private", "protected", "class", "interface", "void", "static", "import", "package", 
        "new", "return", "int", "double", "float", "boolean", "char", "long", "short", "byte", 
        "if", "for", "while", "do", "switch", "case", "break", "continue", "try", "catch", "finally", 
        "final", "extends", "implements", "throws", "throw", "this", "super", "instanceof"
    }   

    # Check if there is any overlap with Java keywords
    has_java_keyword = any(kw in words for kw in java_keywords)

    # Check for System.out/System.err/System.in
    has_system_io = any(s in code_lower for s in ["system.out", "system.err", "system.in"])

    # Check for standard imports like java.util or java.io
    has_java_import = "java." in code_lower

    if not (has_java_keyword or has_system_io or has_java_import):
        return False

    # 3. Detect other languages specifically to exclude them:
    # Python: 'def' or 'elif' keywords
    if "def" in words or "elif" in words:
        return False

    # Javascript: 'function' or 'console.log'
    if "function" in words or "console.log" in code_lower:
        return False

    # HTML/XML tags
    if "<html>" in code_lower or "<body>" in code_lower or "</html>" in code_lower:
        return False

    # SQL: select ... from ...
    if "select" in words and "from" in words:
        return False

    # 4. Detect C/C++:
    # Contains '#include' or headers like <stdio.h>, <iostream>
    if "#include" in code_stripped:
        return False
    if any(h in code_lower for h in [".h>", "iostream"]):
        return False
    # Contains 'printf' as a whole word, but NOT 'system.out.printf'
    if "printf" in words and "system.out.printf" not in code_lower:
        return False
    # Contains C++ patterns 
    if "std::" in code_lower or "cout" in words or "cin" in words:
        return False

    return True


def _safe_float(val: Any) -> float:
    try:
        return float(val)
    except Exception:
        return 0.0


def _safe_load_list(val: Any) -> list[str]:
    if not val:
        return []
    import json
    val_str = str(val).strip()
    if val_str.startswith("[") and val_str.endswith("]"):
        try:
            return json.loads(val_str)
        except Exception:
            pass
    return [val_str] if val_str else []

