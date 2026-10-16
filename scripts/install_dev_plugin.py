#!/usr/bin/env python3
"""Install the local camofox-search plugin into an explicitly chosen Hermes home.

Copies ``plugin/camofox_search`` to ``<hermes-home>/plugins/web/camofox_search``.
Refuses to silently overwrite an existing install: ``--replace`` first moves the
current copy to ``<hermes-home>/backups/plugins/web/``. The copied tree is
verified file-by-file with SHA-256 hashes after the copy.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SOURCE = REPO_ROOT / "plugin" / "camofox_search"
PLUGIN_DESTINATION_RELATIVE = Path("plugins") / "web" / "camofox_search"
BACKUP_ROOT_RELATIVE = Path("backups") / "plugins" / "web"
_REQUIRED_PLUGIN_FILES = (
    "__init__.py", "plugin.yaml", "provider.py", "client.py", "engines.py", "settings.py",
    "rules.py", "manage.py", "advance.py", "advanced.py",
    "skills/camofox-search-rule/SKILL.md",
    "skills/camofox-search-advance/SKILL.md",
    "skills/camofox-search-advance/references/engines-cn.md",
    "skills/camofox-search-advance/references/engines-intl.md",
)
_IGNORED_DIRS = {"__pycache__", ".pytest_cache"}
_IGNORED_SUFFIXES = {".pyc", ".pyo", ".pyd"}


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target-hermes-home",
        type=Path,
        required=True,
        help="Hermes home that receives plugins/web/camofox_search.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Describe the requested change without writing files.",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace an existing plugin target after creating a timestamped backup.",
    )
    return parser.parse_args(argv)


def _ignore_development_artifacts(directory: str, names: list[str]) -> set[str]:
    return set(shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.py[cod]")(directory, names))


def _iter_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return ()
    return (path for path in sorted(root.rglob("*")) if path.is_file())


def collect_relative_file_hashes(root: Path) -> dict[str, str]:
    """SHA-256 per file (POSIX-relative path), skipping development artifacts."""
    hashes: dict[str, str] = {}
    for path in _iter_files(root):
        if any(part in _IGNORED_DIRS for part in path.parts):
            continue
        if path.suffix in _IGNORED_SUFFIXES:
            continue
        relative = path.relative_to(root).as_posix()
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _validate_plugin_source(source: Path) -> Optional[str]:
    if not source.is_dir():
        return f"plugin source directory is missing: {source}"
    missing = [name for name in _REQUIRED_PLUGIN_FILES if not (source / name).is_file()]
    if missing:
        return f"plugin source is incomplete: missing {', '.join(missing)} in {source}"
    return None


def backup_path_for(backup_home: Path, destination: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return backup_home / f"{destination.name}.backup-{timestamp}"


def install_plugin(
    *,
    source: Path,
    hermes_home: Path,
    replace: bool,
    dry_run: bool,
) -> int:
    destination = hermes_home / PLUGIN_DESTINATION_RELATIVE
    backup_home = hermes_home / BACKUP_ROOT_RELATIVE

    validation_error = _validate_plugin_source(source)
    if validation_error:
        print(validation_error, file=sys.stderr)
        return 2
    if source.resolve() == destination.resolve():
        print("refusing to install the plugin over its source directory", file=sys.stderr)
        return 2
    if destination.exists() and not replace:
        print(
            f"refusing to overwrite existing plugin target: {destination}; use --replace",
            file=sys.stderr,
        )
        return 2

    source_hashes = collect_relative_file_hashes(source)
    action = "replace" if destination.exists() else "install"

    if dry_run:
        print(f"[dry-run] would {action} {source} -> {destination}")
        if destination.exists():
            print(f"[dry-run] would back up {destination} under {backup_home}")
        print(f"[dry-run] {len(source_hashes)} file(s) would be copied and hash-verified")
        return 0

    if destination.exists():
        backup_target = backup_path_for(backup_home, destination)
        backup_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(destination), str(backup_target))
        print(f"backed up previous install -> {backup_target}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, ignore=_ignore_development_artifacts)

    installed_hashes = collect_relative_file_hashes(destination)
    if installed_hashes != source_hashes:
        print(
            "verification failed after copy: installed tree does not match source "
            f"({len(installed_hashes)} vs {len(source_hashes)} files)",
            file=sys.stderr,
        )
        return 1

    print(f"{action}ed {len(installed_hashes)} file(s) -> {destination}")
    print("next steps:")
    print("  hermes plugins enable web/camofox_search")
    print("  hermes config set web.search_backend camofox-search   # optional")
    print("  hermes config set web.extract_backend camofox-search  # optional")
    print("restart Hermes Gateway/Desktop so the copied plugin is loaded")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    return install_plugin(
        source=PLUGIN_SOURCE,
        hermes_home=args.target_hermes_home,
        replace=args.replace,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
