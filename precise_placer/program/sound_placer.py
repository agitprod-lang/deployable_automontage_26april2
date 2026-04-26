#!/usr/bin/env python3
"""
Generate timestamped copies of a sound effect and print a Premiere ExtendScript
snippet that drops them on Audio Track 3 at the same times as the visual clips.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence

TIMESTAMP_PREFIX = "%sm%02d"
DEFAULT_EFFECT_NAME = "transition woosh ar.mov"


@dataclass(frozen=True)
class TimestampEntry:
    label: str
    seconds: int
    source_line: str


def parse_args() -> argparse.Namespace:
    script_path = Path(__file__).resolve()
    repo_root = script_path.parents[1]
    project_root = repo_root.parent
    couloirs_dir = repo_root / "input" / "swisstransfer" / "couloirs"
    swisser_insert_dir = project_root / "swisser" / "download" / "insert"
    effects_dir = repo_root / "effects"
    timed_effects_dir = effects_dir / "timed_effect"

    default_list_source = (
        swisser_insert_dir
        if swisser_insert_dir.exists()
        else couloirs_dir / "list.txt"
    )

    parser = argparse.ArgumentParser(
        description="Duplicate an effect clip for each timestamp listed in couloirs/list.txt."
    )
    parser.add_argument(
        "--list",
        type=Path,
        default=default_list_source,
        help=(
            "Path to the source list.txt or directory containing timestamped visual clips."
        ),
    )
    parser.add_argument(
        "--effect",
        type=Path,
        default=effects_dir / DEFAULT_EFFECT_NAME,
        help="Path to the single sound-effect clip that should be duplicated.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=timed_effects_dir,
        help="Destination folder that will receive timestamped copies of the effect.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the operations without copying files or writing list.txt.",
    )
    return parser.parse_args()


LEGACY_TIMESTAMP_PATTERN = re.compile(r"^(\d+)m(\d{2})", re.IGNORECASE)
PRECISE_TIMESTAMP_PATTERN = re.compile(r"^(\d{2})h(\d{2})m(\d{2})s(\d{3})ms", re.IGNORECASE)


def parse_timestamp(token: str) -> TimestampEntry | None:
    if not token:
        return None
    precise_match = PRECISE_TIMESTAMP_PATTERN.match(token)
    if precise_match:
        hours = int(precise_match.group(1))
        minutes = int(precise_match.group(2))
        seconds = int(precise_match.group(3))
        millis = int(precise_match.group(4))
        total_seconds = hours * 3600 + minutes * 60 + seconds + millis / 1000.0
        return TimestampEntry(label=precise_match.group(0), seconds=total_seconds, source_line="")
    legacy_match = LEGACY_TIMESTAMP_PATTERN.match(token)
    if not legacy_match:
        return None
    minutes = int(legacy_match.group(1))
    seconds = int(legacy_match.group(2))
    if seconds >= 60:
        return None
    label = TIMESTAMP_PREFIX % (minutes, seconds)
    total_seconds = minutes * 60 + seconds
    return TimestampEntry(label=label, seconds=total_seconds, source_line="")


def read_list(list_path: Path) -> List[TimestampEntry]:
    entries: List[TimestampEntry] = []
    if not list_path.exists():
        raise FileNotFoundError(f"Source list.txt not found: {list_path}")

    if list_path.is_dir():
        for child in sorted(list_path.iterdir()):
            if not child.is_file():
                continue
            first_token = child.name.split()[0]
            entry = parse_timestamp(first_token)
            if entry is None:
                continue
            entries.append(
                TimestampEntry(
                    label=entry.label,
                    seconds=entry.seconds,
                    source_line=child.name,
                )
            )
    else:
        with list_path.open() as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                first_token = stripped.split()[0]
                entry = parse_timestamp(first_token)
                if entry is None:
                    continue
                entries.append(
                    TimestampEntry(
                        label=entry.label,
                        seconds=entry.seconds,
                        source_line=stripped,
                    )
                )

    if not entries:
        raise ValueError(f"No timestamped entries found in {list_path}")
    entries.sort(key=lambda item: item.seconds)
    return entries


def ensure_output_dir(path: Path, dry_run: bool) -> None:
    if path.exists():
        if not path.is_dir():
            raise NotADirectoryError(f"Output path is not a directory: {path}")
        return
    if dry_run:
        print(f"[dry-run] Would create directory: {path}")
        return
    path.mkdir(parents=True, exist_ok=True)


def clean_output_dir(path: Path, dry_run: bool) -> None:
    if not path.exists():
        return
    if not path.is_dir():
        raise NotADirectoryError(f"Output path is not a directory: {path}")

    for child in path.iterdir():
        if child.is_file() or child.is_symlink():
            if dry_run:
                print(f"[dry-run] Would delete {child}")
                continue
            child.unlink()


def copy_effects(
    entries: Sequence[TimestampEntry],
    effect_path: Path,
    output_dir: Path,
    dry_run: bool,
) -> List[str]:
    if not effect_path.exists():
        raise FileNotFoundError(f"Effect file not found: {effect_path}")
    if not effect_path.is_file():
        raise FileNotFoundError(f"Effect path is not a file: {effect_path}")

    ensure_output_dir(output_dir, dry_run)

    output_names: List[str] = []
    for entry in entries:
        filename = f"{entry.label} {effect_path.name}"
        destination = output_dir / filename
        output_names.append(filename)
        if dry_run:
            print(f"[dry-run] Would copy {effect_path.name} -> {destination}")
            continue
        if destination.exists():
            destination.unlink()
        shutil.copy2(effect_path, destination)
    return output_names


def write_output_list(output_dir: Path, names: Iterable[str], dry_run: bool) -> Path:
    list_path = output_dir / "list.txt"
    names_seq = list(names)
    if dry_run:
        print(f"[dry-run] Would write list.txt with {len(names_seq)} entries.")
        return list_path

    with list_path.open("w") as handle:
        for name in names_seq:
            handle.write(f"{name}\n")
    return list_path


def build_extend_script(folder: Path, entries: Sequence[TimestampEntry]) -> str:
    folder_path = folder.as_posix()
    lines = [
        "(function () {",
        "    function stop(msg) {",
        "        alert(msg);",
        "        throw new Error(msg);",
        "    }",
        "",
        "    var sequence = app.project.activeSequence;",
        "    if (!sequence) {",
        "        stop('No active sequence.');",
        "    }",
        "",
        f"    var folderPath = \"{folder_path}\";",
        "    var listFile = new File(folderPath + '/list.txt');",
        "    if (!listFile.exists) {",
        "        stop('list.txt not found in ' + folderPath);",
        "    }",
        "    if (!listFile.open('r')) {",
        "        stop('Unable to open list.txt in ' + folderPath);",
        "    }",
        "",
        "    var items = [];",
        "    while (!listFile.eof) {",
        "        var line = listFile.readln().trim();",
        "        if (line) {",
        "            items.push(line);",
        "        }",
        "    }",
        "    listFile.close();",
        "    if (!items.length) {",
        "        stop('list.txt is empty.');",
        "    }",
        "",
        "    var targetTrackIndex = 1; // Audio track 2 (zero-based)",
        "    while (sequence.audioTracks.numTracks <= targetTrackIndex) {",
        "        sequence.audioTracks.addTrack();",
        "    }",
        "    var audioTrack = sequence.audioTracks[targetTrackIndex];",
        "",
        "    function findItemByName(name) {",
        "        function recurse(bin) {",
        "            for (var i = 0; i < bin.children.numItems; i++) {",
        "                var child = bin.children[i];",
        "                if (child.type === ProjectItemType.BIN) {",
        "                    var found = recurse(child);",
        "                    if (found) {",
        "                        return found;",
        "                    }",
        "                } else if (child.name === name) {",
        "                    return child;",
        "                }",
        "            }",
        "            return null;",
        "        }",
        "        return recurse(app.project.rootItem);",
        "    }",
        "",
        "    function toSeconds(label) {",
        "        var match = label.match(/^(\\d+)m(\\d{2})/);",
        "        if (!match) {",
        "            return null;",
        "        }",
        "        return parseInt(match[1], 10) * 60 + parseInt(match[2], 10);",
        "    }",
        "",
        "    var placed = 0;",
        "    for (var j = 0; j < items.length; j++) {",
        "        var name = items[j];",
        "        var fullPath = folderPath + '/' + name;",
        "        var file = new File(fullPath);",
        "        if (!file.exists) {",
        "            $.writeln('Missing effect clip: ' + name);",
        "            continue;",
        "        }",
        "",
        "        var projectItem = findItemByName(name);",
        "        if (!projectItem) {",
        "            var imported = app.project.importFiles([file.fsName], false, app.project.rootItem, false);",
        "            if (!imported) {",
        "                $.writeln('Failed to import: ' + name);",
        "                continue;",
        "            }",
        "            $.sleep(200);",
        "            projectItem = findItemByName(name);",
        "        }",
        "        if (!projectItem) {",
        "            $.writeln('Unable to locate project item for: ' + name);",
        "            continue;",
        "        }",
        "",
        "        var seconds = toSeconds(name);",
        "        if (seconds === null) {",
        "            $.writeln('Cannot parse timestamp for: ' + name);",
        "            continue;",
        "        }",
        "",
        "        var time = new Time();",
        "        time.seconds = seconds;",
        "        try {",
        "            audioTrack.overwriteClip(projectItem, time);",
        "            $.writeln('Placed ' + name + ' at ' + seconds + 's on audio track 2');",
        "            placed++;",
        "        } catch (err) {",
        "            $.writeln('Failed to place ' + name + ': ' + err);",
        "        }",
        "    }",
        "",
        "    alert('Inserted ' + placed + ' effect clips on audio track 2.');",
        "})();",
    ]
    return "\n".join(lines)


def main() -> None:
    args = parse_args()

    try:
        entries = read_list(args.list)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print("Running in dry-run mode; no files will be copied.")

    try:
        clean_output_dir(args.output, args.dry_run)
    except NotADirectoryError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        output_names = copy_effects(entries, args.effect, args.output, args.dry_run)
    except (FileNotFoundError, NotADirectoryError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    list_path = write_output_list(args.output, output_names, args.dry_run)

    print(f"Prepared {len(output_names)} effect clip(s).")
    if args.dry_run:
        print("list.txt would be written to:", list_path)
    else:
        print("list.txt written to:", list_path)

    print("\nExtendScript snippet for Premiere:\n")
    print(build_extend_script(args.output, entries))


if __name__ == "__main__":
    main()
