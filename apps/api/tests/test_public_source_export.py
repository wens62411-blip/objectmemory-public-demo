"""Privacy contracts for the current-source public export; no network writes."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("public_export", ROOT / "scripts/export-public-source.py")
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


def put(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_keeps_all_noncache_markdown_but_no_personal_records_or_history(tmp_path):
    source = tmp_path / "source"
    for name in ["README.md", "docs/plan.md", "audit/result.md", "data/verification/review.md"]:
        put(source, name, "Historical document\n")
    for name in [".env", "data/database/objectmemory.sqlite", "data/real-media/photo.jpg", ".git/config",
                 "node_modules/library/README.md", ".venv/package/README.md"]:
        put(source, name, "private-or-cache")
    put(source, "apps/api/app/main.py", "value = 1\n")
    put(source, ".env.example", "OM_RUNTIME_MODE=REAL\n")
    result = exporter.export(source, tmp_path / "public")
    assert result["markdown_count"] == 4
    names = {r["path"] for r in result["files"]}
    assert names == {"README.md", "docs/plan.md", "audit/result.md", "docs/runtime-notes/data/verification/review.md",
                     "apps/api/app/main.py", ".env.example"}
    assert not (tmp_path / "public/data").exists()
    assert (source / "data/real-media/photo.jpg").read_text() == "private-or-cache"


def test_markdown_redaction_preserves_failure_and_public_model_hash(tmp_path):
    source = tmp_path / "source"
    personal, model = "a" * 64, "b" * 64
    original = f"电脑实际连接 `private-hotspot`。\n用户原图 SHA256 {personal}\n模型 SHA256 {model}\n测试失败\n"
    put(source, "docs/REPORT.md", original)
    put(source, "audit/note.md", "snapshot checksum " + personal)
    exporter.export(source, tmp_path / "public")
    public = (tmp_path / "public/docs/REPORT.md").read_text(encoding="utf-8")
    assert "private-hotspot" not in public and personal not in public
    assert model in public and "测试失败" in public
    assert personal not in (tmp_path / "public/audit/note.md").read_text()
    assert (source / "docs/REPORT.md").read_text(encoding="utf-8") == original


def test_does_not_overwrite_existing_destination(tmp_path):
    put(tmp_path / "source", "README.md", "source")
    put(tmp_path / "public", "keep.txt", "keep")
    with pytest.raises(ValueError, match="must not exist"):
        exporter.export(tmp_path / "source", tmp_path / "public")
    assert (tmp_path / "public/keep.txt").read_text() == "keep"


def test_secret_like_source_fails_before_creating_output(tmp_path):
    put(tmp_path / "source", "apps/api/leak.py", "key = 'ghp_" + "x" * 40 + "'")
    with pytest.raises(ValueError, match="Credential-like"):
        exporter.export(tmp_path / "source", tmp_path / "public")
    assert not (tmp_path / "public").exists()
