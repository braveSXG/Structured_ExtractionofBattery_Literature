---
name: battery-literature-extraction
description: Use when extracting structured battery-material information from paper Markdown, building or repairing property chains, cleaning chain fields, or generating battery-paper QA datasets.
---

# Battery Literature Extraction

Run the packaged pipeline for battery-paper Markdown. Use
`scripts/run_pipeline.py`; do not recreate the extraction logic in chat.

## Before running

1. Resolve the directory containing the input `.md` files.
2. Use the supplied DOI CSV when present. Without one, tell the user that DOI
   values will be inferred from Markdown filenames.
3. Resolve the requested scope. Default to `full`; use `chain` to stop after
   cleaned property chains, or a numbered step only when its upstream output
   already exists in the same task directory.
4. Confirm a DeepSeek key is available through `DEEPSEEK_API_KEY` or
   `DASHSCOPE_API_KEY`. A local `--key-env-file` is allowed only when the user
   explicitly supplies that path. Never copy or display a key.
5. Report the actual Markdown file count, not the number of DOI index aliases.

## Run

```bash
python scripts/run_pipeline.py \
  --md-dir ./inputs/papers \
  --doi-csv ./inputs/doi.csv \
  --output-dir ./outputs/run-001 \
  --model deepseek-flash \
  --steps full
```

`--doi-csv` and `--output-dir` are optional. Supported models are
`deepseek-flash` and `deepseek-v4-pro`.

Read [references/input-output.md](references/input-output.md) when selecting a
single stage, explaining filenames, or handing off results.

## Report

Return the successful, failed, and skipped counts; the task output directory;
and the main result files. Summarize actionable errors without pasting the
entire log. A nonzero process exit means the run did not complete successfully.

