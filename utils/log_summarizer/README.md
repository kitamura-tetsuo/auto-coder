# Log Summarization Iterative Refinement Tool

This tool is designed to iteratively refine a deterministic algorithm for error log summarization by comparing it against an LLM-generated "Gold Standard".
It supports maintaining multiple candidate algorithms in parallel to Experiment with different approaches.

## Structure

- `main.py`: CLI entry point.
- `llm_wrapper.py`: Wraps `auto-coder`'s LLM client to generate summaries.
- `candidate.py`: Contains the candidate algorithms (logic to be refined) and the `ALGORITHM_REGISTRY`.

## Setup

Ensure you are in the project root and creating/using the virtual environment.

```bash
# Example
source .venv/bin/activate
```

## Usage

### 1. Generate Gold Standard Summary (using LLM)

This requires the `auto-coder` LLM configuration to be set up.

```bash
python utils/log_summarizer/main.py generate-gold \
  --log-file /path/to/your/log_dir/ \
  --output gold_summary.json \
  --backend gemini  # Optional: Specify LLM backend
```

### 2. Run Candidate Algorithm

You can run a specific algorithm by name. The default is `baseline`.

```bash
python utils/log_summarizer/main.py run-candidate \
  --log-file /path/to/your/log_dir/ \
  --output candidate_summary.json \
  --algorithm baseline
```

### 3. Run All Algorithms

To run all registered algorithms against a log file at once:

```bash
python utils/log_summarizer/main.py run-all \
  --log-file /path/to/your/log_dir/ \
  --output-dir /path/to/output/dir
```
This will generate files naming pattern: `{log_filename}_{algorithm}.json`.

### 4. Evaluate (Score)

Compare a candidate summary (JSON) against a gold standard (JSON).
The score is based on line-by-line exact matching (ignoring whitespace).

```bash
python src/auto-coder/utils/log_summarizer/main.py evaluate \
  --gold-file gold_summary.json \
  --candidate-file candidate_summary.json
```

### 5. Evaluate All (Rank)

Evaluate all JSON files in a directory against a gold standard and rank by F1 Score.

```bash
python src/auto-coder/utils/log_summarizer/main.py evaluate-all \
  --gold-file gold_summary.json \
  --candidates-dir /path/to/candidates/
```

## Adding New Algorithms

1.  Open `src/auto-coder/utils/log_summarizer/candidate.py`.
2.  Define a new class inheriting from `BaseSummarizer`.
3.  Implement the `summarize` method.
4.  Register it in `ALGORITHM_REGISTRY`.

```python
class MyNewAlgo(BaseSummarizer):
    def summarize(self, log_content: str) -> str:
        # Your logic here
        return "Summary..."

ALGORITHM_REGISTRY = {
    "baseline": BaselineSummarizer,
    "my_algo": MyNewAlgo,
}
```

## Iterative Refinement Workflow

1.  Generate a Gold Standard summary for your target log.
2.  Run your candidate algorithm(s).
3.  Compare the outputs.
4.  Modify the code in `candidate.py`.
5.  Re-run and verify improvements.
