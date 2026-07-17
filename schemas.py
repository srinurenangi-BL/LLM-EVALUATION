from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


# Quality label thresholds aligned with master prompt
QUALITY_LABELS = [
    (9.0, "Excellent"),
    (7.5, "Good"),
    (6.0, "Average"),
    (4.0, "Poor"),
    (0.0, "Critical"),
]


class EvaluationResponse(BaseModel):
    model_config = ConfigDict(validate_default=True)

    # Renamed from: correctness → completeness_score
    # Renamed from: code_quality → code_quality_score
    # Renamed from: efficiency → approach_taken_score
    # overall_score is now computed via weighted formula (not a simple average)
    code_logic: str
    completeness_score: float = Field(..., ge=0.0, le=10.0)
    code_quality_score: float = Field(..., ge=0.0, le=10.0)
    approach_taken_score: float = Field(..., ge=0.0, le=10.0)
    overall: str
    common_errors: list[str] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    # correctness_feedback: exactly 2 sentences per master prompt
    # Sentence 1: correctness assessment. Sentence 2: improvement or confirmation.
    correctness_feedback: str
    corrected_code: str

    @field_validator("completeness_score", "code_quality_score", "approach_taken_score", mode="before")
    @classmethod
    def _normalize_score(cls, value: Any) -> float:
        return normalize_score(value)

    @field_validator("code_logic", "overall", "correctness_feedback", mode="before")
    @classmethod
    def _required_text(cls, value: Any) -> str:
        text = "" if value is None else str(value).strip()
        if not text:
            raise ValueError("required text fields must not be empty")
        return text

    @field_validator("corrected_code", mode="before")
    @classmethod
    def _corrected_code(cls, value: Any) -> str:
        text = "" if value is None else str(value).strip()
        if not text:
            return "[No corrected code provided by evaluator]"
        return text

    @field_validator("common_errors", mode="before")
    @classmethod
    def _common_errors(cls, value: Any) -> list[str]:
        return normalize_list(value) or ["No major errors found"]

    @field_validator("strengths", mode="before")
    @classmethod
    def _strengths(cls, value: Any) -> list[str]:
        return normalize_list(value) or ["No specific strengths identified"]

    @field_validator("weaknesses", mode="before")
    @classmethod
    def _weaknesses(cls, value: Any) -> list[str]:
        return normalize_list(value) or ["No major weaknesses found"]

    @field_validator("recommendations", mode="before")
    @classmethod
    def _recommendations(cls, value: Any) -> list[str]:
        return normalize_list(value) or ["Review edge cases and keep code readable"]


def validate_evaluation_response(data: dict[str, Any]) -> EvaluationResponse:
    return EvaluationResponse.model_validate(data)


def evaluation_response_to_dict(model: EvaluationResponse) -> dict[str, Any]:
    return model.model_dump()


def normalize_score(value: Any) -> float:
    if value is None:
        raise ValueError("score is required")

    if isinstance(value, (int, float)):
        return clamp_score(float(value))

    text = str(value).strip()
    if not text:
        raise ValueError("score is empty")

    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        raise ValueError(f"could not parse score from {text!r}")

    return clamp_score(float(match.group(0)))


def clamp_score(value: float) -> float:
    return max(0.0, min(10.0, round(float(value), 2)))


def calculate_overall_score(completeness: float, code_quality: float, approach_taken: float) -> float:
    """Weighted formula from master prompt:
    overall_score = (0.5 * completeness_score) + (0.3 * code_quality_score) + (0.2 * approach_taken_score)
    """
    return round(
        (0.5 * clamp_score(completeness))
        + (0.3 * clamp_score(code_quality))
        + (0.2 * clamp_score(approach_taken)),
        2,
    )


def quality_label(overall_score: float) -> str:
    """Quality label thresholds aligned with master prompt."""
    score = clamp_score(overall_score)
    for lower_bound, label in QUALITY_LABELS:
        if score >= lower_bound:
            return label
    return "Critical"


def normalize_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    return [text]


class ReviewResponse(BaseModel):
    needs_revision: bool
    reason: str = ""
    code_logic: str = ""
    completeness_score: float = 0.0
    code_quality_score: float = 0.0
    approach_taken_score: float = 0.0
    overall: str = ""
    overall_score: float = 0.0
    quality_label: str = ""
    common_errors: list[str] = []
    strengths: list[str] = []
    weaknesses: list[str] = []
    recommendations: list[str] = []
    correctness_feedback: str = ""
    corrected_code: str = ""
