from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_skill_metadata_and_required_resources_exist():
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = text.split("---", 2)[1]

    assert "name: battery-literature-extraction" in frontmatter
    assert "description: Use when" in frontmatter
    assert "scripts/run_pipeline.py" in text
    assert (ROOT / "references" / "input-output.md").is_file()
    assert (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines() == [
        "openai>=1.0.0",
        "python-dotenv>=1.0.0",
        "tqdm>=4.0.0",
        "truststore>=0.10.0",
    ]
