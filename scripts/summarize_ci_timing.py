"""Render a compact, truthful summary of CI timing JSONL records."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def summarize(directory: Path) -> dict[str, object]:
    events: list[dict[str, object]] = []
    invalid = 0
    for path in sorted(directory.glob("timing-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except (json.JSONDecodeError, OSError):
                invalid += 1
    starts = {str(event["span_id"]): event for event in events if event.get("event_kind") == "span_start"}
    ended = {str(event["span_id"]) for event in events if event.get("event_kind") == "span_end"}
    phases = [event for event in events if event.get("event_kind") == "pytest_phase_report"]
    phase_starts = {str(event["span_id"]): event for event in events if event.get("event_kind") == "pytest_phase_start"}
    reported_phases = {str(event["span_id"]) for event in phases if event.get("span_id") is not None}
    spans = [event for event in events if event.get("event_kind") == "span_end"]
    return {
        "schema_version": 1,
        "native_pytest_phases": phases,
        "incomplete_pytest_phases": [phase_starts[key] for key in sorted(phase_starts.keys() - reported_phases)],
        "nested_harness_spans": spans,
        "incomplete_spans": [starts[key] for key in sorted(starts.keys() - ended)],
        "invalid_records": invalid,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rendered = json.dumps(summarize(args.directory), indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
