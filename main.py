from __future__ import annotations

import argparse
import sys
import uuid

from config import ACTIVE_MODEL_NAME, AppConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate student Java code with local Qwen through Ollama.",
    )
    parser.add_argument(
        "--model",
        choices=[ACTIVE_MODEL_NAME],
        default=ACTIVE_MODEL_NAME,
        help="Only qwen is supported in this build. Default: qwen.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--test", type=int, metavar="N", help="Evaluate only the first N non-DONE rows.")
    mode.add_argument("--run-all", action="store_true", help="Evaluate all non-DONE rows.")
    mode.add_argument("--audit", action="store_true", help="Generate Audit_Report without evaluating rows.")
    mode.add_argument("--check", action="store_true", help="Check configuration, Sheets access, and local Qwen setup.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.test is None and not args.run_all and not args.audit and not args.check:
        parser.print_help()
        return 0

    if args.test is not None and args.test < 1:
        parser.error("--test N must use a value of 1 or greater")

    try:
        from services.time_patcher import apply_timezone_patch
        apply_timezone_patch()

        from evaluators.qwen_evaluator import QwenEvaluator
        from services.audit_service import AuditService
        from services.evaluation_runner import EvaluationRunner
        from sheets.sheet_manager import SheetManager

        config = AppConfig.from_env()

        if args.audit:
            config.validate_for_sheets()
            sheet_manager = SheetManager(config)
            audit = AuditService(config, sheet_manager).generate(run_id=f"audit-{uuid.uuid4()}")
            print(f"Audit final status: {audit['final_status']}")
            print(f"Output sheet: {sheet_manager.output_sheet_url}")
            return 0

        if args.check:
            config.validate_for_run()
            sheet_manager = SheetManager(config)
            input_rows = sheet_manager.get_input_rows(allow_duplicates=True)
            sheet_manager.write_config(run_id=f"check-{uuid.uuid4()}", total_clean_input_rows=len(input_rows))
            QwenEvaluator(config).ensure_model_available()
            print("Configuration OK")
            print(f"Active model: {config.qwen_model_name}")
            print(f"Input rows readable: {len(input_rows)}")
            print(f"Student code column: {sheet_manager.student_code_source_column}")
            print(f"Output sheet writable: {sheet_manager.output_sheet_url}")
            return 0

        config.validate_for_run()
        sheet_manager = SheetManager(config)
        runner = EvaluationRunner(
            config=config,
            sheet_manager=sheet_manager,
            qwen_evaluator=QwenEvaluator(config),
        )
        result = runner.run(test_limit=args.test if args.test is not None else None)
        print(f"Processed rows: {result['processed']}")
        print(f"Output sheet: {result['output_sheet_url']}")
        return 0
    except KeyboardInterrupt:
        print("Interrupted by user.")
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
