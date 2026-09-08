"""Export current source with every non-cache Markdown document, no runtime data.

Creates a new directory only. Never copies .git history or changes local originals.
The caller reviews the resulting manifest before initializing a new public repo.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
CACHE_NAMES = {".git", ".venv", ".venv-firmware", "node_modules", "__pycache__", ".pytest_cache",
               ".pio", "dist", "build", "build-output", "build-xiao-esp32s3-sense",
               ".ruff_cache", ".mypy_cache", "playwright-report", "test-results"}
SOURCE_ROOTS = {"apps", "services", "scripts", "firmware", "tools", "demo"}
TEXT_SUFFIXES = {".py", ".ps1", ".bat", ".ts", ".tsx", ".js", ".mjs", ".css", ".html", ".svg",
                 ".json", ".txt", ".ini", ".csv", ".yaml", ".yml", ".toml", ".h", ".cpp", ".c"}
ROOT_FILES = {".gitignore", ".env.example", "LICENSE", "pytest.ini"}
GENERATED_CONFIGS = {"apps/web/vite.config.js", "apps/web/vite.config.d.ts",
                     "apps/web/playwright.config.js", "apps/web/playwright.config.d.ts"}
SECRET_PATTERNS = [r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})",
                   r"sk-(?:proj-)?[A-Za-z0-9_-]{30,}",
                   r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"]


def cached(parts):
    return any(p in CACHE_NAMES or p.startswith(("ai-compat-", ".build-", ".archive-")) for p in parts)


def selected(relative):
    if cached(relative.parts) or relative.parts[:2] == ("data", "public-release"):
        return False
    if relative.suffix.lower() == ".md":
        return True
    if relative.as_posix() in GENERATED_CONFIGS:
        return False
    if len(relative.parts) == 1:
        return relative.name in ROOT_FILES or relative.suffix == ".bat" or relative.name.startswith("requirements") and relative.suffix == ".txt"
    return relative.parts[0] in SOURCE_ROOTS and relative.suffix.lower() in TEXT_SUFFIXES and relative.name != ".env"


def redact_markdown(content):
    # Public documentation examples remain examples, not local host identities.
    content = re.sub(r"(?<=电脑实际连接 )`[^`]+`", "`[本机热点名称已脱敏]`", content)
    content = re.sub(r"(?i)[A-Z]:[\\/]+Users[\\/]+[^\s`\"<>|]*?object-memory", "<PROJECT_ROOT>", content)
    content = re.sub(r"(?i)[A-Z]:[\\/]+Users[\\/]+[^\s`\"<>|]+", "<LOCAL_PATH>", content)
    content = re.sub(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "[LOCAL_ID]", content)
    content = re.sub(r"(?i)(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])", "[LOCAL_ID]", content)
    content = re.sub(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}:){5}[0-9a-f]{2}(?![0-9a-f])", "[DEVICE_MAC]", content)
    content = re.sub(r"\b(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b", "192.168.1.20", content)
    content = re.sub(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+", "[PRIVATE_IMAGE_OMITTED]", content)
    # Media hashes can identify a private picture; model/checksum docs elsewhere
    # keep their public hashes. Only explicitly private evidence contexts redact.
    content = re.sub(r"(?im)^.*(?:截图|实拍|关键帧|参考图|媒体|screenshot|source_frame).*\b[0-9a-f]{64}\b.*$",
                     lambda m: re.sub(r"(?i)\b[0-9a-f]{64}\b", "[PRIVATE_MEDIA_HASH]", m.group(0)), content)
    content = re.sub(r"(?i)(://)[^/\s@]+:[^/\s@]+@", r"\1[REDACTED]@", content)
    content = re.sub(r"(?i)([?&](?:token|password|pairing_code|key)=)[^\s&`)]+", r"\1[REDACTED]", content)
    for pattern in SECRET_PATTERNS:
        content = re.sub(pattern, "[REDACTED_SECRET]", content)
    return content


def export(source, destination):
    source, destination = source.resolve(strict=True), destination.resolve()
    if destination.exists():
        raise ValueError("Destination must not exist; existing user files are never overwritten")
    files = []
    for folder, dirs, names in os.walk(source, followlinks=False):
        parent = Path(folder)
        dirs[:] = [name for name in dirs if not cached((name,)) and not (parent / name).is_symlink()
                   and not getattr(parent / name, "is_junction", lambda: False)()
                   and (parent / name).relative_to(source).parts[:2] != ("data", "public-release")]
        for name in names:
            path = parent / name
            relative = path.relative_to(source)
            if not selected(relative):
                continue
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise ValueError("Public source must not contain links: " + str(relative))
            path.resolve(strict=True).relative_to(source)
            files.append((path, relative))
    private_hashes = set()
    for path, relative in files:
        if relative.suffix.lower() == ".md":
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                if re.search(r"用户原图|原始参考图|data/real-media/", line):
                    private_hashes.update(re.findall(r"(?i)\b[0-9a-f]{64}\b", line))
    planned = []
    for path, relative in sorted(files):
        if path.stat().st_size > 5_000_000:
            raise ValueError("Unexpected large source file: " + str(relative))
        text = path.read_text(encoding="utf-8-sig")
        if "\x00" in text:
            raise ValueError("Unexpected binary source: " + str(relative))
        clean = redact_markdown(text) if relative.suffix.lower() == ".md" else text
        if relative.suffix.lower() == ".md":
            for value in private_hashes:
                clean = clean.replace(value, "[PRIVATE_MEDIA_HASH]")
        if any(re.search(p, clean) for p in SECRET_PATTERNS):
            raise ValueError("Credential-like content needs review: " + str(relative))
        # Captured runtime Markdown is documentation, never a tracked runtime path.
        target = Path("docs/runtime-notes") / relative if relative.parts[0] == "data" else relative
        if (relative.suffix.lower() == ".md" and relative.parts[0] in {"audit", "data"}) or (relative.parts[0] == "docs" and "REPORT" in relative.name):
            clean = "> 公开历史文档副本：本机身份信息已脱敏，私人数据库与实拍媒体未公开。历史结果不代表当前版本通过验收；当前限制请见 README。\n\n" + clean
        planned.append((relative, target, clean.encode("utf-8"), clean != text))
    destination.mkdir(parents=True)
    records = []
    for relative, target, data, changed in planned:
        output = destination / target
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
        records.append({"source": relative.as_posix(), "path": target.as_posix(), "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(), "sanitized": changed})
    return {"file_count": len(records), "markdown_count": sum(r["source"].lower().endswith(".md") for r in records),
            "source_bytes": sum(r["bytes"] for r in records), "files": records,
            "private_data_included": False, "git_history_included": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    result = export(ROOT, args.output)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "files"}, ensure_ascii=False))
