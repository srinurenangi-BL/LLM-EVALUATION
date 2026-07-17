from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv


ACTIVE_MODEL_NAME = "qwen"
DEFAULT_QWEN_MODEL_NAME = "qwen2.5-coder:7b-instruct"
DEFAULT_OUTPUT_SHEET_TITLE = "VSCode_Qwen25Coder7B_Student_Evaluation"
DEFAULT_PROMPT_VERSION = "qwen25coder7b_instruct_student_eval_v2.0"
EXPECTED_OLLAMA_PATH = "/api/chat"
LOCAL_OLLAMA_HOSTS = {"localhost", "127.0.0.1", "::1"}
PLACEHOLDER_MARKERS = {"your_", "your-", "replace_me", "replace-me"}


@dataclass(frozen=True)
class AppConfig:
    input_sheet_url: str
    input_tab_name: str
    output_sheet_url: str
    output_sheet_title: str
    google_service_account_file: str
    qwen_model_name: str
    ollama_chat_url: str
    prompt_version: str
    max_retries: int
    sleep_between_calls: float
    model_timeout_seconds: int

    @classmethod
    def from_env(cls) -> "AppConfig":
        load_dotenv()
        return cls(
            input_sheet_url=os.getenv("INPUT_SHEET_URL", "").strip(),
            input_tab_name=os.getenv("INPUT_TAB_NAME", "API-Testing Report").strip(),
            output_sheet_url=os.getenv("OUTPUT_SHEET_URL", "").strip(),
            output_sheet_title=os.getenv("OUTPUT_SHEET_TITLE", DEFAULT_OUTPUT_SHEET_TITLE).strip(),
            google_service_account_file=os.getenv(
                "GOOGLE_SERVICE_ACCOUNT_FILE",
                "credentials/service_account.json",
            ).strip(),
            qwen_model_name=os.getenv("QWEN_MODEL_NAME", DEFAULT_QWEN_MODEL_NAME).strip(),
            ollama_chat_url=os.getenv("OLLAMA_CHAT_URL", "http://localhost:11434/api/chat").strip(),
            prompt_version=os.getenv("PROMPT_VERSION", DEFAULT_PROMPT_VERSION).strip(),
            max_retries=_parse_int("MAX_RETRIES", default=3),
            sleep_between_calls=_parse_float("SLEEP_BETWEEN_CALLS", default=2.0),
            model_timeout_seconds=_parse_int("MODEL_TIMEOUT_SECONDS", default=240),
        )

    def validate_for_sheets(self) -> None:
        missing = []
        if not self.input_sheet_url or _looks_like_placeholder(self.input_sheet_url):
            missing.append("INPUT_SHEET_URL")
        if not self.input_tab_name:
            missing.append("INPUT_TAB_NAME")
        if not self.google_service_account_file:
            missing.append("GOOGLE_SERVICE_ACCOUNT_FILE")
        if missing:
            raise ValueError(f"Missing required .env values: {', '.join(missing)}")

        service_account_path = Path(self.google_service_account_file)
        if not service_account_path.exists():
            raise FileNotFoundError(
                "Google service account file not found at "
                f"{service_account_path}. Download it and update GOOGLE_SERVICE_ACCOUNT_FILE."
            )

    def validate_for_run(self) -> None:
        self.validate_for_sheets()
        self.validate_qwen()

    def validate_qwen(self) -> None:
        missing = []
        if not self.qwen_model_name:
            missing.append("QWEN_MODEL_NAME")
        if not self.ollama_chat_url:
            missing.append("OLLAMA_CHAT_URL")
        if missing:
            raise ValueError(f"Missing required Qwen .env values: {', '.join(missing)}")
        if self.qwen_model_name.strip().lower() == "qwen-plus":
            raise ValueError("QWEN_MODEL_NAME must be a local Ollama model, not qwen-plus.")

        parsed_url = urlparse(self.ollama_chat_url)
        if parsed_url.scheme != "http" or parsed_url.hostname not in LOCAL_OLLAMA_HOSTS:
            raise ValueError(
                "OLLAMA_CHAT_URL must point to local Ollama, for example "
                "http://localhost:11434/api/chat."
            )
        if parsed_url.path.rstrip("/") != EXPECTED_OLLAMA_PATH:
            raise ValueError(
                "OLLAMA_CHAT_URL must end with /api/chat, for example "
                "http://localhost:11434/api/chat."
            )


def _parse_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw_value!r}") from exc
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


def _parse_float(name: str, default: float) -> float:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw_value!r}") from exc
    if value < 0:
        raise ValueError(f"{name} must be 0 or greater")
    return value


def _looks_like_placeholder(value: str) -> bool:
    normalized = value.strip().lower()
    return any(marker in normalized for marker in PLACEHOLDER_MARKERS)


# Gemini reference only. Gemini is intentionally inactive in this Qwen-only build.
# DEFAULT_GEMINI_MODEL_NAME = "gemini-2.5-flash"
# GEMINI_API_KEY and GEMINI_MODEL_NAME were previously loaded here for Gemini mode.
# That mode is disabled so Qwen is the only evaluator used by the application.
