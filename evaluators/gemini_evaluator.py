# Gemini evaluator reference only.
#
# Gemini is intentionally disabled in this Qwen-only build.
# The active application must not import, instantiate, or call GeminiEvaluator.
#
# Previous implementation kept for future reference:
#
# from __future__ import annotations
#
# import time
# from typing import Any
#
# from config import AppConfig
# from evaluators.base_evaluator import EvaluationResult, done_result, error_result
# from prompts import JAVA_EVALUATOR_SYSTEM_PROMPT, build_user_prompt
# from services.json_parser import parse_evaluation_json
#
#
# class GeminiEvaluator:
#     """Gemini API integration for Java submission evaluation."""
#
#     def __init__(self, config: AppConfig) -> None:
#         self.config = config
#         genai, types = _load_genai_modules()
#         self._types = types
#         self.client = genai.Client(api_key=config.gemini_api_key)
#
#     def evaluate(self, question: str, student_code: str) -> EvaluationResult:
#         raw_text = ""
#         last_error = ""
#
#         for attempt in range(1, self.config.max_retries + 1):
#             try:
#                 response = self.client.models.generate_content(
#                     model=self.config.gemini_model_name,
#                     contents=build_user_prompt(question, student_code),
#                     config=self._types.GenerateContentConfig(
#                         system_instruction=JAVA_EVALUATOR_SYSTEM_PROMPT,
#                         response_mime_type="application/json",
#                         temperature=0,
#                     ),
#                 )
#                 raw_text = _extract_text(response)
#                 parsed = parse_evaluation_json(raw_text)
#                 return done_result(parsed, raw_text, attempt)
#             except Exception as exc:
#                 last_error = str(exc)
#                 if attempt < self.config.max_retries:
#                     time.sleep(self.config.sleep_between_calls * attempt)
#
#         return error_result(
#             error_message=last_error or "Gemini evaluation failed",
#             error_type="GEMINI_EVALUATION_ERROR",
#             raw_response=raw_text,
#             attempts_used=self.config.max_retries,
#         )
#
#
# def _load_genai_modules() -> tuple[Any, Any]:
#     try:
#         from google import genai
#         from google.genai import types
#     except ImportError as exc:
#         raise RuntimeError(
#             "Gemini mode requires the google-genai package. "
#             "Install dependencies with: pip install -r requirements.txt"
#         ) from exc
#     return genai, types
#
#
# def _extract_text(response: Any) -> str:
#     text = getattr(response, "text", None)
#     if text:
#         return str(text)
#
#     candidates = getattr(response, "candidates", None) or []
#     parts: list[str] = []
#     for candidate in candidates:
#         content = getattr(candidate, "content", None)
#         for part in getattr(content, "parts", []) or []:
#             part_text = getattr(part, "text", None)
#             if part_text:
#                 parts.append(str(part_text))
#     return "\n".join(parts)
