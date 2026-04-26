#!/usr/bin/env python3
"""
Remove generated assets from key pipeline directories.
"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = [
    ROOT / "swisser" / "Universal_pipe" / "Insert",
    ROOT / "Insert_downloader" / "output",
    ROOT / "Insert_editor" / "output",
    ROOT / "insert_creator" / "output",
    ROOT / "Comparser" / "output",
    ROOT / "Comparser" / "output" / "second_comparser_output",
    ROOT / "Comparser" / "improved_ref_comparser" / "claude" / "output",
    ROOT / "Comparser" / "timed_AI_illustrator" / "output",
    ROOT / "Comparser" / "Approximate_string_matching" / "output",
    ROOT / "xml_editor_after_comparser" / "output",
    ROOT / "xml_insertor" / "output",
    ROOT / "premiere_automator" / "output" / "csv",
    ROOT / "premiere_automator" / "output" / "xml",
]


def wipe_directory(path: Path, skip_dirs: set[str] | None = None) -> None:
    if not path.exists():
        return
    for entry in list(path.iterdir()):
        if entry.is_dir():
            if skip_dirs and entry.name in skip_dirs:
                continue
            shutil.rmtree(entry, ignore_errors=True)
        else:
            try:
                entry.unlink()
            except OSError:
                pass


def main() -> None:
    for target in TARGETS:
        wipe_directory(target.expanduser())
    wipe_directory(
        (ROOT / "ffmpeger_otio_video_maker" / "output").expanduser(),
        skip_dirs={"old"},
    )


if __name__ == "__main__":
    main()
