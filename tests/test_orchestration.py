from pathlib import Path

import pytest

from test_run_pipeline import load_runner


def test_stage_inputs_use_required_file_patterns(tmp_path):
    runner = load_runner()
    dirs = runner.create_task_dirs(tmp_path / "task")
    (dirs["step2"] / "repair_batch_summary.json").write_text(
        "{}", encoding="utf-8"
    )

    with pytest.raises(FileNotFoundError, match="property_chains_0422"):
        runner.required_stage_inputs("02.2", dirs)

    source = dirs["step2"] / "paper_property_chains_0422.json"
    source.write_text("{}", encoding="utf-8")
    assert runner.required_stage_inputs("02.2", dirs) == [source]


def test_failed_stage_marks_pipeline_failed(monkeypatch, tmp_path):
    runner = load_runner()
    dirs = runner.create_task_dirs(tmp_path / "task")
    config = {
        "dirs": dirs,
        "steps": ["01"],
        "model": "deepseek-flash",
        "md_dir": tmp_path,
        "doi_csv": tmp_path / "doi.csv",
        "max_workers": 1,
        "api_key": "test-key",
    }
    monkeypatch.setattr(
        runner,
        "run_stage",
        lambda *args: runner.StageResult("01", False, 0, 1, "failed"),
    )

    result = runner.run_pipeline(config)

    assert result["ok"] is False
    assert result["failed_stages"] == ["01"]
    assert (dirs["root"] / "summary.json").is_file()


def test_injected_script_is_deleted_and_uses_current_python(monkeypatch, tmp_path):
    runner = load_runner()
    script = tmp_path / "source.py"
    marker = tmp_path / "marker.txt"
    script.write_text(
        "from pathlib import Path\n"
        "VALUE = 'old'\n"
        "# APP_CONFIG_OVERRIDE_POINT\n"
        "Path(OUTPUT).write_text(VALUE, encoding='utf-8')\n",
        encoding="utf-8",
    )

    completed = runner._run_injected_script(
        script,
        {"VALUE": "'new'", "OUTPUT": f"r'{marker}'"},
        cwd=tmp_path,
        log_path=tmp_path / "run.log",
    )

    assert completed.returncode == 0
    assert marker.read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob("skill_stage_*.py"))
