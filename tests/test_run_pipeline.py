import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "scripts" / "run_pipeline.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("battery_skill_runner", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_api_key_precedence(tmp_path):
    runner = load_runner()
    env_file = tmp_path / "key.env"
    env_file.write_text("DASHSCOPE_API_KEY=file-key\n", encoding="utf-8")

    assert runner.resolve_api_key(
        env_file,
        {"DEEPSEEK_API_KEY": "deep-key", "DASHSCOPE_API_KEY": "dash-key"},
    ) == "deep-key"
    assert runner.resolve_api_key(
        env_file, {"DASHSCOPE_API_KEY": "dash-key"}
    ) == "dash-key"
    assert runner.resolve_api_key(env_file, {}) == "file-key"


def test_steps_and_model_allowlist():
    runner = load_runner()

    assert runner.selected_steps("chain") == ["01", "02.1", "02.2", "03"]
    assert runner.selected_steps("full") == ["01", "02.1", "02.2", "03", "04"]
    with pytest.raises(SystemExit):
        runner.parse_args(["--md-dir", ".", "--model", "unsupported"])


def test_markdown_count_is_file_count_not_doi_alias_count(tmp_path):
    runner = load_runner()
    (tmp_path / "b.md").write_text("b", encoding="utf-8")
    (tmp_path / "a.md").write_text("a", encoding="utf-8")
    (tmp_path / "ignore.txt").write_text("x", encoding="utf-8")

    assert [path.name for path in runner.discover_markdown(tmp_path)] == [
        "a.md",
        "b.md",
    ]


def test_missing_csv_is_derived_from_markdown_filenames(tmp_path):
    runner = load_runner()
    papers = tmp_path / "papers"
    task_dir = tmp_path / "task"
    papers.mkdir()
    paper = papers / "10.1002_adma.202302997.md"
    paper.write_text("paper", encoding="utf-8")

    csv_path = runner.derive_doi_csv([paper], task_dir)

    assert csv_path.read_text(encoding="utf-8").splitlines() == [
        "doi",
        "10.1002/adma.202302997",
    ]


def test_task_directories_are_isolated_under_output(tmp_path):
    runner = load_runner()

    dirs = runner.create_task_dirs(tmp_path / "run")

    assert dirs == {
        "root": tmp_path / "run",
        "step1": tmp_path / "run" / "step1_extraction",
        "step2": tmp_path / "run" / "step2_chains",
        "step3": tmp_path / "run" / "step3_cleaned",
        "step4": tmp_path / "run" / "step4_qa",
    }
    assert all(path.is_dir() for path in dirs.values())
