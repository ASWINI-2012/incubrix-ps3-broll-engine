"""Commands for importing and validating the local media library."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from . import media_library  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Import local media and check scene coverage.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("import", help="Validate and register files in MEDIA_LIBRARY_DIR")
    validate = commands.add_parser("validate", help="Report usable footage and uncovered healthcare scenes")
    validate.add_argument("--healthcare", action="store_true", help="Check the built-in healthcare scene set")
    rights = commands.add_parser("rights-report", help="Write a per-video rights review checklist")
    rights.add_argument("--output", type=Path, default=Path("outputs/media_inspection/rights_review_report.json"))
    args = parser.parse_args()
    if args.command == "import":
        result = media_library.import_local_media()
    elif args.command == "rights-report":
        result = media_library.rights_review_report()
        output = args.output if args.output.is_absolute() else Path(__file__).resolve().parents[2] / args.output
        output.parent.mkdir(parents=True, exist_ok=True)
        result["report_path"] = str(output.resolve())
        output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    else:
        result = media_library.validate_healthcare() if args.healthcare else media_library.summary()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
