#!/usr/bin/env python3
"""Offline behavioral checks for the packaged skill."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "scripts" / "pipeline"
sys.path.insert(0, str(PIPELINE))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def main() -> int:
    checks: dict[str, bool] = {}
    runner = _load("battery_skill_runner_selftest", ROOT / "scripts" / "run_pipeline.py")
    structured = _load("battery_skill_structured_selftest", PIPELINE / "structured_model.py")

    checks["chain_steps"] = runner.selected_steps("chain") == [
        "01", "02.1", "02.2", "03"
    ]
    checks["full_steps"] = runner.selected_steps("full") == [
        "01", "02.1", "02.2", "03", "04"
    ]
    checks["model_allowlist"] = runner.MODELS == (
        "deepseek-flash", "deepseek-v4-pro"
    )

    with tempfile.TemporaryDirectory() as directory:
        env_file = Path(directory) / "key.env"
        key_name = "DASHSCOPE" + "_API_KEY"
        env_file.write_text(f"{key_name}=x\n", encoding="utf-8")
        checks["key_precedence"] = (
            runner.resolve_api_key(
                env_file,
                {"DEEPSEEK_API_KEY": "preferred", "DASHSCOPE_API_KEY": "other"},
            )
            == "preferred"
        )

    captured: dict[str, object] = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(choices=[])

    client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    structured.create_structured_completion(
        client,
        model="deepseek-flash",
        messages=[{"role": "user", "content": "结构化抽取"}],
        temperature=0,
        timeout=1800,
        max_tokens=32000,
    )
    checks["thinking_disabled"] = captured.get("extra_body") == {
        "thinking": {"type": "disabled"}
    }
    checks["json_object"] = captured.get("response_format") == {
        "type": "json_object"
    }
    checks["max_tokens"] = captured.get("max_tokens") == 32000
    checks["json_instruction"] = any(
        "json" in str(message.get("content", ""))
        for message in captured.get("messages", [])
    )

    result = {"ok": all(checks.values()), "checks": checks}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
