# Input and output contract

## Inputs

- Markdown directory: recursively scanned for `.md` files.
- DOI CSV: optional UTF-8 CSV. The runner recognizes a `doi` column; without a
  CSV it derives DOI values from filenames such as
  `10.1002_adma.202302997.md` → `10.1002/adma.202302997`.
- Model: `deepseek-flash` or `deepseek-v4-pro`.
- Key: `DEEPSEEK_API_KEY`, then `DASHSCOPE_API_KEY`, then an explicitly passed
  local env file.

## Step modes

| Mode | Work performed | Required upstream files |
|---|---|---|
| `01` | Structured extraction | Markdown + DOI CSV/inference |
| `02.1` | Build property chains | step 01 paper JSON |
| `02.2` | QC and repair chains | `*_property_chains_0422.json` |
| `03` | Clean repaired chains | `*_final.json` |
| `04` | Generate QA | `*_cleaned.json` |
| `chain` | Run 01 through 03 | Markdown + DOI CSV/inference |
| `full` | Run 01 through 04 | Markdown + DOI CSV/inference |

Single-step modes search only the current `--output-dir`. They never reuse an
unrelated historical task directory.

## Output layout

```text
task-directory/
├── step1_extraction/
├── step2_chains/
├── step3_cleaned/
├── step4_qa/
├── manifest.json
├── summary.json
├── errors.jsonl
└── run.log
```

`manifest.json` records input paths, model, selected steps, timestamps, and
produced files. `summary.json` records per-stage successful, failed, and skipped
counts. Logs and error records never contain API keys.

The command exits nonzero when a requested stage fails or when its expected
outputs are missing.

