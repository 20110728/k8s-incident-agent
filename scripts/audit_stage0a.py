"""Read-only baseline inventory; no DB, Kubernetes, model or application startup.

Run from the repository: python -m scripts.audit_stage0a
Exit 0 means inventory collected, NOT acceptance passed. No environment values
or dependency download URLs are included. Redirect stdout to preserve evidence.
"""
import argparse
import ast
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.check_output(
        ["git", *args], cwd=ROOT, encoding="utf-8"
    ).strip()


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    files = git("ls-files", "-z").split("\0")
    inventory, syntax_errors = [], []
    for name in sorted(filter(None, files)):
        path = ROOT / name
        if not path.is_file():
            inventory.append({"path": name, "missing": True})
            continue
        raw = path.read_bytes()
        inventory.append({
            "path": name, "bytes": len(raw),
            "sha256_lf": hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest(),
        })
        if path.suffix == ".py":
            try:
                ast.parse(raw.decode("utf-8-sig"), filename=name)
            except (SyntaxError, UnicodeError) as error:
                syntax_errors.append({"path": name, "error": str(error)})
    packages = {}
    for name in ("pytest", "pydantic", "langgraph", "langgraph-checkpoint",
                 "langgraph-checkpoint-postgres", "psycopg", "fastapi"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    from backend.app.persistence.migrations import MIGRATIONS
    print(json.dumps({
        "head": git("rev-parse", "HEAD"),
        "worktree": git("status", "--short", "--untracked-files=all"),
        "python": platform.python_version(),
        "python_supported": sys.version_info[:2] == (3, 12),
        "packages": packages,
        "tools_available": {name: shutil.which(name) is not None
                            for name in ("node", "npm", "docker", "psql")},
        "declared_application_migrations": [m.version for m in MIGRATIONS],
        "live_database_version": "not_queried",
        "live_migrations": "not_queried",
        "python_files_parsed": sum(f.endswith(".py") for f in files),
        "syntax_errors": syntax_errors,
        "tracked_files": inventory,
        "scope": "inventory_only_not_runtime_acceptance",
    }, ensure_ascii=False, indent=2))
    return 1 if syntax_errors or any(f.get("missing") for f in inventory) else 0


if __name__ == "__main__":
    raise SystemExit(main())
