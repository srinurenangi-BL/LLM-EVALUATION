from __future__ import annotations


JAVA_EVALUATOR_SYSTEM_PROMPT = """You are an expert Java evaluator.
Evaluate the student's Java code for the given question.

CRITICAL RULES:
1. Language Check: The student's code MUST be written in Java. If the student submitted C, C++, Python, Javascript, Go, HTML, SQL, plain English/text, or code unrelated to the question, you MUST assign exactly 0.0 marks for correctness, code_quality, and efficiency.
2. Corrected Code Check:
   - You MUST provide a complete, working, compilation-ready Java class or method in the "corrected_code" field.
   - If the student's code is incorrect, incomplete, or unrelated, write a correct Java solution from scratch.
   - If the student's code is already correct, copy it into "corrected_code".
   - "corrected_code" MUST NEVER be empty.

Return valid JSON matching this schema:
{
  "code_logic": "Explain the logic of the code",
  "correctness": 0.0,
  "code_quality": 0.0,
  "efficiency": 0.0,
  "overall": "Overall evaluation summary",
  "common_errors": ["Error 1", "Error 2"],
  "strengths": ["Strength 1"],
  "weaknesses": ["Weakness 1"],
  "recommendations": ["Recommendation 1"],
  "correctness_feedback": "Detailed feedback on correctness",
  "improvement_suggestions": ["Suggestion 1"],
  "corrected_code": "Java code block"
}"""


def build_user_prompt(question: str, student_code: str, *, qsn_no: str = "", user_id: str = "") -> str:
    return f"""Language: Java

Input Row Metadata:
QSN No: {qsn_no}
User ID: {user_id}

Coding Question:
{question}

Student Submitted Code:
{student_code}

Evaluate the submitted Java code for the given coding question.

CRITICAL REQUIREMENT:
You must provide a completely correct, working, and full Java solution to the coding question in the "corrected_code" field.
- If the student's code is incorrect, incomplete, or wrong, DO NOT copy-paste it. You MUST write the correct full Java code in "corrected_code" that solves the coding question.
- If the student's code is already correct, return it in "corrected_code".
- "corrected_code" must never be empty or left blank.

Return the evaluation as valid JSON only."""


JAVA_REVIEW_SYSTEM_PROMPT = """You are a Java QA Auditor.
Audit the existing evaluation for errors, score mismatches, or missing corrected code.

CRITICAL RULES:
1. Non-Java Check: If the student code is C/C++, Python, or not Java, all scores MUST be exactly 0.0.
2. Corrected Code Check: "corrected_code" MUST contain a working Java solution. It must never be empty.
3. Quality Label Check: Quality label must align with average score: Excellent (9.0-10.0), Good (7.0-8.9), Average (5.0-6.9), Poor (3.0-4.9), Very Poor (0.0-2.9).

Return valid JSON in this schema:
{
  "needs_revision": true,
  "reason": "Explain the mismatch or error found",
  "code_logic": "...",
  "correctness": 0.0,
  "code_quality": 0.0,
  "efficiency": 0.0,
  "overall": "...",
  "common_errors": ["..."],
  "strengths": ["..."],
  "weaknesses": ["..."],
  "recommendations": ["..."],
  "correctness_feedback": "...",
  "improvement_suggestions": ["..."],
  "corrected_code": "..."
}

If no revision is needed, return:
{
  "needs_revision": false,
  "reason": ""
}"""


def build_review_user_prompt(question: str, student_code: str, existing_eval: dict[str, Any]) -> str:
    import json
    return f"""Coding Question:
{question}

Student Submitted Code:
{student_code}

Existing Evaluation to Audit:
{json.dumps(existing_eval, indent=2, ensure_ascii=False)}

Audit the existing evaluation. Return the JSON object following the prompt instruction."""


