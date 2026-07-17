from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests

from config import AppConfig
from evaluators.base_evaluator import EvaluationResult, done_result, error_result
from prompts import JAVA_EVALUATOR_SYSTEM_PROMPT, build_user_prompt, JAVA_REVIEW_SYSTEM_PROMPT, build_review_user_prompt
from services.json_parser import parse_evaluation_json, parse_review_json


class QwenEvaluator:
    """Local Ollama/Qwen integration for Java submission evaluation."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.session = requests.Session()

    def ensure_model_available(self) -> None:
        tags_url = _ollama_tags_url(self.config.ollama_chat_url)
        try:
            response = self.session.get(tags_url, timeout=10)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise RuntimeError(
                "Ollama API is not reachable at http://localhost:11434. "
                "Start Ollama and try again."
            ) from exc

        installed_models = {
            str(model.get("name") or model.get("model") or "").strip()
            for model in data.get("models", [])
        }
        if self.config.qwen_model_name not in installed_models:
            raise RuntimeError(
                f"Configured Qwen model {self.config.qwen_model_name!r} is not installed locally. "
                f"Run: ollama pull {self.config.qwen_model_name}"
            )

    def evaluate(
        self,
        question: str,
        student_code: str,
        *,
        qsn_no: str = "",
        user_id: str = "",
        warning_msg: str = "",
    ) -> EvaluationResult:
        raw_text = ""
        last_error = ""

        for attempt in range(1, self.config.max_retries + 1):
            try:
                user_content = build_user_prompt(
                    question,
                    student_code,
                    qsn_no=qsn_no,
                    user_id=user_id,
                )
                if warning_msg:
                    user_content += f"\n\nCRITICAL WARNING: {warning_msg}"

                payload = {
                    "model": self.config.qwen_model_name,
                    "messages": [
                        {"role": "system", "content": JAVA_EVALUATOR_SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": user_content,
                        },
                    ],
                    "stream": False,
                    "format": "json",
                    "options": {
                        "temperature": 0,
                    },
                }
                response = self.session.post(
                    self.config.ollama_chat_url,
                    json=payload,
                    timeout=self.config.model_timeout_seconds,
                )
                response.raise_for_status()
                response_json: dict[str, Any] = response.json()
                content = response_json.get("message", {}).get("content")
                raw_text = str(content) if content is not None else json.dumps(response_json)
                parsed = parse_evaluation_json(raw_text)
                return done_result(parsed, raw_text, attempt)
            except Exception as exc:
                last_error = str(exc)
                if attempt < self.config.max_retries:
                    time.sleep(self.config.sleep_between_calls * attempt)

        return error_result(
            error_message=last_error or "Qwen evaluation failed",
            error_type="QWEN_EVALUATION_ERROR",
            raw_response=raw_text,
            attempts_used=self.config.max_retries,
        )

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
        raw_text = ""
        last_error = ""

        for attempt in range(1, self.config.max_retries + 1):
            try:
                user_content = build_review_user_prompt(
                    question,
                    student_code,
                    existing_eval,
                )
                if warning_msg:
                    user_content += f"\n\nCRITICAL WARNING: {warning_msg}"

                payload = {
                    "model": self.config.qwen_model_name,
                    "messages": [
                        {"role": "system", "content": JAVA_REVIEW_SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": user_content,
                        },
                    ],
                    "stream": False,
                    "format": "json",
                    "options": {
                        "temperature": 0,
                    },
                }
                response = self.session.post(
                    self.config.ollama_chat_url,
                    json=payload,
                    timeout=self.config.model_timeout_seconds,
                )
                response.raise_for_status()
                response_json: dict[str, Any] = response.json()
                content = response_json.get("message", {}).get("content")
                raw_text = str(content) if content is not None else json.dumps(response_json)
                parsed = parse_review_json(raw_text)
                return {
                    "status": "DONE",
                    "parsed": parsed,
                    "raw_response": raw_text,
                    "attempts_used": attempt,
                }
            except Exception as exc:
                last_error = str(exc)
                if attempt < self.config.max_retries:
                    time.sleep(self.config.sleep_between_calls * attempt)

        return {
            "status": "ERROR",
            "parsed": None,
            "raw_response": raw_text,
            "error": last_error or "Qwen review failed",
            "error_type": "QWEN_REVIEW_ERROR",
            "attempts_used": self.config.max_retries,
        }


def _ollama_tags_url(chat_url: str) -> str:
    parsed = urlparse(chat_url)
    return urlunparse((parsed.scheme, parsed.netloc, "/api/tags", "", "", ""))
