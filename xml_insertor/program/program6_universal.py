#!/usr/bin/env python3
"""
Wrapper around program6 that automatically uses the latest XML produced by
xml_editor_after_comparser as the rush track (video/audio track 1) before all
insert overlays are added.
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path
from typing import Sequence
import xml.etree.ElementTree as ET

import program6

UNIVERSAL_OUTPUT_DIR = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/xml_editor_after_comparser/output"
)
MUSIC_ASSET_DIR = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/swisser/Universal_pipe/asset/music"
)
MUSIC_RELATIVE_MULTIPLIER = 0.4
# Target peak for music in the final mix: rush target (–1 dBFS) × MUSIC_RELATIVE_MULTIPLIER ≈ –9 dBFS
MUSIC_TARGET_PEAK_DB: float = -1.0 + 20.0 * math.log10(MUSIC_RELATIVE_MULTIPLIER)


def _music_gain_for_file(path: Path, fallback_gain: str) -> str:
    """Return Premiere-unit gain that normalises path's peak to MUSIC_TARGET_PEAK_DB."""
    peak = program6.analyze_audio_peak_db(path)
    if peak is None or not math.isfinite(peak) or peak <= -60.0:
        return fallback_gain
    return program6.db_to_premiere_units(MUSIC_TARGET_PEAK_DB - peak)


def pick_random_music_pair() -> tuple[Path, Path]:
    """Randomly select a music folder and return (intro_path, outro_path)."""
    folders = [d for d in MUSIC_ASSET_DIR.iterdir() if d.is_dir()]
    if not folders:
        raise FileNotFoundError(f"No music folders found in {MUSIC_ASSET_DIR}")
    folder = random.choice(folders)
    mp3s = list(folder.glob("*.mp3"))
    intro = next((f for f in mp3s if "intro" in f.name.lower()), None)
    outro = next((f for f in mp3s if "intro" not in f.name.lower()), None)
    if intro is None or outro is None:
        raise FileNotFoundError(
            f"Music folder {folder.name} missing intro or outro file (found: {[f.name for f in mp3s]})"
        )
    return intro, outro


def configure_universal_audio_routing() -> None:
    """
    Universal runs still need titre clips to use the extract audio lane for the
    intro-style comparison overlays, but normal insert audio is now mirrored to
    the resolved video lane inside program6 and must not be pinned here.
    """
    if getattr(program6, "_UNIVERSAL_AUDIO_PATCHED", False):
        return
    program6._UNIVERSAL_AUDIO_PATCHED = True
    original_apply_label_rules = program6.apply_label_layout_rules

    def apply_label_layout_rules_override(clip, metadata, label):
        result = original_apply_label_rules(clip, metadata, label)
        if program6.label_matches(label, "titre"):
            if clip.video_track_override == program6.INTRO_VIDEO_OVERLAY_TRACK_INDEX:
                clip.audio_track_override = program6.EXTRACT_AUDIO_TRACK_INDEX
        return result

    program6.apply_label_layout_rules = apply_label_layout_rules_override


configure_universal_audio_routing()


def resolve_reference_and_output(argv: Sequence[str]) -> tuple[Path, Path]:
    args = program6.parse_args(argv)
    if args.reference_xml:
        reference_xml = Path(args.reference_xml).expanduser()
    else:
        reference_xml = program6.find_latest_otio_xml()
    output_path = program6.determine_output_path(reference_xml, args.output)
    return reference_xml, output_path


def ensure_audio_track(
    sequence: ET.Element, track_index: int
) -> tuple[list[ET.Element], ET.Element]:
    audio_parent = sequence.find("./media")
    if audio_parent is None:
        audio_parent = ET.SubElement(sequence, "media")
    audio_section = sequence.find("./media/audio")
    if audio_section is None:
        audio_section = ET.SubElement(audio_parent, "audio")
    program6.ensure_track_count(audio_section, track_index, is_audio=True)
    audio_tracks = [track for track in audio_section.findall("track")]
    return audio_tracks, audio_tracks[track_index - 1]


def append_spec_to_track(
    track: ET.Element,
    *,
    spec: program6.AudioClipSpec,
    metadata: program6.SequenceMetadata,
    track_index: int,
    clip_counter: int,
    id_seed: int,
) -> None:
    clipitem = program6.create_audio_only_clipitem(
        clip=spec,
        metadata=metadata,
        track_index=track_index,
        clip_id_suffix=id_seed,
        group_index=70000 + id_seed,
        clip_index=clip_counter,
        clip_id_prefix="universal-audio-clipitem",
        file_id_prefix="universal-audio-file",
    )
    track.append(clipitem)


def inject_universal_audio(output_path: Path) -> None:
    if not output_path.exists():
        print(f"⚠️  Universal output missing, cannot inject audio: {output_path}")
        return
    intro_path, outro_path = pick_random_music_pair()
    print(f"    Selected music: {intro_path.parent.name} / intro={intro_path.name}, outro={outro_path.name}")
    metadata = program6.extract_metadata(output_path)
    tree = ET.parse(output_path)
    root = tree.getroot()
    sequence = root.find("./sequence")
    if sequence is None:
        print("⚠️  Output XML lacks <sequence>; skipping universal audio injection.")
        return
    duration_frames = program6.parse_int(sequence.findtext("duration"), 0)
    if duration_frames <= 0:
        print("⚠️  Unable to determine sequence duration; skipping universal audio injection.")
        return
    audio_tracks, target_track = ensure_audio_track(sequence, program6.INTRO_AUDIO_TRACK_INDEX)
    track_index = program6.INTRO_AUDIO_TRACK_INDEX
    clip_counter = len(target_track.findall("clipitem"))
    id_seed = 80000
    rush_track = (
        audio_tracks[program6.RUSH_AUDIO_TRACK_INDEX - 1]
        if len(audio_tracks) >= program6.RUSH_AUDIO_TRACK_INDEX
        else None
    )
    rush_gain_level = program6.compute_rush_peak_safe_gain_units(rush_track)
    program6.apply_gain_to_track_clipitems(
        rush_track,
        gain_level=rush_gain_level,
        predicate=program6.is_rush_clipitem,
    )
    fallback_gain = program6.linear_multiplier_to_premiere_units(
        program6.premiere_units_to_multiplier(rush_gain_level) * MUSIC_RELATIVE_MULTIPLIER
    )
    intro_gain_level = _music_gain_for_file(intro_path, fallback_gain)
    outro_gain_level = _music_gain_for_file(outro_path, fallback_gain)

    intro_duration_seconds = program6.probe_audio_duration(intro_path)
    intro_duration_frames = program6.seconds_to_frames(intro_duration_seconds, metadata.fps, allow_zero=True)
    intro_spec = program6.build_audio_spec(
        intro_path,
        metadata,
        start_frames=0,
        source_in_frames=0,
        max_duration_frames=min(duration_frames, intro_duration_frames),
        gain_level=intro_gain_level,
        fade_in_frames=0,
        fade_out_frames=0,
    )
    if intro_spec is not None:
        clip_counter += 1
        id_seed += 1
        append_spec_to_track(
            target_track,
            spec=intro_spec,
            metadata=metadata,
            track_index=track_index,
            clip_counter=clip_counter,
            id_seed=id_seed,
        )

    outro_duration_seconds = program6.probe_audio_duration(outro_path)
    outro_duration_frames = program6.seconds_to_frames(outro_duration_seconds, metadata.fps, allow_zero=True)
    outro_start = max(0, duration_frames - outro_duration_frames)
    outro_clip_duration = duration_frames - outro_start
    outro_spec = None
    if outro_clip_duration > 0:
        outro_spec = program6.build_audio_spec(
            outro_path,
            metadata,
            start_frames=outro_start,
            source_in_frames=0,
            max_duration_frames=outro_clip_duration,
            gain_level=outro_gain_level,
            fade_in_frames=0,
            fade_out_frames=0,
        )
    if outro_spec is not None:
        clip_counter += 1
        id_seed += 1
        append_spec_to_track(
            target_track,
            spec=outro_spec,
            metadata=metadata,
            track_index=track_index,
            clip_counter=clip_counter,
            id_seed=id_seed,
        )

    program6.indent_xml(root)
    with output_path.open("wb") as handle:
        handle.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        handle.write(b"<!DOCTYPE xmeml>\n")
        handle.write(ET.tostring(root, encoding="utf-8"))
    print(
        f"Injected universal intro/outro audio on track {track_index}: "
        f"intro={intro_path.name} (gain {intro_gain_level}), "
        f"outro={outro_path.name} (gain {outro_gain_level}) "
        f"(rush gain {rush_gain_level})"
    )


def find_latest_universal_xml(directory: Path = UNIVERSAL_OUTPUT_DIR) -> Path:
    if not directory.exists():
        raise FileNotFoundError(f"Universal XML directory not found: {directory}")
    candidates = [path for path in directory.glob("*.xml") if path.is_file()]
    if not candidates:
        raise FileNotFoundError(f"No XML files found inside {directory}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def needs_reference_override(argv: Sequence[str]) -> bool:
    """
    Only inject the default universal XML when the caller did not already
    provide --reference-xml and is not requesting --help/-h.
    """
    for token in argv:
        if token in ("-h", "--help"):
            return False
        if token == "--reference-xml" or token.startswith("--reference-xml="):
            return False
    return True


def main(argv: Sequence[str] | None = None) -> int:
    forwarded = list(argv) if argv is not None else list(sys.argv[1:])
    if needs_reference_override(forwarded):
        try:
            reference_xml = find_latest_universal_xml()
        except FileNotFoundError as exc:
            raise SystemExit(str(exc)) from exc
        forwarded = ["--reference-xml", str(reference_xml), *forwarded]
    try:
        _, output_path = resolve_reference_and_output(forwarded)
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from exc
    result = program6.main(forwarded)
    if result == 0:
        try:
            inject_universal_audio(output_path)
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️  Failed to inject universal audio beds: {exc}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
