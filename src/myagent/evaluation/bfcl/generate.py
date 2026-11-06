"""Rebuild the checked-in, reviewable JSONL. No models or network required."""

import json

from .integration_cases import build_integration_cases
from .schema import DEFAULT_DATASET, validate_cases
from .tool_cases import build_tool_cases


def main():
    cases = build_tool_cases() + build_integration_cases()
    summary = validate_cases(cases)
    DEFAULT_DATASET.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_DATASET.write_text("".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
