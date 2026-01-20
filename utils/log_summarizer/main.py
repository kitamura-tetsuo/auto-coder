import json
import os
import sys

import click

# Ensure src is in python path
# Current file: .../auto-coder/utils/log_summarizer/main.py
# Root: .../auto-coder
# Src: .../auto-coder/src
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../src")))

from pathlib import Path
from typing import Iterator, Optional

# Universal import block
if __package__:
    from .candidate import ALGORITHM_REGISTRY
    from .llm_wrapper import LLMWrapper
    from .scorer import LogScorer
else:
    from candidate import ALGORITHM_REGISTRY
    from llm_wrapper import LLMWrapper
    from scorer import LogScorer


@click.group()
def cli():
    """Log Summarization Iterative Refinement Tool."""
    pass


def find_log_files(path: Path) -> Iterator[Path]:
    """
    Recursively find valid JSON log files in the directory.
    A valid log file must have 'stdout' or 'stderr' and MUST NOT have 'method'.
    """
    if path.is_file():
        if path.suffix == ".json":
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if ("stdout" in data or "stderr" in data) and "method" not in data:
                    yield path
            except Exception:
                pass
        return

    for p in path.rglob("*.json"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Check if it looks like a raw log (has output but no method summary tag)
            if ("stdout" in data or "stderr" in data) and "method" not in data:
                yield p
        except Exception:
            # Ignore file read/parse errors
            pass


import re

from auto_coder.llm_backend_config import get_llm_config


@cli.command()
@click.option("--log-file", required=True, type=click.Path(exists=True), help="Path to the input log file or directory.")
@click.option("--output", required=False, type=click.Path(), help="Path to save the JSON output (optional for directory mode).")
@click.option("--backend", default=None, help="LLM Backend to use (optional).")
def generate_gold(log_file: str, output: Optional[str], backend: Optional[str]):
    """
    Generate a 'Gold Standard' summary using the LLM.
    Accepts a single file or a directory.
    Output filename: {stem}_gold_{backend}.json
    """
    input_path = Path(log_file)
    target_files = list(find_log_files(input_path))

    if not target_files:
        click.echo(f"No valid JSON log files found in {log_file}")
        sys.exit(0)

    click.echo(f"Found {len(target_files)} log files to process.")

    # Resolve backend name
    if backend:
        current_backend = backend
    else:
        try:
            current_backend = get_llm_config().default_backend
        except Exception:
            current_backend = "default"

    # Sanitize backend name for filename
    safe_backend = re.sub(r"[^\w\-. ]", "_", current_backend)

    wrapper = LLMWrapper(backend_name=backend)

    for log_path in target_files:
        try:
            # Determine output path first to check existence
            if input_path.is_file() and output:
                out_path = Path(output)
            else:
                # New naming convention: {stem}_gold_{backend}.json
                out_path = log_path.parent / f"{log_path.stem}_gold_{safe_backend}.json"

            # Check for existing file and skip if it already exists
            if out_path.exists():
                try:
                    with open(out_path, "r", encoding="utf-8") as f:
                        existing_data = json.load(f)

                    # We trust the filename uniqueness now, but double check content if needed?
                    # Actually, if the file exists with the specific backend suffix, we skip.
                    click.echo(f"Skipping {log_path.name}: Gold summary already exists at {out_path.name}.")
                    continue
                except Exception:
                    # If read fails, proceed to regenerate
                    pass

            click.echo(f"Processing {log_path.name}...")
            with open(log_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Extract content from JSON log structure
            log_content = data.get("stdout", "") or data.get("stderr", "")
            if not log_content:
                click.echo(f"  Skipping {log_path.name}: empty content")
                continue

            summary = wrapper.summarize_log(log_content)

            result = {"source_file": str(log_path), "method": "llm_gold", "llm_backend": current_backend, "summary": summary, "original_data": data}  # Preserve original metadata if needed

            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)

            click.echo(f"  Saved to {out_path}")

        except Exception as e:
            click.echo(f"  Error processing {log_path.name}: {e}", err=True)


@cli.command()
@click.option("--log-file", required=True, type=click.Path(exists=True), help="Path to the input log file or directory.")
@click.option("--output", required=False, type=click.Path(), help="Path to save the JSON output (optional for directory mode).")
@click.option("--algorithm", default="baseline", help="Name of the algorithm to run (default: baseline).")
def run_candidate(log_file: str, output: Optional[str], algorithm: str):
    """
    Run a specific Candidate Algorithm to generate a summary.
    Accepts a single file or a directory.
    """
    if algorithm not in ALGORITHM_REGISTRY:
        click.echo(f"Error: Algorithm '{algorithm}' not found. Available: {', '.join(ALGORITHM_REGISTRY.keys())}", err=True)
        sys.exit(1)

    input_path = Path(log_file)
    target_files = list(find_log_files(input_path))

    if not target_files:
        click.echo(f"No valid JSON log files found in {log_file}")
        sys.exit(0)

    click.echo(f"Found {len(target_files)} log files. Running '{algorithm}'...")
    SummarizerClass = ALGORITHM_REGISTRY[algorithm]

    for log_path in target_files:
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            log_content = data.get("stdout", "") or data.get("stderr", "")
            if not log_content:
                # If both are empty, skip or treat as empty
                pass

            algo = SummarizerClass()
            summary = algo.summarize(log_content)

            result = {"source_file": str(log_path), "method": f"candidate_{algorithm}", "summary": summary}

            # Determine output path
            if input_path.is_file() and output:
                out_path = Path(output)
            else:
                out_path = log_path.parent / f"{log_path.stem}_{algorithm}.json"

            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)

            click.echo(f"Generated {out_path.name}")

        except Exception as e:
            click.echo(f"Error running candidate algorithm on {log_path.name}: {e}", err=True)


@cli.command()
@click.option("--log-file", required=True, type=click.Path(exists=True), help="Path to the input log file or directory.")
@click.option("--output-dir", required=False, type=click.Path(exists=True, file_okay=False, dir_okay=True), help="Directory to save results (optional, defaults to source directory).")
def run_all(log_file: str, output_dir: Optional[str]):
    """
    Run ALL registered algorithms on the log file(s).
    Accepts a single file or a directory.
    Output files will be named: {log_filename}_{algorithm}.json
    """
    input_path = Path(log_file)
    target_files = list(find_log_files(input_path))

    if not target_files:
        click.echo(f"No valid JSON log files found in {log_file}")
        sys.exit(0)

    click.echo(f"Found {len(target_files)} log files. Running all algorithms...")

    for log_path in target_files:
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            log_content = data.get("stdout", "") or data.get("stderr", "")

            # Default output directory is the same as the log file
            target_out_dir = Path(output_dir) if output_dir else log_path.parent
            log_stem = log_path.stem

            for name, SummarizerClass in ALGORITHM_REGISTRY.items():
                try:
                    algo = SummarizerClass()
                    summary = algo.summarize(log_content)

                    output_filename = f"{log_stem}_{name}.json"
                    output_file = target_out_dir / output_filename

                    result_data = {"source_file": str(log_path), "method": f"candidate_{name}", "summary": summary}

                    with open(output_file, "w", encoding="utf-8") as f:
                        json.dump(result_data, f, indent=2, ensure_ascii=False)

                    click.echo(f"  Generated {output_filename}")

                except Exception as e:
                    click.echo(f"  Error running '{name}' on {log_path.name}: {e}", err=True)

        except Exception as e:
            click.echo(f"Error processing {log_path.name}: {e}", err=True)

    click.echo("Completed.")


@cli.command()
@click.option("--gold-file", required=True, type=click.Path(exists=True), help="Path to the Gold Standard JSON file.")
@click.option("--candidate-file", required=True, type=click.Path(exists=True), help="Path to the Candidate JSON file.")
@click.option("--output", type=click.Path(), help="Path to save the evaluation result JSON (optional).")
def evaluate(gold_file: str, candidate_file: str, output: Optional[str]):
    """
    Evaluate a candidate summary against a gold standard.
    Calculates Precision, Recall, and F1 score based on line matching.
    """
    try:
        with open(gold_file, "r", encoding="utf-8") as f:
            gold_data = json.load(f)

        with open(candidate_file, "r", encoding="utf-8") as f:
            candidate_data = json.load(f)

        gold_summary = gold_data.get("summary", "")
        candidate_summary = candidate_data.get("summary", "")

        scorer = LogScorer()
        scores = scorer.calculate_score(candidate_summary, gold_summary)

        # Display results on stdout
        click.echo("Evaluation Results:")
        click.echo(f"  Precision: {scores['precision']:.4f}")
        click.echo(f"  Recall:    {scores['recall']:.4f}")
        click.echo(f"  F1 Score:  {scores['f1']:.4f}")
        click.echo(f"  Matches:   {scores['matches']} / {scores['gold_count']} gold lines")

        if output:
            result = {"gold_source": gold_file, "candidate_source": candidate_file, "scores": scores}
            with open(output, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
            click.echo(f"Evaluation results saved to {output}")

    except Exception as e:
        click.echo(f"Error evaluating files: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.option("--gold-file", required=False, type=click.Path(exists=True), help="Path to the Gold Standard JSON file. If omitted, searches for ALL *_gold*.json files in candidates-dir.")
@click.option("--candidates-dir", required=True, type=click.Path(exists=True, file_okay=False, dir_okay=True), help="Directory containing Candidate JSON files.")
@click.option("--output", type=click.Path(), help="Path to save the aggregated evaluation results JSON (optional).")
def evaluate_all(gold_file: Optional[str], candidates_dir: str, output: Optional[str]):
    """
    Evaluate candidate JSON files against gold standards.

    Mode 1 (Single Gold): Provide --gold-file. Evaluates all candidates in --candidates-dir against that single gold file.
    Mode 2 (Hierarchical): Omit --gold-file. Searches --candidates-dir for files matching pattern '{stem}_gold_{backend}.json' (or legacy _gold.json).
                           Matches them with sibling files '{stem}_{algo}.json'.
                           Ranks algorithms by AVERAGE F1 score across all logs.
    """
    try:
        candidates_path = Path(candidates_dir)
        scorer = LogScorer()
        results = []

        # --- Mode 1: Single Gold File ---
        if gold_file:
            gold_path = Path(gold_file)
            with open(gold_path, "r", encoding="utf-8") as f:
                gold_data = json.load(f)
            gold_summary = gold_data.get("summary", "")

            click.echo(f"Evaluating candidates in {candidates_dir} against {gold_file}...")

            candidate_files = list(candidates_path.glob("*.json"))
            for cand_file in candidate_files:
                if cand_file.resolve() == gold_path.resolve():
                    continue
                try:
                    with open(cand_file, "r", encoding="utf-8") as f:
                        cand_data = json.load(f)
                    if "summary" not in cand_data:
                        continue

                    method_name = cand_data.get("method", cand_file.stem)
                    scores = scorer.calculate_score(cand_data.get("summary", ""), gold_summary)
                    results.append({"filename": cand_file.name, "method": method_name, "scores": scores})
                except Exception:
                    pass

            # Sort by F1
            results.sort(key=lambda x: x["scores"]["f1"], reverse=True)

            _print_table(results)

            if output:
                _save_output(output, {"gold_source": str(gold_path), "candidates_dir": str(candidates_path), "rankings": results})

        # --- Mode 2: Hierarchical Discovery ---
        else:
            click.echo(f"Searching for gold standards and matching candidates in {candidates_dir}...")

            # Map: Algorithm Name -> List of Scores
            algo_scores = {}

            # Find all potential gold files
            # Regex to match {stem}_gold_{backend}.json or {stem}_gold.json
            # Group 1 = stem, Group 2 = backend string (optional)
            gold_pattern = re.compile(r"^(.*)_gold(?:_(.*))?\.json$")

            found_gold_count = 0

            for file_path in candidates_path.rglob("*.json"):
                match = gold_pattern.match(file_path.name)
                if not match:
                    continue

                stem = match.group(1)
                # Ensure the file is not a candidate result that happens to have _gold_ in it?
                # The pattern assumes end with _gold... so candidates like stem_algo.json won't match
                # UNLESS algo name starts with gold_. Unlikely collision.

                found_gold_count += 1
                gold_p = file_path

                try:
                    with open(gold_p, "r", encoding="utf-8") as f:
                        gold_data = json.load(f)
                    gold_summary = gold_data.get("summary", "")

                    # Find siblings that start with stem and end in .json
                    # candidates are expected to be {stem}_{algo}.json
                    # We need to be careful not to match the gold file itself or other gold files for same stem
                    for sibling in gold_p.parent.glob(f"{stem}_*.json"):
                        if sibling.resolve() == gold_p.resolve():
                            continue

                        # Check if sibling is another gold file
                        if gold_pattern.match(sibling.name):
                            continue

                        try:
                            with open(sibling, "r", encoding="utf-8") as f:
                                sib_data = json.load(f)
                            if "summary" not in sib_data:
                                continue

                            # Extract Algo Name
                            # filename: stem_algo.json -> algo
                            # sibling.name starts with stem + "_"
                            # suffix = sibling.name[len(stem)+1 : -5]
                            algo_suffix = sibling.name[len(stem) + 1 : -5]
                            method_name = sib_data.get("method", algo_suffix)

                            scores = scorer.calculate_score(sib_data.get("summary", ""), gold_summary)

                            if method_name not in algo_scores:
                                algo_scores[method_name] = []
                            algo_scores[method_name].append(scores)

                        except Exception:
                            pass
                except Exception as e:
                    click.echo(f"Error processing gold file {gold_p.name}: {e}", err=True)

            if found_gold_count == 0:
                click.echo("No gold standard files found matching pattern *_gold*.json.")
                sys.exit(0)

            click.echo(f"Found {found_gold_count} gold standard files.")

            # Aggregate Results
            aggregated_results = []
            for algo, score_list in algo_scores.items():
                count = len(score_list)
                avg_f1 = sum(s["f1"] for s in score_list) / count
                avg_prec = sum(s["precision"] for s in score_list) / count
                avg_rec = sum(s["recall"] for s in score_list) / count

                aggregated_results.append({"method": algo, "count": count, "scores": {"f1": avg_f1, "precision": avg_prec, "recall": avg_rec}})

            aggregated_results.sort(key=lambda x: x["scores"]["f1"], reverse=True)

            _print_table(aggregated_results, aggregated=True)

            if output:
                _save_output(output, {"candidates_dir": str(candidates_path), "mode": "hierarchical", "rankings": aggregated_results})

    except Exception as e:
        click.echo(f"Error in evaluate-all: {e}", err=True)
        # print stack trace for debug
        import traceback

        traceback.print_exc()
        sys.exit(1)


def _print_table(results, aggregated=False):
    click.echo("\n" + "=" * 80)
    if aggregated:
        click.echo(f"{'Rank':<5} | {'Method':<30} | {'Count':<6} | {'Avg F1':<8} | {'Avg Prec':<8} | {'Avg Rec':<8}")
    else:
        click.echo(f"{'Rank':<5} | {'Method / File':<30} | {'F1':<8} | {'Prec':<8} | {'Recall':<8}")
    click.echo("-" * 80)

    for idx, res in enumerate(results, 1):
        s = res["scores"]
        name = res["method"]
        if len(name) > 28:
            name = name[:25] + "..."

        if aggregated:
            click.echo(f"{idx:<5} | {name:<30} | {res['count']:<6} | {s['f1']:.4f}   | {s['precision']:.4f}   | {s['recall']:.4f}")
        else:
            click.echo(f"{idx:<5} | {name:<30} | {s['f1']:.4f}   | {s['precision']:.4f}   | {s['recall']:.4f}")

    click.echo("=" * 80 + "\n")


def _save_output(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    click.echo(f"Results saved to {path}")


if __name__ == "__main__":
    cli()
