# dual_llm_code_evaluator

Python pipeline for evaluating student Java submissions from Google Sheets with local Qwen through Ollama.

Gemini is intentionally disabled. The old Gemini evaluator code is kept only as commented reference code in `evaluators/gemini_evaluator.py`.

The app reads four required input values from the input sheet, sends them to Qwen, writes results to `Qwen_Evaluation`, validates the final output fields, retries incomplete `DONE` evaluations, logs model failures to `Error_Log`, and writes consistency checks to `Audit_Report`. `Corrected_Code` is required for every completed evaluation row.

Only `.env` is loaded by the application. `.env.example` is a safe template for rebuilding `.env` if needed; it is not a second active configuration file.

## Flow

1. Read input rows from the external Google Sheet.
2. Require `QSN No`, `User ID`, `Question`, and one supported student-code column.
3. Send those four values to local Qwen through Ollama.
4. Parse and validate Qwen's JSON response.
5. Calculate deterministic fields in Python: `Avg Score` and `Quality Label`.
6. Require non-empty `Corrected_Code`; Qwen must return the final accepted Java code.
7. Write one row to the separate `Qwen_Evaluation` output tab.
8. If a `DONE` row is missing required output fields, retry evaluation before saving it as complete.
9. Generate `Audit_Report`; incomplete, missing, duplicate, or errored rows become `NEEDS_REVIEW`.

## Folder Structure

- `main.py`: CLI parsing and Qwen-only command dispatch.
- `config.py`: `.env` loading and Qwen validation.
- `prompts.py`: shared Java evaluation prompt.
- `schemas.py`: response schema, score normalization, average score, and quality labels.
- `evaluators/qwen_evaluator.py`: local Ollama/Qwen integration.
- `evaluators/gemini_evaluator.py`: commented Gemini reference only.
- `services/evaluation_runner.py`: Qwen row workflow, resume logic, and output assembly.
- `services/audit_service.py`: Qwen output completeness audit.
- `services/json_parser.py`: robust JSON extraction and key normalization.
- `sheets/sheet_manager.py`: Google Sheets reading, writing, tab setup, styling, and cell safety.
- `tests/`: offline unit tests with mocked APIs and fake worksheets.

## Setup

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Use the environment file:

If `.env` already exists, edit that file directly. Only copy the template on a fresh setup where `.env` is missing.

```powershell
Copy-Item .env.example .env
```

3. Fill `.env` without committing it:

```env
INPUT_SHEET_URL=<input sheet URL>
INPUT_TAB_NAME=API-Testing Report
OUTPUT_SHEET_URL=<existing output sheet URL or blank to create one>
OUTPUT_SHEET_TITLE=VSCode_Qwen25Coder7B_Student_Evaluation
GOOGLE_SERVICE_ACCOUNT_FILE=credentials/service_account.json
QWEN_MODEL_NAME=qwen2.5-coder:7b-instruct
OLLAMA_CHAT_URL=http://localhost:11434/api/chat
PROMPT_VERSION=qwen25coder7b_instruct_student_eval_v2.0
MAX_RETRIES=3
SLEEP_BETWEEN_CALLS=2
MODEL_TIMEOUT_SECONDS=240
```

4. Put the Google service account JSON here:

```text
credentials/service_account.json
```

5. Share both the input and output Google Sheets with the service-account email.

6. Install Ollama and pull the local model:

```bash
ollama pull qwen2.5-coder:7b-instruct
```

## Input Sheet

Required columns after trimming header spaces:

- `QSN No`
- `User ID`
- `Question`
- one of `Student code`, `Student Code`, or `Actual Code`

The input sheet is read-only. The app does not update, clear, or style it.

## Output Tabs

- `Qwen_Evaluation`: Qwen scores, feedback, raw response, error message, and final row status.
- `Audit_Report`: completeness and duplicate checks.
- `Error_Log`: evaluator errors with raw response previews.
- `Config`: non-secret run metadata.

Existing tabs are preserved. If `Qwen_Evaluation` already contains data with a different header, the app stops instead of overwriting historical results.

## Commands

Check Sheets access and local Qwen setup:

```bash
python -B main.py --check
python -B main.py --model qwen --check
```

Evaluate two eligible rows:

```bash
python -B main.py --test 2
python -B main.py --model qwen --test 2
```

Run every eligible row:

```bash
python -B main.py --run-all
python -B main.py --model qwen --run-all
```

Generate the audit report without calling Qwen:

```bash
python -B main.py --audit
```

## Resume Rules

- `Final_Row_Status = DONE` with all required fields filled: skipped.
- `Final_Row_Status = DONE` but missing required fields such as `Corrected_Code`: retried.
- `Final_Row_Status = ERROR`: retried.
- Incomplete `DONE` output fields: re-evaluated before the row is saved as complete.
- Existing input IDs are updated in place.
- New input IDs are appended.
- Rows are never reset automatically.

## Testing

```bash
python -m pytest -q
python -B -m unittest discover -s tests -v
python -m compileall .
```

## Pending Integration

The code is complete for the Qwen-only flow. The remaining work is external setup: valid Google Sheets access through `credentials/service_account.json`, correct `.env` values, Ollama running locally, and the Qwen model pulled in Ollama.

## Data Safety

- Do not commit `.env`.
- Do not commit `credentials/service_account.json`.
- Do not print service-account private keys.
- Do not use hosted Qwen APIs; Qwen mode is local Ollama only.
