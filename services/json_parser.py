from __future__ import annotations

import json
import re
from typing import Any, Iterable

from schemas import EvaluationResponse, validate_evaluation_response


KEY_ALIASES = {
    # Score field aliases — old names and common LLM variants → new names
    "correctness": "completeness_score",
    "correctness_score": "completeness_score",
    "completeness": "completeness_score",
    "code_quality": "code_quality_score",
    "quality_score": "code_quality_score",
    "efficiency": "approach_taken_score",
    "efficiency_score": "approach_taken_score",
    "approach_taken": "approach_taken_score",
    "approach": "approach_taken_score",
    # List field aliases
    "commonerrors": "common_errors",
    "common_error": "common_errors",
    "errors": "common_errors",
    "done_well": "strengths",
    "donewell": "strengths",
    # improvement_suggestions no longer a separate field — map to correctness_feedback
    "suggestions": "correctness_feedback",
    "improvements": "correctness_feedback",
    "improvement": "correctness_feedback",
    "improvement_suggestions": "correctness_feedback",
    # corrected_code aliases
    "fixed_code": "corrected_code",
    "correct_code": "corrected_code",
    "corrected_java_code": "corrected_code",
    # code_logic aliases
    "logic": "code_logic",
    "code_logic_feedback": "code_logic",
}


def parse_evaluation_json(raw_text: str) -> EvaluationResponse:
    if raw_text is None or not str(raw_text).strip():
        raise ValueError("Model returned an empty response")

    errors: list[str] = []
    for candidate in _candidate_json_strings(str(raw_text)):
        try:
            loaded = json.loads(candidate)
            normalized = _normalize_keys(loaded)
            return validate_evaluation_response(normalized)
        except Exception as exc:
            errors.append(str(exc))

    detail = errors[-1] if errors else "No JSON object found"
    raise ValueError(f"Could not parse model response as evaluation JSON: {detail}")


parse_qwen_evaluation_json = parse_evaluation_json


def _candidate_json_strings(text: str) -> list[str]:
    candidates: list[str] = []
    stripped = text.strip()
    candidates.append(stripped)
    candidates.extend(fenced.strip() for fenced in _extract_fenced_blocks(stripped))
    candidates.extend(balanced.strip() for balanced in _extract_balanced_json_objects(stripped))
    return _deduplicate(candidate for candidate in candidates if candidate)


def _extract_fenced_blocks(text: str) -> Iterable[str]:
    pattern = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
    for match in pattern.finditer(text):
        yield match.group(1)


def _extract_balanced_json_objects(text: str) -> Iterable[str]:
    start_index: int | None = None
    depth = 0
    in_string = False
    escape_next = False

    for index, char in enumerate(text):
        if escape_next:
            escape_next = False
            continue
        if char == "\\" and in_string:
            escape_next = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            if depth == 0:
                start_index = index
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start_index is not None:
                yield text[start_index : index + 1]
                start_index = None


def _normalize_keys(value: Any) -> Any:
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            normalized[_canonical_key(str(key))] = _normalize_keys(item)
        return normalized
    if isinstance(value, list):
        return [_normalize_keys(item) for item in value]
    return value


def _canonical_key(key: str) -> str:
    snake_key = _to_snake_case(key)
    compact_key = snake_key.replace("_", "")
    return KEY_ALIASES.get(snake_key, KEY_ALIASES.get(compact_key, snake_key))


def _to_snake_case(value: str) -> str:
    underscored = re.sub(r"[\s\-]+", "_", value.strip())
    camel_split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", underscored)
    return re.sub(r"_+", "_", camel_split).strip("_").lower()


def _deduplicate(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value not in seen:
            unique.append(value)
            seen.add(value)
    return unique


def parse_review_json(raw_text: str) -> ReviewResponse:
    from schemas import ReviewResponse
    if raw_text is None or not str(raw_text).strip():
        raise ValueError("Model returned an empty response")

    errors: list[str] = []
    for candidate in _candidate_json_strings(str(raw_text)):
        try:
            loaded = json.loads(candidate)
            normalized = _normalize_keys(loaded)
            return ReviewResponse.model_validate(normalized)
        except Exception as exc:
            errors.append(str(exc))

    detail = errors[-1] if errors else "No JSON object found"
    raise ValueError(f"Could not parse model response as review JSON: {detail}")

