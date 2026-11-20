#!/usr/bin/env python3
"""Portable CLI for the battery literature extraction skill."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping, NamedTuple, Optional, Sequence


MODELS = ("deepseek-flash", "deepseek-v4-pro")
STEP_MODES = ("01", "02.1", "02.2", "03", "04", "chain", "full")
SKILL_ROOT = Path(__file__).resolve().parents[1]
PIPELINE_DIR = SKILL_ROOT / "scripts" / "pipeline"
PROMPT_DIR = SKILL_ROOT / "assets" / "prompts"
SCRIPTS = {
    "01": PIPELINE_DIR / "step01_extract.py",
    "02.1": PIPELINE_DIR / "step02_build_chains.py",
    "02.2": PIPELINE_DIR / "step02_repair_chains.py",
    "03": PIPELINE_DIR / "step03_clean.py",
    "04": PIPELINE_DIR / "step04_generate_qa.py",
}


class StageResult(NamedTuple):
    stage: str
    ok: bool
    success_count: int
    fail_count: int
    message: str


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract structured battery-paper data and build QA datasets."
    )
    parser.add_argument("--md-dir", required=True, type=Path)
    parser.add_argument("--doi-csv", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--model", choices=MODELS, default="deepseek-flash")
    parser.add_argument("--steps", choices=STEP_MODES, default="full")
    parser.add_argument("--max-workers", type=int, default=1)
    parser.add_argument("--key-env-file", type=Path)
    return parser.parse_args(argv)


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise FileNotFoundError(f"API key env file does not exist: {path}")
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def resolve_api_key(
    key_env_file: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> str:
    values = os.environ if environ is None else environ
    for name in ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY"):
        value = values.get(name, "").strip()
        if value:
            return value
    if key_env_file is not None:
        file_values = _read_env_file(Path(key_env_file))
        for name in ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY"):
            value = file_values.get(name, "").strip()
            if value:
                return value
    raise ValueError(
        "Missing DeepSeek API key. Set DEEPSEEK_API_KEY or DASHSCOPE_API_KEY, "
        "or pass --key-env-file."
    )


def discover_markdown(md_dir: Path) -> list[Path]:
    root = Path(md_dir).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Markdown directory does not exist: {root}")
    files = sorted(
        (path for path in root.rglob("*.md") if path.is_file()),
        key=lambda path: str(path).lower(),
    )
    if not files:
        raise FileNotFoundError(f"No Markdown files found under: {root}")
    return files


def _doi_from_stem(stem: str) -> str:
    normalized = re.sub(r"^(10\.\d{4,9})_", r"\1/", stem.strip(), count=1)
    if not re.match(r"^10\.\d{4,9}/\S+$", normalized, flags=re.IGNORECASE):
        raise ValueError(f"Cannot infer DOI from Markdown filename: {stem}.md")
    return normalized


def derive_doi_csv(md_files: Iterable[Path], task_dir: Path) -> Path:
    root = Path(task_dir)
    root.mkdir(parents=True, exist_ok=True)
    csv_path = root / "derived_dois.csv"
    dois = [_doi_from_stem(Path(path).stem) for path in md_files]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["doi"])
        writer.writerows([[doi] for doi in dois])
    return csv_path


def selected_steps(mode: str) -> list[str]:
    if mode == "chain":
        return ["01", "02.1", "02.2", "03"]
    if mode == "full":
        return ["01", "02.1", "02.2", "03", "04"]
    if mode not in STEP_MODES:
        raise ValueError(f"Unsupported step mode: {mode}")
    return [mode]


def create_task_dirs(output_dir: Path) -> dict[str, Path]:
    root = Path(output_dir).expanduser().resolve()
    dirs = {
        "root": root,
        "step1": root / "step1_extraction",
        "step2": root / "step2_chains",
        "step3": root / "step3_cleaned",
        "step4": root / "step4_qa",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def required_stage_inputs(stage: str, dirs: Mapping[str, Path]) -> list[Path]:
    patterns = {
        "02.2": ("step2", "*_property_chains_0422.json"),
        "03": ("step2", "*_final.json"),
        "04": ("step3", "*_cleaned.json"),
    }
    if stage == "02.1":
        excluded = {"summary.json", "md_index.json"}
        files = sorted(
            path
            for path in dirs["step1"].glob("*.json")
            if path.name not in excluded
        )
        pattern = "step 01 paper JSON"
    elif stage in patterns:
        dir_key, glob_pattern = patterns[stage]
        files = sorted(dirs[dir_key].glob(glob_pattern))
        pattern = glob_pattern
    elif stage == "01":
        return []
    else:
        raise ValueError(f"Unsupported stage: {stage}")
    if not files:
        raise FileNotFoundError(f"Missing required input for stage {stage}: {pattern}")
    return files


def _append_log(log_path: Path, text: str) -> None:
    if not text:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(text)
        if not text.endswith("\n"):
            handle.write("\n")


def _run_injected_script(
    script_path: Path,
    overrides: Mapping[str, str],
    *,
    cwd: Path,
    log_path: Path,
    extra_env: Optional[Mapping[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    source = Path(script_path).read_text(encoding="utf-8")
    lines = source.splitlines()
    marker_index = next(
        (index + 1 for index, line in enumerate(lines)
         if line.strip() == "# APP_CONFIG_OVERRIDE_POINT"),
        -1,
    )
    if marker_index < 0:
        raise ValueError(f"Missing APP_CONFIG_OVERRIDE_POINT: {script_path}")
    injection = ["", "# ==== skill runtime overrides ===="]
    injection.extend(f"{name} = {value}" for name, value in overrides.items())
    injection.append("# ==== end skill runtime overrides ====")
    rendered = "\n".join(lines[:marker_index] + injection + lines[marker_index:])

    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            prefix="skill_stage_",
            delete=False,
            dir=Path(script_path).parent,
            encoding="utf-8",
        ) as handle:
            handle.write(rendered)
            temp_path = Path(handle.name)
        env = os.environ.copy()
        env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        if extra_env:
            env.update(extra_env)
        completed = subprocess.run(
            [sys.executable, str(temp_path)],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        _append_log(log_path, completed.stdout)
        _append_log(log_path, completed.stderr)
        return completed
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _extract_balanced_json(text: str) -> Optional[str]:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def _prepare_step01_for_builder(dirs: Mapping[str, Path]) -> Path:
    source_files = required_stage_inputs("02.1", dirs)
    destination = dirs["step1"] / "_preprocessed_for_builder"
    destination.mkdir(parents=True, exist_ok=True)
    written = 0
    for source in source_files:
        try:
            data = json.loads(source.read_text(encoding="utf-8-sig"))
            response = data.get("response") if isinstance(data, dict) else None
            if isinstance(response, str):
                extracted = _extract_balanced_json(response)
                if extracted:
                    parsed = json.loads(extracted)
                    if isinstance(parsed, dict):
                        parsed["doi"] = data.get("doi", "")
                        data = parsed
            (destination / source.name).write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            written += 1
        except Exception as exc:
            raise ValueError(f"Cannot preprocess step 01 output {source.name}: {exc}") from exc
    if written == 0:
        raise RuntimeError("No step 01 outputs were prepared for stage 02.1")
    return destination


def _path_expr(path: Path) -> str:
    return f"Path(r'{Path(path)}')"


def run_stage(stage: str, config: Mapping[str, object]) -> StageResult:
    dirs = config["dirs"]
    assert isinstance(dirs, Mapping)
    log_path = Path(dirs["root"]) / "run.log"
    api_env = {"DEEPSEEK_API_KEY": str(config["api_key"])}
    model = str(config["model"])
    workers = str(config["max_workers"])

    if stage == "01":
        overrides = {
            "MD_ROOT": _path_expr(Path(config["md_dir"])),
            "CSV_PATH": _path_expr(Path(config["doi_csv"])),
            "PROMPT_PATH": _path_expr(PROMPT_DIR / "extraction.md"),
            "API_KEY_ENV_FILE": _path_expr(Path(dirs["root"]) / ".unused.env"),
            "OUTPUT_DIR": _path_expr(Path(dirs["step1"])),
            "DEFAULT_MODEL": repr(model),
            "MAX_WORKERS": workers,
            "MAX_RETRIES": "3",
            "REQUEST_TIMEOUT": "1800",
        }
        expected = lambda: [
            path for path in Path(dirs["step1"]).glob("*.json")
            if path.name not in {"summary.json", "md_index.json"}
        ]
    elif stage == "02.1":
        input_dir = _prepare_step01_for_builder(dirs)
        overrides = {
            "INPUT_DIR": _path_expr(input_dir),
            "OUTPUT_DIR": _path_expr(Path(dirs["step2"])),
            "MAX_WORKERS": workers,
            "SKIP_IF_OUTPUT_EXISTS": "False",
            "ERROR_LOG_PATH": _path_expr(Path(dirs["step2"]) / "builder_batch_errors.jsonl"),
            "SUMMARY_PATH": _path_expr(Path(dirs["step2"]) / "builder_batch_summary.json"),
        }
        expected = lambda: list(Path(dirs["step2"]).glob("*_property_chains_0422.json"))
    elif stage == "02.2":
        required_stage_inputs(stage, dirs)
        overrides = {
            "ENV_PATH": repr(str(Path(dirs["root"]) / ".unused.env")),
            "V3_DIR": _path_expr(Path(dirs["step2"])),
            "PROMPT_PATH": _path_expr(PROMPT_DIR / "property-chain-repair.md"),
            "OUTPUT_DIR": _path_expr(Path(dirs["step2"])),
            "MODEL_NAME": repr(model),
            "API_MAX_WORKERS": workers,
            "API_MAX_RETRIES": "3",
            "REQUEST_TIMEOUT": "1800",
            "FILE_PARALLELISM": "1",
            "SKIP_IF_OUTPUT_EXISTS": "False",
            "GLOBAL_ERROR_LOG_PATH": _path_expr(Path(dirs["step2"]) / "repair_batch_errors.jsonl"),
            "SUMMARY_PATH": _path_expr(Path(dirs["step2"]) / "repair_batch_summary.json"),
        }
        expected = lambda: list(Path(dirs["step2"]).glob("*_final.json"))
    elif stage == "03":
        required_stage_inputs(stage, dirs)
        overrides = {
            "INPUT_DIR": _path_expr(Path(dirs["step2"])),
            "OUTPUT_DIR": _path_expr(Path(dirs["step3"])),
        }
        expected = lambda: list(Path(dirs["step3"]).glob("*_cleaned.json"))
    elif stage == "04":
        required_stage_inputs(stage, dirs)
        overrides = {
            "DEFAULT_API_KEY_ENV_FILE": repr(str(Path(dirs["root"]) / ".unused.env")),
            "DEFAULT_PROMPT_MD": repr(str(PROMPT_DIR / "property-qa.md")),
            "DEFAULT_INPUT_DIR": repr(str(Path(dirs["step3"]))),
            "DEFAULT_OUT_DIR": repr(str(Path(dirs["step4"]))),
            "DEFAULT_MODEL": repr(model),
            "DEFAULT_MAX_WORKERS": workers,
            "DEFAULT_MAX_RETRIES": "3",
            "DEFAULT_REQUEST_TIMEOUT": "1800",
            "DEFAULT_MAX_TOKENS": "32000",
        }
        expected = lambda: list(Path(dirs["step4"]).glob("*_QA.json"))
    else:
        raise ValueError(f"Unsupported stage: {stage}")

    completed = _run_injected_script(
        SCRIPTS[stage],
        overrides,
        cwd=PIPELINE_DIR,
        log_path=log_path,
        extra_env=api_env,
    )
    outputs = expected()
    ok = completed.returncode == 0 and bool(outputs)
    message = (
        f"stage {stage} produced {len(outputs)} output file(s)"
        if ok
        else f"stage {stage} failed (exit={completed.returncode}, outputs={len(outputs)})"
    )
    return StageResult(stage, ok, len(outputs) if ok else 0, 0 if ok else 1, message)


def run_pipeline(config: Mapping[str, object]) -> dict[str, object]:
    dirs = config["dirs"]
    assert isinstance(dirs, Mapping)
    root = Path(dirs["root"])
    started_at = datetime.now().astimezone().isoformat()
    manifest = {
        "started_at": started_at,
        "md_dir": str(Path(config["md_dir"])),
        "doi_csv": str(Path(config["doi_csv"])),
        "model": str(config["model"]),
        "steps": list(config["steps"]),
        "output_dir": str(root),
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    stage_results: list[StageResult] = []
    failed_stages: list[str] = []
    errors_path = root / "errors.jsonl"
    for stage in config["steps"]:
        try:
            result = run_stage(str(stage), config)
        except Exception as exc:
            result = StageResult(str(stage), False, 0, 1, str(exc))
            with errors_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(
                    {"stage": str(stage), "error": str(exc)}, ensure_ascii=False
                ) + "\n")
        stage_results.append(result)
        if not result.ok:
            failed_stages.append(result.stage)
            break

    summary = {
        "ok": not failed_stages,
        "failed_stages": failed_stages,
        "started_at": started_at,
        "finished_at": datetime.now().astimezone().isoformat(),
        "stages": [result._asdict() for result in stage_results],
    }
    (root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    manifest["finished_at"] = summary["finished_at"]
    manifest["artifacts"] = [
        str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()
    ]
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def default_output_dir() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path.cwd() / "outputs" / f"task_{stamp}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    markdown = discover_markdown(args.md_dir)
    output_dir = args.output_dir or default_output_dir()
    dirs = create_task_dirs(output_dir)
    doi_csv = args.doi_csv or derive_doi_csv(markdown, dirs["root"])
    api_key = resolve_api_key(args.key_env_file)
    print(f"Markdown files: {len(markdown)}")
    print(f"DOI CSV: {Path(doi_csv).resolve()}")
    print(f"Steps: {', '.join(selected_steps(args.steps))}")
    print(f"Output: {dirs['root']}")
    result = run_pipeline({
        "dirs": dirs,
        "steps": selected_steps(args.steps),
        "model": args.model,
        "md_dir": Path(args.md_dir).resolve(),
        "doi_csv": Path(doi_csv).resolve(),
        "max_workers": args.max_workers,
        "api_key": api_key,
    })
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
