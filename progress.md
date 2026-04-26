# deployable_auto-montage — Progress

## 2026-04-25 — Fix: 0.250s black screen near end of video

### Problem
A 0.250s black screen appeared at ~41.375s in the final MP4 output (total: 42.583s), just before the outro dip-to-black. Visible in QuickTime at the 42s mark.

### Root Cause
The program6 output XML contained V1 clips where `end - start` (timeline extent) ≠ `out - in` (source content). Example:
- Clip at timeline 39.125–41.625s (60 frames) had only 54 frames of source content
- The renderer used `out - in = 54 frames`, so the clip ended at 41.375s
- The outro-dip starts at 41.625s (frame 999 = `end`) → 6-frame (0.250s) gap → black base showed through

Same mismatch pattern caused a secondary bug: a 35-frame rush clip was silently skipped in the OTIO because the cursor overshot the next clip's start.

### Fix
Two files changed:

**`ffmpeger_otio_video_maker/program/ffmpeg_video_creator.py` — `segment_from_clip_record` (line 74)**
- Use `end - start` (timeline extent) as the authoritative clip duration
- Fallback to `out - in` only when timeline extent is zero

**`ffmpeger_otio_video_maker/program/xml_to_otio_converter.py` — `build_track_children` (line 251)**
- Advance OTIO cursor by `end - start` instead of `out - in`
- Prevents false Gap objects and skipped clips when timeline ≠ source duration

### Status
✅ Fixed and verified — black screen gone on re-run.
