import importlib.util
from pathlib import Path
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
PACKAGER_PATH = ROOT / "scripts" / "package_skill.py"


def load_packager():
    spec = importlib.util.spec_from_file_location("battery_skill_packager", PACKAGER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_archive_excludes_secrets_and_has_skill_at_root(tmp_path):
    packager = load_packager()
    archive = packager.build_archive(ROOT, tmp_path / "skill.zip")

    with ZipFile(archive) as zip_file:
        names = zip_file.namelist()
        assert "SKILL.md" in names
        assert "scripts/run_pipeline.py" in names
        assert all(not name.endswith(".env") for name in names)
        assert all("tests/" not in name for name in names)
        assert all(
            not ({part.lower() for part in Path(name).parts} & {"output", "outputs"})
            for name in names
        )
        assert all("__pycache__" not in name for name in names)
        payload = "\n".join(
            zip_file.read(name).decode("utf-8", errors="ignore")
            for name in names
            if not name.endswith("/")
        )
        assert "F:\\work\\" not in payload
        assert "DASHSCOPE_API_KEY=" not in payload


def test_archive_contains_only_allowlisted_roots(tmp_path):
    packager = load_packager()
    archive = packager.build_archive(ROOT, tmp_path / "skill.zip")

    with ZipFile(archive) as zip_file:
        roots = {name.split("/", 1)[0] for name in zip_file.namelist()}

    assert roots == {
        "SKILL.md",
        "agents",
        "assets",
        "references",
        "requirements.txt",
        "scripts",
    }
