#!/usr/bin/env python3
"""
VFX Plate Exporter — single-file entry point.
"""

import sys
import os
# pymiere's core.py does `from distutils.version import StrictVersion` —
# distutils was removed from the standard library entirely in Python
# 3.12+ (this app runs on 3.13). `import setuptools` first (setuptools
# carries its own distutils compatibility shim) fixes this when running
# from source, but breaks the py2app-built app specifically: the shim's
# _distutils_hack machinery pulls in jaraco.functools, a transitive
# dependency py2app's static analysis doesn't discover/bundle, so the
# built app fails at launch with ModuleNotFoundError: No module named
# 'jaraco.functools' instead. pymiere only ever uses StrictVersion for
# a simple Premiere-Pro-version comparison, so a small self-contained
# replacement sidesteps the entire setuptools dependency chain.
import types
import re as _re
import functools as _functools


def _install_distutils_version_shim():
    if "distutils.version" in sys.modules:
        return

    @_functools.total_ordering
    class StrictVersion:
        version_re = _re.compile(r'^(\d+)\.(\d+)(\.(\d+))?([ab](\d+))?$')

        def __init__(self, vstring=None):
            self.version = ()
            self.prerelease = None
            self.vstring = vstring
            if vstring:
                self.parse(vstring)

        def parse(self, vstring):
            match = self.version_re.match(vstring)
            if not match:
                raise ValueError(f"invalid version number '{vstring}'")
            major, minor, patch, prerelease, prerelease_num = match.group(1, 2, 4, 5, 6)
            self.version = (int(major), int(minor), int(patch or 0))
            self.prerelease = (prerelease, int(prerelease_num)) if prerelease else None
            self.vstring = vstring

        def __str__(self):
            return self.vstring

        def __repr__(self):
            return f"StrictVersion('{self}')"

        def _cmp_key(self):
            return (self.version, self.prerelease is None, self.prerelease or ())

        def __eq__(self, other):
            if isinstance(other, str):
                other = StrictVersion(other)
            return self._cmp_key() == other._cmp_key()

        def __lt__(self, other):
            if isinstance(other, str):
                other = StrictVersion(other)
            return self._cmp_key() < other._cmp_key()

    distutils_mod = types.ModuleType("distutils")
    version_mod = types.ModuleType("distutils.version")
    version_mod.StrictVersion = StrictVersion
    distutils_mod.version = version_mod
    sys.modules.setdefault("distutils", distutils_mod)
    sys.modules["distutils.version"] = version_mod


_install_distutils_version_shim()

# ─── Version & update config ────────────────────────────────────────────────
VERSION_URL     = "https://raw.githubusercontent.com/esandijp-dotcom/finishing-tool/main/version.json"
DOWNLOAD_URL    = "https://raw.githubusercontent.com/esandijp-dotcom/finishing-tool/main/main.py"

def _load_local_version():
    """Read version from version.json next to this script."""
    try:
        import json as _json
        _vpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "version.json")
        with open(_vpath) as _f:
            return _json.load(_f).get("version", "1.0")
    except Exception:
        return "1.0"

APP_VERSION = _load_local_version()
# ─────────────────────────────────────────────────────────────────────────────
import re
import subprocess
import threading
import time
import glob
import tempfile
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional
from collections import defaultdict
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

os.environ["PATH"] += os.pathsep + "/opt/homebrew/bin"
try:
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = "/opt/homebrew/bin/tesseract"
except ImportError:
    pass

# ═══════════════════════════════════════════════════════════════════════════════
# RESOLVE CONNECTOR
# ═══════════════════════════════════════════════════════════════════════════════

def get_resolve():
    paths = [
        "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules",
        os.path.expanduser("~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules"),
    ]
    for path in paths:
        if os.path.exists(path) and path not in sys.path:
            sys.path.append(path)
    try:
        import DaVinciResolveScript as dvr
        resolve = dvr.scriptapp("Resolve")
        if resolve is None:
            raise ConnectionError("Could not connect to DaVinci Resolve. Make sure it is open.")
        return resolve
    except ImportError:
        raise ImportError("DaVinci Resolve scripting module not found.")

def get_current_project(resolve):
    pm = resolve.GetProjectManager()
    project = pm.GetCurrentProject()
    if not project:
        raise RuntimeError("No project is currently open in DaVinci Resolve.")
    return project

def get_current_timeline(project):
    timeline = project.GetCurrentTimeline()
    if not timeline:
        raise RuntimeError("No timeline is currently open.")
    return timeline

def get_timeline_info(timeline):
    return {
        "name": timeline.GetName(),
        "frame_rate": timeline.GetSetting("timelineFrameRate"),
        "start_frame": timeline.GetStartFrame(),
        "end_frame": timeline.GetEndFrame(),
    }

def get_all_clips_on_track(timeline, track_type, track_index):
    clips = timeline.GetItemListInTrack(track_type, track_index)
    return clips or []

def get_track_count(timeline, track_type="video"):
    return timeline.GetTrackCount(track_type)

def is_clip_orange(clip):
    """Returns True for Orange and Apricot clips only. Chocolate and others are skipped."""
    return clip.GetClipColor() in ("Orange", "Apricot")

def is_clip_apricot(clip):
    return clip.GetClipColor() == "Apricot"

def is_clip_enabled(clip):
    return clip.GetProperty("Enabled") != "0"

def get_clip_start_frame(clip):
    return clip.GetStart()

def get_clip_end_frame(clip):
    return clip.GetEnd()

def get_offline_reference_path(project, show_code=""):
    """
    Find the offline reference video from the media pool by matching:
    1. Contains 'REF' in the name
    2. Contains the show code if available
    3. Duration closest to the current timeline duration
    Falls back to just REF + duration if show code doesn't match.
    """
    try:
        timeline = project.GetCurrentTimeline()
        if not timeline:
            return None

        # Get timeline duration in seconds
        fps = float(timeline.GetSetting("timelineFrameRate") or 24)
        tl_duration = (timeline.GetEndFrame() - timeline.GetStartFrame()) / fps

        # Search entire media pool
        media_pool = project.GetMediaPool()
        root = media_pool.GetRootFolder()

        candidates = []

        def search_folder(folder):
            clips = folder.GetClipList()
            if clips:
                for clip in clips:
                    props = clip.GetClipProperty()
                    name = props.get("Clip Name", "").upper()
                    file_path = props.get("File Path", "")
                    duration_str = props.get("Duration", "")

                    if not file_path:
                        continue
                    if not any(ext in file_path.lower() for ext in [".mov", ".mp4", ".mxf"]):
                        continue
                    if "REF" not in name:
                        continue

                    # Parse duration (format: HH:MM:SS:FF or frames)
                    clip_duration = None
                    try:
                        if ":" in duration_str:
                            parts = duration_str.split(":")
                            h, m, s, f = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
                            clip_duration = h*3600 + m*60 + s + f/fps
                        else:
                            clip_duration = float(duration_str) / fps
                    except Exception:
                        clip_duration = None

                    duration_diff = abs(clip_duration - tl_duration) if clip_duration else 9999
                    has_show_code = show_code.upper() in name if show_code else False

                    candidates.append({
                        "path": file_path,
                        "name": name,
                        "duration_diff": duration_diff,
                        "has_show_code": has_show_code,
                    })

            subfolders = folder.GetSubFolderList()
            if subfolders:
                for sub in subfolders:
                    search_folder(sub)

        search_folder(root)

        # Also search clips used directly in the current timeline
        try:
            track_count = timeline.GetTrackCount("video")
            for t in range(1, track_count + 1):
                clips = timeline.GetItemListInTrack("video", t)
                if not clips:
                    continue
                for clip in clips:
                    media = clip.GetMediaPoolItem()
                    if not media:
                        continue
                    props = media.GetClipProperty()
                    name = props.get("Clip Name", "").upper()
                    file_path = props.get("File Path", "")
                    duration_str = props.get("Duration", "")

                    if not file_path:
                        continue
                    if not any(ext in file_path.lower() for ext in [".mov", ".mp4", ".mxf"]):
                        continue
                    if "REF" not in name:
                        continue

                    # Avoid duplicates
                    if any(c["path"] == file_path for c in candidates):
                        continue

                    clip_duration = None
                    try:
                        if ":" in duration_str:
                            parts = duration_str.split(":")
                            h, m, s, f = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
                            clip_duration = h*3600 + m*60 + s + f/fps
                        else:
                            clip_duration = float(duration_str) / fps
                    except Exception:
                        clip_duration = None

                    duration_diff = abs(clip_duration - tl_duration) if clip_duration else 9999
                    has_show_code = show_code.upper() in name if show_code else False

                    candidates.append({
                        "path": file_path,
                        "name": name,
                        "duration_diff": duration_diff,
                        "has_show_code": has_show_code,
                    })
        except Exception:
            pass

        if not candidates:
            return None

        # Extract YYMMDD date from filename if present
        import re as _re
        for c in candidates:
            m = _re.search('[0-9]{6}', c["name"])
            c["date"] = int(m.group(0)) if m else 0

        # Check if timeline name contains "VFX"
        tl_name = timeline.GetName().upper()
        tl_has_vfx = "VFX" in tl_name

        # Score each candidate - all criteria must match for best score
        def score(c):
            has_show = c["has_show_code"]
            has_vfx = "VFX" in c["name"] if tl_has_vfx else True
            close_duration = c["duration_diff"] < 60  # within 60 seconds
            # Higher score = better match. All three criteria ideally True.
            match_score = -(int(has_show) + int(has_vfx) + int(close_duration))
            return (match_score, -c["date"], c["duration_diff"])

        candidates.sort(key=score)
        best_path = candidates[0]["path"]
        # Only return if file actually exists on disk
        if os.path.exists(best_path):
            return best_path
        return None

    except Exception:
        return None

def frames_to_davinci_tc(frame_number, frame_rate, timeline_start_frame, start_timecode_str):
    parts = start_timecode_str.split(":")
    sh, sm, ss, sf = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
    fps = round(float(frame_rate))
    start_tc_frames = ((sh * 3600 + sm * 60 + ss) * fps) + sf
    offset = frame_number - timeline_start_frame
    total = start_tc_frames + offset
    ff = total % fps
    total_secs = total // fps
    hh = total_secs // 3600
    mm = (total_secs % 3600) // 60
    ss2 = total_secs % 60
    return f"{hh:02d}:{mm:02d}:{ss2:02d}:{ff:02d}"

def timecode_str_to_frames(timecode, frame_rate):
    fps = round(float(frame_rate))
    tc = str(timecode).strip()
    if ":" in tc:
        parts = tc.split(":")
        hh = int(parts[0])
        mm = int(parts[1]) if len(parts) > 1 else 0
        ss = int(parts[2]) if len(parts) > 2 else 0
        ff = int(parts[3]) if len(parts) > 3 else 0
    else:
        hh = int(tc)
        mm, ss, ff = 59, 50, 0
    return int((hh * 3600 + mm * 60 + ss) * fps + ff)

def clear_render_queue(project):
    project.DeleteAllRenderJobs()

# ═══════════════════════════════════════════════════════════════════════════════
# EPISODE DETECTOR
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class EpisodeMarker:
    episode_code: str
    frame_number: int
    timecode: str

def check_dependencies():
    missing = []
    try:
        import cv2
    except ImportError:
        missing.append("opencv-python")
    try:
        import pytesseract
    except ImportError:
        missing.append("pytesseract")
    result = subprocess.run(["which", "tesseract"], capture_output=True)
    if result.returncode != 0:
        missing.append("tesseract (brew install tesseract)")
    return missing

def extract_episode_code(frame):
    import cv2
    import pytesseract

    # Crop to center 40% of frame - episode title cards are almost always centered
    h, w = frame.shape[:2]
    y1 = int(h * 0.30)
    y2 = int(h * 0.70)
    x1 = int(w * 0.10)
    x2 = int(w * 0.90)
    cropped = frame[y1:y2, x1:x2]

    gray = cv2.cvtColor(cropped, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 128, 255, cv2.THRESH_BINARY)
    ch, cw = thresh.shape
    enlarged = cv2.resize(thresh, (cw * 2, ch * 2), interpolation=cv2.INTER_CUBIC)
    config = "--psm 6"
    text = pytesseract.image_to_string(enlarged, config=config).upper().strip()
    match = re.search('EPISODE[ \t]+([0-9]+[A-Z]?)', text)
    if match:
        ep_raw = match.group(1)
        num_match = re.match('([0-9]+)([A-Z]?)', ep_raw)
        if num_match:
            num = int(num_match.group(1))
            letter = num_match.group(2)
            return f"EP{num:02d}{letter}"
    return None

# ═══════════════════════════════════════════════════════════════════════════════
# PLATE ORGANIZER
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class PlateLayer:
    clip: object
    track_index: int
    start_frame: int
    end_frame: int
    layer_number: int
    sub_index: Optional[int] = None
    filename: str = ""

@dataclass
class VFXPlate:
    anchor_clip: object
    start_frame: int
    end_frame: int
    episode_code: str = ""
    plate_number: int = 0
    filename: str = ""
    layers: list = field(default_factory=list)

def get_vfx_plates(timeline):
    """
    Collect all orange/apricot clips from all tracks (2+).
    Group overlapping clips into plates.
    The lowest-track clip in each group is the anchor.
    """
    track_count = get_track_count(timeline, "video")

    # Collect all orange clips from all tracks
    clips_by_track = {}
    for track_idx in range(2, track_count + 1):
        clips = get_all_clips_on_track(timeline, "video", track_idx)
        vfx = [c for c in clips if is_clip_orange(c) and is_clip_enabled(c)]
        if vfx:
            clips_by_track[track_idx] = sorted(vfx, key=lambda c: get_clip_start_frame(c))

    if not clips_by_track:
        return []

    # Build a flat list of all clips with their track
    all_clips = []
    for track_idx in sorted(clips_by_track.keys()):
        for clip in clips_by_track[track_idx]:
            all_clips.append((track_idx, clip))
    all_clips.sort(key=lambda x: (get_clip_start_frame(x[1]), x[0]))

    # Group clips that overlap in time into plates
    # Anchor = lowest-track clip; all others are layers above it
    plates = []
    used = set()

    for i, (anchor_track, anchor_clip) in enumerate(all_clips):
        if id(anchor_clip) in used:
            continue
        used.add(id(anchor_clip))

        a_start = get_clip_start_frame(anchor_clip)
        a_end = get_clip_end_frame(anchor_clip)
        plate = VFXPlate(anchor_clip=anchor_clip,
                         start_frame=a_start, end_frame=a_end)

        # Find all clips on higher tracks that overlap with this anchor
        overlapping = []
        for track_idx, clip in all_clips:
            if id(clip) in used:
                continue
            if track_idx <= anchor_track:
                continue
            cs = get_clip_start_frame(clip)
            ce = get_clip_end_frame(clip)
            if cs >= a_start and ce <= a_end:
                overlapping.append((track_idx, clip))
                used.add(id(clip))

        if overlapping:
            # Count total orange (non-apricot) clips in this stack
            all_in_stack = [(anchor_track, anchor_clip)] + overlapping
            total_orange = sum(1 for _, c in all_in_stack if not is_clip_apricot(c))
            use_v_numbers = total_orange >= 2
            orange_counter = 0

            # Add anchor as first layer
            if is_clip_apricot(anchor_clip):
                plate.layers.append(PlateLayer(
                    clip=anchor_clip, track_index=anchor_track,
                    start_frame=a_start, end_frame=a_end,
                    layer_number=0, sub_index=None
                ))
            else:
                orange_counter += 1
                plate.layers.append(PlateLayer(
                    clip=anchor_clip, track_index=anchor_track,
                    start_frame=a_start, end_frame=a_end,
                    layer_number=orange_counter if use_v_numbers else -1,
                    sub_index=None
                ))

            # Add overlapping clips as layers, grouped by track
            track_clips = defaultdict(list)
            for track_idx, c in overlapping:
                track_clips[track_idx].append(c)

            for track_idx in sorted(track_clips.keys()):
                clips_on_track = track_clips[track_idx]
                for clip in clips_on_track:
                    if is_clip_apricot(clip):
                        plate.layers.append(PlateLayer(
                            clip=clip, track_index=track_idx,
                            start_frame=get_clip_start_frame(clip),
                            end_frame=get_clip_end_frame(clip),
                            layer_number=0, sub_index=None
                        ))
                    else:
                        orange_counter += 1
                        plate.layers.append(PlateLayer(
                            clip=clip, track_index=track_idx,
                            start_frame=get_clip_start_frame(clip),
                            end_frame=get_clip_end_frame(clip),
                            layer_number=orange_counter if use_v_numbers else -1,
                            sub_index=clips_on_track.index(clip) + 1 if len(clips_on_track) > 1 else None
                        ))

        plates.append(plate)

    plates.sort(key=lambda p: p.start_frame)
    return plates

def assign_episodes(plates, episode_markers):
    if not episode_markers:
        for plate in plates:
            plate.episode_code = "EP00"
        return

    # Sort markers by frame number to ensure correct order
    sorted_markers = sorted(episode_markers, key=lambda m: m.frame_number)

    for plate in plates:
        assigned = sorted_markers[0].episode_code
        for marker in sorted_markers:
            if plate.start_frame >= marker.frame_number:
                assigned = marker.episode_code
            # Don't break early - check all markers to find the last one that applies
        plate.episode_code = assigned

    counters = {}
    for plate in sorted(plates, key=lambda p: p.start_frame):
        ep = plate.episode_code
        counters[ep] = counters.get(ep, 0) + 1
        plate.plate_number = counters[ep]

def build_filename(show_code, show_acronym, date, episode_code, plate_number, layer=None):
    base = f"{show_code}_{show_acronym}_VFX_{episode_code}_{date}_{plate_number:02d}"
    if layer is None:
        return base
    if layer.layer_number == 0:
        return base + "_CLEAN"
    if layer.layer_number == -1:
        return base  # single orange with only CLEAN above — no V# suffix
    suffix = f"_V{layer.layer_number}"
    if layer.sub_index is not None:
        suffix += f"_{layer.sub_index:02d}"
    return base + suffix

def assign_filenames(plates, show_code, show_acronym, date):
    for plate in plates:
        if not plate.layers:
            clean = is_clip_apricot(plate.anchor_clip)
            plate.filename = build_filename(
                show_code, show_acronym, date, plate.episode_code, plate.plate_number,
                layer=PlateLayer(clip=None, track_index=2, start_frame=0, end_frame=0,
                                 layer_number=0) if clean else None)
        else:
            for layer in plate.layers:
                layer.filename = build_filename(
                    show_code, show_acronym, date, plate.episode_code, plate.plate_number, layer)

def get_export_list(plates):
    exports = []
    for plate in plates:
        if not plate.layers:
            exports.append({
                "clip": plate.anchor_clip,
                "track_index": 2,
                "filename": plate.filename,
                "episode_code": plate.episode_code,
                "plate_number": plate.plate_number,
                "start_frame": plate.start_frame,
                "end_frame": plate.end_frame,
            })
        else:
            for layer in plate.layers:
                exports.append({
                    "clip": layer.clip,
                    "track_index": layer.track_index,
                    "filename": layer.filename,
                    "episode_code": plate.episode_code,
                    "plate_number": plate.plate_number,
                    "start_frame": layer.start_frame,
                    "end_frame": layer.end_frame,
                })
    return exports

# ═══════════════════════════════════════════════════════════════════════════════
# RENDER
# ═══════════════════════════════════════════════════════════════════════════════

RENDER_PRESET = "02_COLORED VFX 4444 XQ"

def find_output_folder(show_code, show_acronym=None, log_callback=None):
    """
    Auto-detect output folder: any mounted volume whose name CONTAINS the
    show code, searched (up to a bounded depth) for the first folder
    anywhere inside it with "TO VFX" in its name. Deliberately permissive
    by request — the show code appearing in the volume name is the only
    requirement now. Earlier versions also required an exact
    SHOWCODE_*_EDIT -> *TURNOVER* -> *TO VFX* folder chain, which
    silently failed to match any structure that didn't follow it exactly
    — including a volume prefixed with "V-"/"I-" before the show code,
    the specific bug this was originally built to fix, and any other
    naming/organization variant a given show happens to use.
    show_acronym is accepted but unused — kept for call-site compatibility.
    log_callback, if given, is called with a line for what was searched
    and what matched, so a failed auto-detect is debuggable instead of
    just "not found".
    """
    import glob, os

    def _log(msg):
        if log_callback:
            log_callback(msg)

    if not show_code:
        _log("Output folder auto-detect: no show code set yet, skipping.")
        return None

    volumes = [v for v in glob.glob("/Volumes/*") if show_code in os.path.basename(v)]
    _log(f"Output folder auto-detect: show code \"{show_code}\" -> "
         f"{len(volumes)} volume(s) matched" + (f" ({', '.join(sorted(volumes))})" if volumes else ""))
    if not volumes:
        return None

    MAX_DEPTH = 6
    for volume in sorted(volumes):
        base_depth = volume.rstrip(os.sep).count(os.sep)
        found = False
        for root, dirs, _files in os.walk(volume):
            depth = root.rstrip(os.sep).count(os.sep) - base_depth
            if depth >= MAX_DEPTH:
                dirs[:] = []
                continue
            for d in dirs:
                if "TO VFX" in d.upper():
                    found = True
                    return os.path.join(root, d)
        if not found:
            _log(f"  {volume}: no folder with \"TO VFX\" in its name found "
                 f"(searched up to {MAX_DEPTH} levels deep).")

    return None


def generate_plate_list_xlsx(export_list, output_dir, show_code, acronym, date_str,
                              shot_map=None, list_folder=None, log_callback=None,
                              timeline=None, **kwargs):
    """Generate plate list xlsx using pre-captured screenshots from shot_map."""
    import xlsxwriter, os

    if shot_map is None:
        shot_map = {}
    if list_folder is None:
        list_folder = os.path.join(output_dir, "LIST TO POST")
    os.makedirs(list_folder, exist_ok=True)

    filename = f"{show_code}_{acronym}_VFX_LIST TO POST_{date_str}.xlsx"
    out_path = os.path.join(list_folder, filename)

    # FPS and start TC for timecode display
    fps = 24.0
    start_tc_offset = "00:00:00:00"
    if timeline:
        try:
            fps = float(timeline.GetSetting("timelineFrameRate") or 24)
            start_tc_offset = timeline.GetStartTimecode() or "00:00:00:00"
        except Exception:
            pass

    def frames_to_tc(frame_num):
        try:
            parts = start_tc_offset.split(":")
            offset = (int(parts[0])*3600 + int(parts[1])*60 + int(parts[2])) * int(fps) + int(parts[3])
            total = frame_num
        except Exception:
            total = frame_num
        fi = int(fps)
        ff = total % fi; total //= fi
        ss = total % 60; total //= 60
        mm = total % 60; hh = total // 60
        return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"

    # Group export_list: one row per unique plate (V# layers share same start/end)
    seen = {}
    for item in export_list:
        key = (item["episode_code"], item["start_frame"], item["end_frame"])
        if key not in seen:
            seen[key] = {"item": item, "layers": 1}
        else:
            seen[key]["layers"] += 1
    unique_plates = sorted(seen.values(), key=lambda x: x["item"]["start_frame"])

    CELL_H_PX = 190
    ROW_H_PT  = CELL_H_PX * 72 / 96
    _tmp_files = []

    # Create workbook with XlsxWriter
    wb = xlsxwriter.Workbook(out_path)
    ws = wb.add_worksheet("Plate List")

    # Formats
    header_fmt = wb.add_format({
        'bold': True, 'font_name': 'Arial', 'font_size': 10,
        'font_color': '#E8A838', 'bg_color': '#1E1E1E',
        'align': 'center', 'valign': 'vcenter',
        'border': 1, 'border_color': '#333333'
    })
    cell_fmt = wb.add_format({
        'font_name': 'Arial', 'font_size': 10,
        'align': 'center', 'valign': 'vcenter',
        'text_wrap': True,
        'border': 1, 'border_color': '#333333'
    })

    columns   = ["FILE NAME", "EPISODE", "PLATES TO TURNOVER",
                 "VFX REF TIMECODE_IN", "VFX REF TIMECODE_OUT",
                 "TURNOVER STATUS", "TURNOVER NOTE", "VFX", "VFX NOTE", "SCREENSHOT"]
    col_widths = [45, 12, 20, 22, 22, 18, 20, 12, 20, 25]

    # Header row
    for col_idx, (col_name, col_w) in enumerate(zip(columns, col_widths)):
        ws.write(0, col_idx, col_name, header_fmt)
        ws.set_column(col_idx, col_idx, col_w)
    ws.set_row(0, 18)

    # Data rows
    for row_idx, plate_data in enumerate(unique_plates, 1):
        item   = plate_data["item"]
        layers = plate_data["layers"]

        tc_in  = frames_to_tc(item["start_frame"])
        tc_out = frames_to_tc(item["end_frame"])

        values = [
            item["filename"] + ".mov",
            item["episode_code"],
            layers,
            tc_in,
            tc_out,
            "", "", "", "", ""
        ]

        ws.set_row(row_idx, ROW_H_PT)

        for col_idx, val in enumerate(values):
            ws.write(row_idx, col_idx, val, cell_fmt)

        # Screenshot — embed directly into cell
        shot_data = shot_map.get(item["filename"])
        if shot_data:
            shot_path, img_w, img_h = shot_data if isinstance(shot_data, tuple) else (shot_data, 160, 90)
            if os.path.exists(shot_path):
                try:
                    # Scale to fit cell height, maintain aspect ratio, center horizontally
                    # Cell: ~175px wide x 190px tall. Image: 720x1280
                    scale = CELL_H_PX / 1280.0
                    scaled_w = 720 * scale  # ~106px
                    CELL_W_PX = 175
                    x_offset = max(0, int((CELL_W_PX - scaled_w) / 2))
                    ws.insert_image(row_idx, 9, shot_path, {
                        'x_scale': scale,
                        'y_scale': scale,
                        'x_offset': x_offset,
                        'y_offset': 2,
                        'positioning': 1
                    })
                except Exception as e:
                    if log_callback:
                        log_callback(f"  ⚠ Screenshot embed failed: {e}")

        if log_callback:
            log_callback(f"  [{row_idx}/{len(unique_plates)}] {item['filename']}")

    wb.close()

    if log_callback:
        log_callback(f"✓ Plate list saved: {filename}")

    return out_path


def render_single_clip(project, timeline, render_preset,
                       output_path, filename, mark_in, mark_out, log_callback=None):
    """Render one clip — tracks must already be set correctly before calling."""
    project.LoadRenderPreset(render_preset)
    project.SetRenderSettings({
        "SelectAllFrames": False,
        "MarkIn": mark_in,
        "MarkOut": mark_out,
        "TargetDir": output_path,
        "CustomName": filename,
        "UniqueFilenameStyle": 0,
        "ExportVideo": True,
        "ExportAudio": False,
    })

    clear_render_queue(project)
    job_id = project.AddRenderJob()

    if not job_id:
        if log_callback:
            log_callback(f"    Failed to add render job for {filename}")
        return False

    if log_callback:
        jobs = project.GetRenderJobList()
        for job in jobs:
            if job.get("JobId") == job_id:
                log_callback(f"    MarkIn={job.get('MarkIn')}, MarkOut={job.get('MarkOut')}")

    project.StartRendering(job_id)

    timeout = 600
    elapsed = 0
    while elapsed < timeout:
        time.sleep(1)
        elapsed += 1
        status = project.GetRenderJobStatus(job_id)
        job_status = status.get("JobStatus", "")
        if job_status == "Complete":
            break
        elif job_status == "Failed":
            if log_callback:
                log_callback(f"    Render failed: {status}")
            clear_render_queue(project)
            return False

    clear_render_queue(project)
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# EXPORT ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

class ExportEngine:
    def __init__(self, log_callback=None, progress_callback=None):
        self.log = log_callback or print
        self.progress = progress_callback or (lambda pct, msg: None)
        self.resolve = None
        self.project = None
        self.timeline = None
        self.episode_markers = []
        self.export_list = []

    def connect(self):
        self.log("Connecting to DaVinci Resolve...")
        self.resolve = get_resolve()
        self.project = get_current_project(self.resolve)
        self.timeline = get_current_timeline(self.project)
        info = get_timeline_info(self.timeline)
        self.log(f"Connected. Timeline: {info['name']} @ {info['frame_rate']} fps")

        parts = info["name"].split("_")
        info["show_code"] = parts[0] if len(parts) > 0 else ""
        info["show_acronym"] = parts[1] if len(parts) > 1 else ""

        start_tc = self.timeline.GetStartTimecode()
        info["start_timecode"] = start_tc
        info["start_frame"] = self.timeline.GetStartFrame()
        info["start_hour"] = int(start_tc.split(":")[0]) if start_tc else 3
        self.log(f"Timeline start timecode: {start_tc}")

        return info

    def scan_episodes(self, reference_video_path=None, start_timecode="03:59:50:00", stop_flag=None):
        import cv2, time

        if not reference_video_path:
            self.log("Auto-detecting reference video from project...")
            reference_video_path = get_offline_reference_path(self.project)
        if not reference_video_path or not os.path.exists(reference_video_path):
            raise FileNotFoundError("Could not find reference video. Please select it manually.")

        self.log(f"Reference video: {os.path.basename(reference_video_path)}")

        info = get_timeline_info(self.timeline)
        frame_rate = float(info["frame_rate"])

        self.log("Finding gaps across all tracks...")
        track_count = get_track_count(self.timeline, "video")
        all_ranges = []
        for t in range(1, track_count + 1):
            clips = get_all_clips_on_track(self.timeline, "video", t)
            for c in clips:
                all_ranges.append((get_clip_start_frame(c), get_clip_end_frame(c)))

        if not all_ranges:
            self.log("No clips found in timeline.")
            self.episode_markers = []
            return self.episode_markers

        all_ranges.sort(key=lambda x: x[0])
        merged = [list(all_ranges[0])]
        for start, end in all_ranges[1:]:
            if start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])

        gaps = []
        timeline_start = self.timeline.GetStartFrame()
        if merged[0][0] > timeline_start:
            gap_len = merged[0][0] - timeline_start
            if gap_len >= 20:
                gaps.append((timeline_start, merged[0][0]))
        for i in range(len(merged) - 1):
            gap_start = merged[i][1]
            gap_end = merged[i + 1][0]
            if gap_end - gap_start >= 20:
                gaps.append((gap_start, gap_end))

        self.log(f"Found {len(gaps)} gaps ≥ 20 frames in timeline")

        if not gaps:
            self.log("No gaps found.")
            self.episode_markers = []
            return self.episode_markers

        # Step 1: Get timecodes from DaVinci for each gap
        self.log("Getting timecodes from DaVinci...")
        gap_timecodes = []
        for i, (gap_start, gap_end) in enumerate(gaps):
            if stop_flag and stop_flag():
                self.log("Scan stopped by user.")
                cap_ref = None
                break
            pct = (i / len(gaps)) * 40
            self.progress(pct, f"Getting timecode {i+1}/{len(gaps)}...")

            if i == 0:
                mid_frame = max(gap_end - 24, gap_start + 1)
            else:
                mid_frame = min(((gap_start + gap_end) // 2) + 10, gap_end - 1)

            target_tc = frames_to_davinci_tc(
                mid_frame, frame_rate,
                self.timeline.GetStartFrame(),
                self.timeline.GetStartTimecode()
            )
            self.timeline.SetCurrentTimecode(target_tc)
            confirmed_tc = self.timeline.GetCurrentTimecode()
            gap_timecodes.append((confirmed_tc, gaps[i][0]))
            self.log(f"  Gap {i+1}: {confirmed_tc}")

        self.resolve.OpenPage("edit")
        self.log("Returned to Edit page. Now scanning reference video...")

        # Step 2: Seek into reference video and OCR
        video_start_frames = timecode_str_to_frames(start_timecode, frame_rate)

        cap = cv2.VideoCapture(reference_video_path)
        if not cap.isOpened():
            raise RuntimeError(f"Could not open reference video: {reference_video_path}")

        markers = []
        last_episode = None

        for i, (tc_str, gap_start_frame) in enumerate(gap_timecodes):
            if stop_flag and stop_flag():
                self.log("Scan stopped by user.")
                break
            pct = 40 + (i / len(gap_timecodes)) * 50
            self.progress(pct, f"OCR on gap {i+1}/{len(gap_timecodes)}...")

            parts = tc_str.split(":")
            hh, mm, ss, ff = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
            fps = round(float(frame_rate))
            tc_total_frames = ((hh * 3600 + mm * 60 + ss) * fps) + ff
            file_frame = tc_total_frames - video_start_frames

            if file_frame < 0:
                continue

            cap.set(cv2.CAP_PROP_POS_FRAMES, file_frame)
            ret, frame = cap.read()
            if not ret or frame is None:
                self.log(f"  Gap {i+1}: could not read frame")
                continue

            code = extract_episode_code(frame)
            self.log(f"  Gap {i+1} at {tc_str}: OCR={repr(code)}")

            if code and code != last_episode:
                markers.append(EpisodeMarker(
                    episode_code=code,
                    frame_number=gap_start_frame,
                    timecode=tc_str
                ))
                last_episode = code
                self.log(f"  ✓ Found {code} at {tc_str}")

        cap.release()

        self.episode_markers = markers
        self.progress(95, "Scan complete.")
        self.log(f"Found {len(self.episode_markers)} episodes:")
        for m in self.episode_markers:
            self.log(f"  {m.episode_code} → {m.timecode}")
        return self.episode_markers

    def prepare_export(self, show_code, show_acronym, date):
        self.log("Scanning for orange VFX plates...")
        self.progress(60, "Scanning for VFX plates...")
        plates = get_vfx_plates(self.timeline)
        self.log(f"Found {len(plates)} VFX plates")
        for p in plates:
            if p.layers:
                self.log(f"  Plate at {p.start_frame}-{p.end_frame}: {len(p.layers)} layers: {[l.layer_number for l in p.layers]}")
            else:
                self.log(f"  Plate at {p.start_frame}-{p.end_frame}: no layers")
        self.progress(70, "Assigning episode codes...")
        assign_episodes(plates, self.episode_markers)
        self.progress(80, "Building filenames...")
        assign_filenames(plates, show_code, show_acronym, date)
        self.export_list = get_export_list(plates)
        self.log(f"\nReady to export {len(self.export_list)} clips:")
        for item in self.export_list:
            self.log(f"  {item['filename']}")
        return self.export_list

    def run_export(self, output_dir, stop_flag=None, start_index=0, screenshot_callback=None):
        if not self.export_list:
            raise RuntimeError("No clips to export. Run prepare_export first.")
        os.makedirs(output_dir, exist_ok=True)

        # Create episode folders and 01_ARCHIVE subfolders
        episodes = set(item["episode_code"] for item in self.export_list)
        for ep in sorted(episodes):
            ep_folder = os.path.join(output_dir, ep)
            os.makedirs(ep_folder, exist_ok=True)
            os.makedirs(os.path.join(ep_folder, "01_ARCHIVE"), exist_ok=True)
            self.log(f"  Created folder: {ep}/")

        total = len(self.export_list)
        track_count = self.timeline.GetTrackCount("video")
        self.log(f"\nExporting {total} clips track by track...")

        # Group clips by track, preserving order
        from collections import defaultdict
        clips_by_track = defaultdict(list)
        for item in self.export_list:
            clips_by_track[item["track_index"]].append(item)

        done = start_index  # count already-done clips in progress

        # Filter export list to only clips from start_index onward
        remaining = self.export_list[start_index:]
        clips_by_track = defaultdict(list)
        for item in remaining:
            clips_by_track[item["track_index"]].append(item)

        for track_index in sorted(clips_by_track.keys()):
            track_clips = clips_by_track[track_index]
            self.log(f"\n── Track {track_index}: {len(track_clips)} clips ──")

            # ── Edit page: mute all tracks except this one ─────────────────
            self.resolve.OpenPage("edit")
            time.sleep(1.5)

            for t in range(1, track_count + 1):
                self.timeline.SetTrackEnable("video", t, t == track_index)
            time.sleep(1)

            active = [t for t in range(1, track_count + 1)
                     if self.timeline.GetIsTrackEnabled("video", t)]
            self.log(f"  Active tracks: {active}")

            # ── Deliver page: render all clips on this track ───────────────
            self.resolve.OpenPage("deliver")
            time.sleep(1.5)

            for item in track_clips:
                if stop_flag and stop_flag():
                    self.log("Export stopped by user.")
                    # Clean up DaVinci state so it's ready for continue
                    clear_render_queue(self.project)
                    self.resolve.OpenPage("edit")
                    time.sleep(0.5)
                    for t in range(1, track_count + 1):
                        self.timeline.SetTrackEnable("video", t, True)
                    return False

                done += 1
                pct = (done / total) * 100
                track_num = item.get('track_index', '?')
                self.progress(pct, f"Exporting Plate: {done}/{total} · {item['episode_code']} · Track {track_num}")
                self.log(f"  [{done}/{total}] {item['filename']} · Track {track_num}")

                # Grab screenshot before rendering
                if screenshot_callback:
                    screenshot_callback(item)

                ep_folder = os.path.join(output_dir, item["episode_code"])
                success = render_single_clip(
                    project=self.project,
                    timeline=self.timeline,
                    render_preset=RENDER_PRESET,
                    output_path=ep_folder,
                    filename=item["filename"],
                    mark_in=item["start_frame"],
                    mark_out=item["end_frame"] - 1,
                    log_callback=self.log
                )

                if not success:
                    self.log(f"  ✗ Failed: {item['filename']}")
                else:
                    self.log(f"  ✓ Done: {item['filename']}")
                    # Only update _last_done after confirmed success
                    self._last_done = done

        # ── Restore all tracks ─────────────────────────────────────────────
        self.resolve.OpenPage("edit")
        time.sleep(1)
        self.log("\nRestoring all tracks...")
        for t in range(1, track_count + 1):
            self.timeline.SetTrackEnable("video", t, True)

        self.progress(100, "Export complete!")
        self.log(f"\nAll {total} clips exported to: {output_dir}")
        return True

# ═══════════════════════════════════════════════════════════════════════════════
# UI
# ═══════════════════════════════════════════════════════════════════════════════

# Fixed window width used by every resize call across both tabs. Sized so
# the Export Queue reliably fits 17 chips per row even in the worst case:
# 16 regular "EPxx" chips plus the wider TRAILER chip sharing the same row,
# WITH the scrollbar visible (which itself eats into the row's available
# width) — verified live via a scratch measurement script, not guessed.
APP_WIDTH    = 1501
BG_DARK      = "#1E1E1E"
BG_OUTER     = "#161616"  # slightly darker for outer app background
BG_PANEL     = "#2A2A2A"
BG_INPUT     = "#333333"
ACCENT       = "#E8A838"
ACCENT_HOVER = "#F0BC5A"
TEXT_PRIMARY = "#F0F0F0"
TEXT_MUTED   = "#888888"
TEXT_SUCCESS = "#5DBD74"
TEXT_ERROR   = "#E05555"
TEXT_WARN    = "#E8A838"
BORDER       = "#3A3A3A"
FONT_MAIN    = ("SF Pro Display", 13)
FONT_SMALL   = ("SF Pro Display", 11)
FONT_MONO    = ("SF Mono", 11)
FONT_TITLE   = ("SF Pro Display", 22, "bold")
FONT_LABEL   = ("SF Pro Display", 12)


class VFXExporterApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Finishing Tool")
        self.configure(bg=BG_OUTER)
        try:
            from ctypes import windll
        except ImportError:
            pass
        # Fix Retina/HiDPI scaling when launched as .app bundle
        try:
            self.tk.call("tk", "scaling", 1.0)
        except Exception:
            pass
        self.resizable(False, False)
        self.attributes("-alpha", 0)
        self.withdraw()
        self._programmatic_resize = False

        self.engine = None
        self.episode_markers = []
        self.export_list = []
        self.reference_video_path = tk.StringVar()
        self.reference_video_path.trace_add("write", lambda *a: self._on_ref_video_change())
        self.output_dir = tk.StringVar(value="")
        self.output_dir.trace_add("write", lambda *a: self._on_output_dir_change())
        self.show_code = tk.StringVar()
        self.show_acronym = tk.StringVar()
        self.export_date = tk.StringVar(value=datetime.now().strftime("%y%m%d"))
        self.start_timecode = tk.StringVar(value="")
        self.start_timecode_display = tk.StringVar(value="Auto-detected on connect")
        self._stop_export = False
        self._disabled_episodes = set()
        self._export_started = False  # episodes toggled off by user
        self._vfx_run_complete = False  # True once an export finishes without being stopped — locks DISABLE ALL/ENABLE ALL until a fresh Scan Episodes
        self._stop_scan = False
        self._scanning = False
        self._suppress_progress = False
        self.filename_preview = tk.StringVar(value="Connect to DaVinci to auto-fill...")

        # Independent mode vars for each section
        self.show_mode = tk.StringVar(value="auto")
        self.ref_mode = tk.StringVar(value="auto")
        self.out_mode = tk.StringVar(value="auto")

        for var in [self.show_code, self.show_acronym, self.export_date]:
            var.trace_add("write", lambda *args: self.after(0, self._update_filename_preview))

        self._build_ui()
        self.after(200, self._disable_reset_btn)
        self._check_deps_on_start()
        self._pp_check_deps_on_start()
        self.after(2000, self._check_for_updates)
        self.after(1, self._center_window)
        self.after(100, self._setup_menu)

    def _center_window(self):
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        w = APP_WIDTH
        h = self.winfo_reqheight()
        if h < 100:
            h = 800
        x = (sw - w) // 2
        # Flush to the top of the screen (not vertically centered) — the
        # Episode Export tab grows much taller once chips are populated, so
        # starting as high as possible maximizes room to grow before the
        # window runs off the bottom of the screen.
        y = 20
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.deiconify()
        self.attributes("-alpha", 1)
        self.lift()
        self.focus_force()

    def _build_ui(self):
        main = tk.Frame(self, bg=BG_OUTER, padx=28, pady=24)
        main.pack(fill="both", expand=True)
        self._main_frame = main

        # ── App title + Reset button ───────────────────────────────────────
        title_row = tk.Frame(main, bg=BG_OUTER)
        title_row.pack(fill="x", pady=(0, 0))

        # Left: Finishing Tool + version
        title_lbl_frame = tk.Frame(title_row, bg=BG_OUTER)
        title_lbl_frame.pack(side="left")
        tk.Label(title_lbl_frame, text="Finishing Tool", font=FONT_TITLE,
                 bg=BG_OUTER, fg=ACCENT).pack(side="left")
        self._version_label = tk.Label(title_lbl_frame, text=f"v{APP_VERSION}", font=("SF Pro Display", 13),
                 bg=BG_OUTER, fg=TEXT_MUTED)
        self._version_label.pack(side="left", padx=(8, 0), pady=(6, 0))

        # Center: show code pill (hidden until connected)
        self._show_pill = tk.Frame(title_row, bg=BG_OUTER)
        self._show_pill.pack(side="left", expand=True)
        self._show_pill_widget = None
        # Reset button as rounded canvas
        # Reset button as canvas for color control on macOS
        def _make_reset_canvas(text, bg, fg):
            font = ("SF Pro Display", 12, "bold")
            import tkinter.font as tkfont
            f = tkfont.Font(family="SF Pro Display", size=12, weight="bold")
            tw, th = f.measure(text), f.metrics("linespace")
            w, h, r = tw + 32, th + 14, 7
            c = tk.Canvas(title_row, width=w, height=h, bg=BG_OUTER,
                          highlightthickness=0, cursor="")
            x1,y1,x2,y2 = 0,0,w,h
            pts = [x1+r,y1,x2-r,y1,x2,y1,x2,y1+r,x2,y2-r,x2,y2,
                   x2-r,y2,x1+r,y2,x1,y2,x1,y2-r,x1,y1+r,x1,y1]
            poly_id = c.create_polygon(pts, fill=bg, outline="", smooth=True)
            text_id = c.create_text(w//2, h//2, font=font, fill=fg)
            c._text = text
            c._bg = bg
            c.itemconfig(text_id, text=text)

            def draw(fill):
                c.itemconfig(poly_id, fill=fill)
                c.itemconfig(text_id, text=c._text)

            c._draw = draw
            c.bind("<Enter>", lambda e: draw("#3a8e3a" if c._bg=="#2a6e2a" else "#ae3a3a"))
            c.bind("<Leave>", lambda e: draw(c._bg))
            c.bind("<ButtonPress-1>", lambda e: draw("#1a5e1a" if c._bg=="#2a6e2a" else "#6e1a1a"))
            # ButtonRelease handled by _enable_reset_btn bindings
            pass
            c._action = lambda: None
            return c

        # ? Help button — filled orange circle
        help_size = 26
        self._help_btn = tk.Canvas(title_row, width=help_size, height=help_size,
                                    bg=BG_OUTER, highlightthickness=0, cursor="")
        self._help_btn.create_oval(1, 1, help_size-1, help_size-1,
                                    fill=ACCENT, outline="")
        self._help_btn.create_text(help_size//2, help_size//2, text="?",
                                    font=("SF Pro Display", 13, "bold"), fill="#000000")
        self._help_btn.pack(side="right", padx=(14, 0))
        self._help_btn.bind("<Enter>",  lambda e: self._help_btn.itemconfig(1, fill="#f0c060"))
        self._help_btn.bind("<Leave>",  lambda e: self._help_btn.itemconfig(1, fill=ACCENT))
        self._help_btn.bind("<ButtonPress-1>",   lambda e: self._help_btn.itemconfig(1, fill="#a06010"))
        self._help_btn.bind("<ButtonRelease-1>", lambda e: (self._help_btn.itemconfig(1, fill=ACCENT), self._open_guide()))

        self.btn_reset = _make_reset_canvas("RESET ALL", "#2a6e2a", "#FFFFFF")
        self.btn_reset.pack(side="right", padx=(0, 0))
        self.btn_reset._action = self._do_reset
        self.btn_reset._enabled = False

        tk.Label(main, text="By Juan Esandi",
                 font=FONT_SMALL, bg=BG_OUTER, fg=TEXT_MUTED).pack(anchor="w", pady=(0, 36))
        self._thinking_active = False
        self._thinking_frames = []
        self._thinking_frame_idx = 0

        # Load GIF frames
        import os as _os
        # Find GIF next to the script, with detected by clip count paths
        _script_dir = _os.path.dirname(_os.path.abspath(__file__)) if "__file__" in dir() else _os.path.expanduser("~/Downloads/vfx_exporter")
        gif_path = _os.path.join(_script_dir, "thinking.gif")
        try:
            from PIL import Image, ImageTk
            if not _os.path.exists(gif_path):
                raise FileNotFoundError(f"GIF not found: {gif_path}")
            gif = Image.open(gif_path)
            while True:
                frame = gif.copy().convert("RGB")
                self._thinking_frames.append(ImageTk.PhotoImage(frame))
                try:
                    gif.seek(gif.tell() + 1)
                except EOFError:
                    break
        except Exception:
            self._thinking_frames = []

        # ── Tab bar ────────────────────────────────────────────────────────
        self._active_tab = "vfx"
        # RESET ALL is one shared button/widget, but its enabled look is
        # conceptually per-tab (VFX and Episode Export have independent
        # progress) — tracked here so switching tabs shows the right state
        # instead of whichever tab last touched the button.
        self._reset_armed = {"vfx": False, "test": False}
        # SHOWCODE pill is one shared widget too — same per-tab tracking.
        self._show_pill_text = {"vfx": "", "test": ""}
        self._tab_canvases = {}
        tab_bar = tk.Frame(main, bg=BG_OUTER)
        tab_bar.pack(fill="x", pady=(0, 0))
        # Thinking indicator on right side of tab bar
        self._thinking_label = tk.Label(tab_bar, bg=BG_OUTER)
        self._thinking_label.pack(side="right", padx=(0, 4))
        # Status text for connect (shown instead of GIF during connect)
        self._connect_status = tk.Label(tab_bar, text="", font=FONT_SMALL,
                                         bg=BG_OUTER, fg=TEXT_MUTED)
        self._connect_status.pack(side="right", padx=(0, 8))

        def _draw_tab(canvas, label, active):
            tw, th, r = 130, 36, 10
            col = ACCENT if active else "#888888"
            bg = BG_DARK if active else "#252525"

            lw = 1.5 if active else 1
            canvas.delete("all")
            canvas.create_rectangle(0, r, tw, th+1, fill=bg, outline="")
            canvas.create_rectangle(r, 0, tw-r, r, fill=bg, outline="")
            canvas.create_arc(0, 0, r*2, r*2, start=90, extent=90, fill=bg, outline="")
            canvas.create_arc(tw-r*2, 0, tw, r*2, start=0, extent=90, fill=bg, outline="")
            if active:
                canvas.create_line(r, 1, tw-r, 1, fill=col, width=lw)
                canvas.create_arc(1, 1, r*2+1, r*2+1, start=90, extent=90,
                                  outline=col, style="arc", width=lw)
                canvas.create_arc(tw-r*2-1, 1, tw-1, r*2+1, start=0, extent=90,
                                  outline=col, style="arc", width=lw)
                canvas.create_line(1, r, 1, th+1, fill=col, width=lw)
                canvas.create_line(tw-1, r, tw-1, th+1, fill=col, width=lw)
            fw = "bold" if active else "normal"
            canvas.create_text(tw//2, th//2, text=label,
                              font=("SF Pro Display", 12, fw), fill=col)

        def _make_tab(label, tab_id, disabled=False):
            tw, th = 130, 36
            c = tk.Canvas(tab_bar, width=tw, height=th+1, bg=BG_OUTER, highlightthickness=0)
            c.pack(side="left", padx=(0, 2))
            self._tab_canvases[tab_id] = (c, label)
            _draw_tab(c, label, tab_id == self._active_tab)
            if not disabled:
                c.bind("<Button-1>", lambda e, tid=tab_id: self._switch_tab(tid))

        _make_tab("VFX EXPORT", "vfx")
        _make_tab("EPISODE EXPORT", "test")
        self._draw_tab_fn = lambda: [_draw_tab(c, l, tid == self._active_tab)
                                      for tid, (c, l) in self._tab_canvases.items()]

        # ── Tab content panel ──────────────────────────────────────────────
        self.tab_content_frame = tk.Frame(main, bg=BG_DARK,
                                           highlightthickness=1,
                                           highlightbackground="#5a5a5a",
                                           highlightcolor="#5a5a5a")
        self.tab_content_frame.pack(fill="both", expand=True, pady=(0, 8))

        self._vfx_content = tk.Frame(self.tab_content_frame, bg=BG_DARK, padx=16, pady=12)
        self._vfx_content.pack(fill="both", expand=True)
        self.tab_content = self._vfx_content
        self._build_vfx_tab(self._vfx_content)

        self._test_content = tk.Frame(self.tab_content_frame, bg=BG_DARK, padx=16, pady=12)
        self._build_episode_export_tab(self._test_content)

    def _build_episode_export_tab(self, main):
        """Build the Episode Export tab UI for Premiere Pro auto-nesting."""
        # State
        self._pp_connected       = False
        self._pp_seq             = None
        self._pp_track_idx       = None
        self._pp_track_mode      = tk.StringVar(value="auto")
        self._pp_out_mode        = tk.StringVar(value="auto")
        self._pp_tails_tc        = None     # tails clip start, in ticks (int) — scratch, for whichever reel is mid-scan
        self._pp_nesting_active = False     # True only while a nest run is actually in flight (running or paused)
        self._pp_exporting_active = False   # True only while _pp_export_task's background thread is actually running — see _pp_clear_all_exp
        self._pp_hide_overwrite_prompt = False  # "Yes to All"/"No to All" (this nesting task only) — reuse _pp_overwrite_remembered_answer from then on, see _pp_overwrite_dialog
        self._pp_overwrite_remembered_answer = True  # last Yes/No choice made alongside "Don't show again" — see _pp_overwrite_dialog
        self._pp_reels           = []       # one dict per distinct connected timeline this session — see _pp_arm_timeline
        self._pp_current_reel    = None     # index into self._pp_reels
        self._pp_show_info_locked = False   # Show Code/Acronym/Date lock in after the first timeline
        self._pp_stringout_map   = {}       # {sequence name: Sequence obj} — STRINGOUT timelines found on Connect
        self._pp_created_seqs    = []       # (name, Sequence obj, reel_idx) triples, accumulated all session
        self._pp_stop_nest       = False
        self._pp_stop_export     = False
        self._pp_nest_done       = False
        self._pp_nest_resume_idx = 0        # mirrors the current reel's resume_idx, for status/log messages
        self._pp_mute_ref        = True     # remembered from the first Nest Episodes click
        self._pp_ame_preset_paths = {}      # {style: preset path}, auto-found per style — see _pp_find_ame_preset/_pp_resolve_style_presets
        self._pp_exp_resume_idx  = 0        # next unqueued (included) episode position — for Continue Queueing
        self._pp_exp_chip_canvases = []
        self._pp_exp_disabled    = set()    # episode names excluded from the next Queue Episodes run
        self._pp_exp_all_disabled = False
        self._pp_exp_started     = False    # locks EXPORT QUEUE toggling once queueing has begun
        self._pp_exp_queued      = set()    # episode names successfully queued in ANY prior run this session — permanent
        self._pp_exp_done_style  = {}       # {episode name: style} — whichever style's pass finished it last, for chip color on rebuild
        self._pp_exp_run_complete = False   # True once a queue run finishes without being paused — locks DISABLE ALL/ENABLE ALL/CLEAR ALL until a fresh scan/nest
        self._pp_ame_connected   = False    # True once Connect to AME has succeeded this session
        self._pp_skip_nest_mode  = False    # True once Skip Nest is clicked — changes what Connect to AME does
        self._pp_prescan_active  = False    # True while _pp_prescan_all_reels_task is running (blocks manual track re-picks — see _pp_on_manual_track_picked)
        self._pp_prescan_abort   = False    # Reset Nest's way to interrupt a still-running prescan without disconnecting (see _pp_reset_nest) — cleared at the start of each fresh prescan
        self._pp_scan_cancel     = False    # STOP's per-task abort for a single reel scan (arm_timeline/track re-pick/prescan) — see _pp_scan_tracks/_pp_rescan_current_reel. Cleared at the start of each fresh scan
        self._pp_connect_cancel  = False    # STOP's per-task abort for Connect to Premiere — see _pp_connect_task
        self._pp_ame_scan_cancel = False    # STOP's per-task abort for Connect to AME/Rescan Episodes — see _pp_skip_nest_scan_task
        self._pp_trailer_seq_name = None    # export-queue name of the TRAILER entry found by Skip Nest, if any — gates SOCIAL MEDIA and routes trailer exports
        # Background SRT watcher state — deliberately NOT reset by RESET
        # ALL, since it tracks files that may still be rendering in AME
        # long after the UI moves on. See _pp_start_srt_watcher.
        self._pp_srt_watch_targets = []     # [(expected .srt source path, destination folder), ...]
        self._pp_srt_watch_lock = threading.Lock()
        self._pp_srt_watcher_running = False

        # ── SHOW INFO ──────────────────────────────────────────────────────
        self._section_label(main, "SHOW INFO")
        self._pp_show_panel = self._panel(main)

        # Input mode
        self._pp_show_mode_var = tk.StringVar(value="auto")
        self._mode_row(self._pp_show_panel, self._pp_show_mode_var,
                       self._pp_on_show_mode_change)

        prev_row = tk.Frame(self._pp_show_panel, bg=BG_PANEL)
        prev_row.pack(fill="x", pady=4)
        tk.Label(prev_row, text="Filename Preview", font=FONT_LABEL, bg=BG_PANEL,
                 fg=TEXT_PRIMARY, width=22, anchor="w").pack(side="left")
        self._pp_preview_label = tk.Label(prev_row, text="Connect to Premiere to auto-fill...",
                                           font=FONT_SMALL, bg=BG_PANEL, fg=ACCENT, anchor="w")
        self._pp_preview_label.pack(side="left", fill="x", expand=True)

        # Manual show info fields (hidden in auto mode) — same style as VFX tab
        self._pp_manual_frame = tk.Frame(self._pp_show_panel, bg=BG_PANEL)
        self.pp_show_code = tk.StringVar()
        self.pp_acronym   = tk.StringVar()
        self.pp_date      = tk.StringVar(value=datetime.now().strftime("%y%m%d"))

        self._field_row(self._pp_manual_frame, "Show Code",        self.pp_show_code, "e.g. V-LA35")
        self._field_row(self._pp_manual_frame, "Acronym",          self.pp_acronym,   "e.g. HBFSHR")
        self._field_row(self._pp_manual_frame, "Date (YYMMDD)",    self.pp_date,      "e.g. 260617")

        def _update_pp_preview(*_):
            sc = self.pp_show_code.get().strip()
            ac = self.pp_acronym.get().strip()
            dt = self.pp_date.get().strip()
            if sc and ac and dt:
                self._pp_preview_label.config(text=f"{sc}_{ac}_FINAL_EP01_{dt}")
            else:
                self._pp_preview_label.config(text="Connect to Premiere to auto-fill...")
        for v in [self.pp_show_code, self.pp_acronym, self.pp_date]:
            v.trace_add("write", _update_pp_preview)

        # ── TITLE CARD TRACK ─────────────────────────────────────────────────
        self._section_label(main, "TITLE CARD TRACK")
        self._pp_track_panel = self._panel(main)
        self._pp_track_radios = self._mode_row(self._pp_track_panel, self._pp_track_mode,
                                                self._pp_on_track_mode_change)

        # Auto mode: one box PER REEL, same layout as Manual mode below
        # (identical widget structure/dimensions on purpose — switching
        # modes used to visibly change the section's height because the
        # old single-box Auto layout wasn't shaped the same as Manual's
        # row of boxes), each showing that reel's auto-detected track as
        # a readonly (non-clickable) dropdown instead of a picker. Boxes
        # appear one by one as each reel's background scan actually
        # finishes (see _pp_refresh_auto_track_box), not all at once.
        self._pp_track_auto_row = tk.Frame(self._pp_track_panel, bg=BG_PANEL)
        self._pp_track_auto_row.pack(anchor="w", pady=(6, 0))  # "auto" is the default mode
        self._pp_track_auto_boxes = {}  # reel label -> box dict
        self._pp_build_auto_track_boxes()

        # Manual mode: one box PER REEL, side by side, each with its own
        # track picker — filtered to whichever tracks actually exist and
        # have a plausible clip count for that reel once its scan lands
        # (see _pp_refresh_manual_track_box), falling back to the full
        # "V1".."V20" list until then / if nothing plausible was found.
        # Built as soon as every reel's name is known (right after
        # Connect to Premiere — see _pp_build_manual_track_boxes), not tied
        # to a reel having actually been scanned/armed yet; picking a track
        # here is what triggers registering that reel (see
        # _pp_on_manual_track_picked).
        self._pp_track_manual_row = tk.Frame(self._pp_track_panel, bg=BG_PANEL)
        self._pp_track_manual_boxes = {}  # reel label -> box dict

        # ── OUTPUT FOLDER ────────────────────────────────────────────────────
        self._section_label(main, "OUTPUT FOLDER")
        self._pp_out_panel = self._panel(main)
        self._mode_row(self._pp_out_panel, self._pp_out_mode,
                       self._pp_on_out_mode_change)

        # Browse box — always visible, same look as the VFX tab's Output Folder box.
        # Disabled/greyed with the auto-detected path shown read-only in Auto mode,
        # editable in Manual mode.
        self._pp_out_dir = tk.StringVar()
        # Separate destinations for MARKETING episodes and any TRAILER
        # export (LIVE/MARKETING/SOCIAL MEDIA all land together here) —
        # auto-detected alongside the LIVE folder in _pp_detect_output.
        # Manual-mode-only browse rows for these two are built below and
        # only shown when actually relevant (MARKETING checked / a TRAILER
        # entry is in the queue) — see _pp_refresh_manual_folder_rows.
        self._pp_marketing_out_dir = tk.StringVar()
        self._pp_trailer_out_dir = tk.StringVar()
        # Destination the background SRT watcher moves .srt sidecars to,
        # once LIVE WITH SRTs actually renders them — see
        # _pp_start_srt_watcher. Auto-detected alongside LIVE/MARKETING.
        self._pp_srt_out_dir = tk.StringVar()

        # Fixed 2x2 grid, always fully visible in both Auto and Manual
        # mode — row 0: LIVE, SRT. Row 1: MARKETING (below LIVE), TRAILER
        # (below SRT). Relevance/mode is expressed as enabled vs. disabled
        # (greyed) per box, not by hiding/showing or reflowing them — see
        # _pp_refresh_out_folder_enabled, which is the single place that
        # decides each box's enabled state. uniform="outcol" forces both
        # columns to the same width even when e.g. MARKETING and TRAILER's
        # natural label widths differ.
        self._pp_out_grid = tk.Frame(self._pp_out_panel, bg=BG_PANEL)
        self._pp_out_grid.pack(fill="x")
        self._pp_out_grid.columnconfigure(0, weight=1, uniform="outcol")
        self._pp_out_grid.columnconfigure(1, weight=1, uniform="outcol")

        self._pp_out_browse_row, self._pp_out_entry, self._pp_out_browse_btn = \
            self._pp_build_out_folder_cell(self._pp_out_grid, "LIVE", self._pp_out_dir, 0, 0)
        self._pp_srt_out_row, self._pp_srt_out_entry, self._pp_srt_out_browse_btn = \
            self._pp_build_out_folder_cell(self._pp_out_grid, "SRT", self._pp_srt_out_dir, 0, 1)
        self._pp_marketing_out_row, self._pp_marketing_out_entry, self._pp_marketing_out_browse_btn = \
            self._pp_build_out_folder_cell(self._pp_out_grid, "MARKETING", self._pp_marketing_out_dir, 1, 0)
        self._pp_trailer_out_row, self._pp_trailer_out_entry, self._pp_trailer_out_browse_btn = \
            self._pp_build_out_folder_cell(self._pp_out_grid, "TRAILER", self._pp_trailer_out_dir, 1, 1)
        self._pp_refresh_out_folder_enabled()

        # Always below the whole grid, regardless of which/how many of the
        # four boxes are currently showing.
        self._pp_out_hint = tk.Label(self._pp_out_panel, text="Auto-detected after nesting",
                                      font=FONT_SMALL, bg=BG_PANEL, fg=TEXT_MUTED, anchor="w")
        self._pp_out_hint.pack(anchor="w", pady=(4, 0))

        # ── PHASE 1 — NEST EPISODES ────────────────────────────────────────
        self._section_label(main, "PHASE 1 — NEST EPISODES")
        p1_row = tk.Frame(main, bg=BG_DARK)
        p1_row.pack(fill="x", pady=(0, 12), padx=(4, 0))
        self._pp_p1_row = p1_row
        self._pp_circ_p1_1 = self._step_circle(p1_row, "1")
        # min_width=170 matches the timeline dropdown and "Rescan Reels"
        # button that can occupy this same slot later — so swapping between
        # them never shifts "Nest Episodes" and everything after it.
        self.btn_pp_connect = self._rounded_btn(p1_row, "Connect to Premiere", self._pp_connect,
                                                 min_width=170)
        self.btn_pp_connect.pack(side="left", padx=(0, 14))
        self._pp_circ_p1_2 = self._step_circle(p1_row, "2", enabled=False)
        self.btn_pp_run = self._rounded_btn(p1_row, "Nest Episodes",
                                             self._pp_run_autonest, enabled=False, accent=True,
                                             reserve_text="Continue Nesting")
        self.btn_pp_run.pack(side="left", padx=(0, 14))
        self.btn_pp_stop_nest = self._rounded_btn(p1_row, "■ Stop",
                                                   self._pp_stop_nest_now, enabled=False)
        # Don't pack — Stop replaces Run button when running
        self.btn_pp_reset_nest = self._rounded_btn(p1_row, "Reset Nest",
                                                    self._pp_reset_nest_click, enabled=False)
        self.btn_pp_reset_nest.pack(side="left")

        # Skip Nest — bypass Phase 1 entirely when episodes were already
        # nested in a previous session; unlocks Connect to AME, which then
        # scans the project for existing FINAL_EP sequences instead of just
        # launching AME. Only usable once connected to Premiere.
        self.btn_pp_skip_nest = self._rounded_btn(p1_row, "Skip Nest",
                                                   self._pp_skip_nest_click, enabled=False)
        self.btn_pp_skip_nest.pack(side="right")

        # Mutes (Track.setMute) the Video Reference track + everything below
        # it — was a one-time confirm dialog per nest run, now a persistent
        # checkbox (same pattern as the VFX tab's "Create .xlsx" checkbox) so
        # it's visible and settable up front instead of interrupting every
        # reel.
        self._pp_mute_var = tk.BooleanVar(value=False)
        self._pp_mute_check = self._canvas_checkbox(p1_row, self._pp_mute_var, "Mute Master Clips")
        self._pp_mute_check.pack(side="left", padx=(16, 0))
        self._pp_mute_check.set_enabled(False)

        # Nest All — when checked, Nest Episodes processes every STRINGOUT
        # timeline in order starting from the first not-yet-nested one,
        # instead of just the currently selected reel. Timeline selection is
        # moot while this is on, so the dropdown locks along with it.
        self._pp_nest_all_var = tk.BooleanVar(value=False)
        self._pp_nest_all_check = self._canvas_checkbox(p1_row, self._pp_nest_all_var, "Nest All")
        self._pp_nest_all_check.pack(side="left", padx=(12, 0))
        self._pp_nest_all_check.set_enabled(False)
        self._pp_nest_all_var.trace_add("write", lambda *_: self._pp_on_nest_all_toggled())

        tk.Label(p1_row, text="Starting Episode #", font=FONT_SMALL,
                 bg=BG_DARK, fg=TEXT_MUTED).pack(side="left", padx=(16, 8))
        self.pp_start_ep = tk.StringVar(value="1")
        self._pp_ep_entry = tk.Entry(p1_row, textvariable=self.pp_start_ep,
                                      font=FONT_LABEL, bg=BG_INPUT, fg=TEXT_PRIMARY,
                                      relief="flat", bd=4, insertbackground=TEXT_PRIMARY,
                                      width=4, state="disabled", justify="center",
                                      highlightthickness=0, disabledbackground=BG_INPUT,
                                      disabledforeground=TEXT_MUTED)
        self._pp_ep_entry.pack(side="left")
        self._pp_ep_entry.bind("<FocusOut>", lambda e: self._pp_on_start_ep_edited())
        self._pp_ep_entry.bind("<Return>", lambda e: self._pp_on_start_ep_edited())

        # Progress
        self._pp_nest_progress_var = tk.DoubleVar(value=0)
        self._pp_nest_progress_var2 = tk.DoubleVar(value=0)
        self._pp_nest_progress = ttk.Progressbar(main,
                                                   variable=self._pp_nest_progress_var2,
                                                   maximum=100, mode="determinate",
                                                   style="PPNest.Horizontal.TProgressbar")
        # pady top=16 matches VFX's progress_bar (self.progress_bar) — this
        # used to be 4, giving noticeably less breathing room above the bar.
        self._pp_nest_progress.pack(fill="x", pady=(16, 2))
        _nest_style = ttk.Style()
        _nest_style.configure("PPNest.Horizontal.TProgressbar",
                               troughcolor=BG_INPUT, background=ACCENT,
                               borderwidth=0, thickness=10)

        self._pp_nest_status = tk.Label(main, text="",
                                         font=FONT_SMALL, bg=BG_DARK, fg=TEXT_MUTED, anchor="w")
        self._pp_nest_status.pack(anchor="w", pady=(0, 6))

        # EPISODES NESTED chip box (like ep_outer in VFX tab)
        self._section_label(main, "EPISODES NESTED")
        # padx=12, pady=0 on the outer frame + symmetric pady=12 on the
        # inner frame's pack() — matches VFX's ep_outer (DETECTED EPISODES)
        # exactly, so both boxes are the same height when empty (24px:
        # 12 top + 12 bottom). The "REEL N" header label packed inside
        # this frame (see _pp_build_reel_chips/_pp_refresh_chip_visibility)
        # has its own pady=(2, 2) on top of this once populated — a
        # visible gap difference vs. DETECTED EPISODES once reels exist,
        # but matching the shared empty-state box height takes priority.
        self._pp_nest_ep_outer = tk.Frame(main, bg="#252525", padx=12, pady=0)
        # bottom=20 (was 8) — extra separation from PHASE 2's header below,
        # which otherwise sat close enough to read as part of this box.
        self._pp_nest_ep_outer.pack(fill="x", pady=(0, 20))
        self._pp_nest_chips_frame = tk.Frame(self._pp_nest_ep_outer, bg="#252525")
        self._pp_nest_chips_frame.pack(fill="x", pady=12)
        tk.Frame(self._pp_nest_chips_frame, bg="#252525", width=0, height=0).pack()

        # ── PHASE 2 — EXPORT ──────────────────────────────────────────────
        self._section_label(main, "PHASE 2 — EXPORT")
        p2_row = tk.Frame(main, bg=BG_DARK)
        p2_row.pack(fill="x", pady=(0, 12), padx=(4, 0))
        self._pp_circ_p2_1 = self._step_circle(p2_row, "1", enabled=False)
        # min_width=170 matches the Phase 1 connect slot's width — this
        # button relabels itself to "Rescan Episodes" in place (see
        # _pp_connect_ame) without ever being destroyed/recreated, so it
        # needs enough room reserved up front for the longer text.
        self.btn_pp_connect_ame = self._rounded_btn(p2_row, "Connect to AME", self._pp_connect_ame,
                                                      enabled=False, min_width=170)
        self.btn_pp_connect_ame.pack(side="left", padx=(0, 14))
        self._pp_circ_p2_2 = self._step_circle(p2_row, "2", enabled=False)
        self.btn_pp_export = self._rounded_btn(p2_row, "Queue Episodes",
                                                self._pp_run_export, enabled=False, accent=True,
                                                reserve_text="Continue Queueing")
        self.btn_pp_export.pack(side="left", padx=(0, 14))
        self.btn_pp_stop_exp = self._rounded_btn(p2_row, "■ Stop",
                                                  self._pp_stop_export_now, enabled=False)
        # Don't pack — Stop replaces Export button when running
        self.btn_pp_reset_exp = self._rounded_btn(p2_row, "Reset Queue",
                                                   self._pp_reset_export, enabled=False)
        self.btn_pp_reset_exp.pack(side="left")

        # Export styles — each checked style gets queued for every episode,
        # in this order, using that style's own preset (matched by exact
        # name, e.g. MARKETING.epr) and mute rules (see
        # _pp_apply_variant_mute_state / _pp_build_export_variants). LIVE is
        # the existing/default behavior and starts checked; MARKETING/SOCIAL
        # MEDIA are opt-in and locked until AME connects (see chk.set_enabled below).
        self._pp_style_vars = {}
        self._pp_style_checks = {}
        for label, default_checked in (("LIVE", True), ("MARKETING", False), ("SOCIAL MEDIA", False)):
            var = tk.BooleanVar(value=default_checked)
            self._pp_style_vars[label] = var
            chk = self._canvas_checkbox(p2_row, var, label)
            chk.pack(side="left", padx=(16, 0))
            chk.set_enabled(False)  # locked until Connect to AME succeeds
            self._pp_style_checks[label] = chk
            if label == "SOCIAL MEDIA":
                # SRT sits immediately to the right of SOCIAL MEDIA, matching
                # the same enabled/disabled-until-AME-connected gating as
                # LIVE/MARKETING (not further gated on trailer presence).
                # Requires LIVE also checked to do anything — see _pp_run_export.
                self._pp_srt_var = tk.BooleanVar(value=False)
                self._pp_srt_check = self._canvas_checkbox(p2_row, self._pp_srt_var, "SRT")
                self._pp_srt_check.pack(side="left", padx=(16, 0))
                self._pp_srt_check.set_enabled(False)
                self._pp_srt_var.trace_add("write", lambda *a: self._pp_refresh_manual_folder_rows())
            if label in ("LIVE", "MARKETING"):
                # Reveals/hides the Manual-mode MARKETING/SRT output-folder
                # rows — see _pp_refresh_manual_folder_rows.
                var.trace_add("write", lambda *a: self._pp_refresh_manual_folder_rows())
            if label == "LIVE":
                # SRT depends on LIVE — disable (and force-uncheck) it the
                # moment LIVE gets unchecked. See _pp_refresh_srt_enabled.
                var.trace_add("write", lambda *a: self._pp_refresh_srt_enabled())

        # Refreshes the date embedded in each queued item's name (both
        # episodes and the trailer always end in _YYMMDD) to today's date
        # right before it's queued — renames the Premiere sequence itself,
        # not just the output filename. Opt-in since not every queue run
        # is meant to bump the date (e.g. same-day re-queues). Same
        # enabled/disabled-until-AME-connected gating as LIVE/MARKETING.
        self._pp_update_date_var = tk.BooleanVar(value=False)
        self._pp_update_date_check = self._canvas_checkbox(p2_row, self._pp_update_date_var, "UPDATE DATE")
        self._pp_update_date_check.pack(side="left", padx=(16, 0))
        self._pp_update_date_check.set_enabled(False)

        self._pp_exp_progress_var = tk.DoubleVar(value=0)
        self._pp_exp_progress = ttk.Progressbar(main,
                                                  variable=self._pp_exp_progress_var,
                                                  maximum=100, mode="determinate",
                                                  style="PPExp.Horizontal.TProgressbar")
        # pady top=16 matches VFX's progress_bar (self.progress_bar) — this
        # used to be 4, giving noticeably less breathing room above the bar.
        self._pp_exp_progress.pack(fill="x", pady=(16, 2))
        _exp_style = ttk.Style()
        _exp_style.configure("PPExp.Horizontal.TProgressbar",
                              troughcolor=BG_INPUT, background=self.EXP_PROGRESS_DEFAULT,
                              borderwidth=0, thickness=10)

        self._pp_exp_status = tk.Label(main, text="",
                                        font=FONT_SMALL, bg=BG_DARK, fg=TEXT_MUTED, anchor="w")
        self._pp_exp_status.pack(anchor="w", pady=(0, 6))

        # EXPORT QUEUE chip box (toggleable, mirrors VFX's DETECTED EPISODES)
        exp_header = tk.Frame(main, bg=BG_DARK)
        # pady=(12, 2) lands the label-to-box gap at 4px, matching
        # _section_label's gap (e.g. PHASE 1/2, EPISODES NESTED, NUMBER OF
        # PLATES DETECTED) — this row's own height (taller than the label
        # alone, since DISABLE ALL/CLEAR ALL sit beside it) already
        # contributes 2px of that before any pady is added.
        exp_header.pack(fill="x", pady=(12, 2))
        tk.Label(exp_header, text="EXPORT QUEUE",
                 font=("SF Pro Display", 10, "bold"), bg=BG_DARK, fg=TEXT_MUTED).pack(side="left")
        self.btn_toggle_exp_all = tk.Label(exp_header, text="DISABLE ALL",
                                            font=("SF Pro Display", 10, "bold"),
                                            bg="#2a2a2a", fg="#555555",
                                            padx=10, pady=3)
        self.btn_toggle_exp_all.pack(side="right")
        self.btn_toggle_exp_all.bind("<Enter>",           lambda e: self.btn_toggle_exp_all.config(bg="#3a3a3a"))
        self.btn_toggle_exp_all.bind("<Leave>",           lambda e: self.btn_toggle_exp_all.config(bg=BG_INPUT))
        self.btn_toggle_exp_all.bind("<ButtonPress-1>",   lambda e: self.btn_toggle_exp_all.config(bg="#1a1a1a"))
        self.btn_toggle_exp_all.bind("<ButtonRelease-1>", lambda e: (self.btn_toggle_exp_all.config(bg="#3a3a3a"), self._pp_toggle_all_exp()))

        # CLEAR ALL — same look, but only shown once there's actually
        # something in the queue to clear (see _pp_refresh_exp_clear_buttons,
        # called from _pp_build_exp_chips).
        self.btn_clear_all_exp = tk.Label(exp_header, text="CLEAR ALL",
                                           font=("SF Pro Display", 10, "bold"),
                                           bg="#2a2a2a", fg="#555555", padx=10, pady=3)
        self.btn_clear_all_exp.bind("<Enter>",           lambda e: self.btn_clear_all_exp.config(bg="#3a3a3a"))
        self.btn_clear_all_exp.bind("<Leave>",           lambda e: self.btn_clear_all_exp.config(bg=BG_INPUT))
        self.btn_clear_all_exp.bind("<ButtonPress-1>",   lambda e: self.btn_clear_all_exp.config(bg="#1a1a1a"))
        self.btn_clear_all_exp.bind("<ButtonRelease-1>", lambda e: (self.btn_clear_all_exp.config(bg="#3a3a3a"), self._pp_clear_all_exp()))
        # padx=12, pady=0 on the outer frame + symmetric pady=12 on the
        # canvas's pack() — matches VFX's ep_outer (DETECTED EPISODES)
        # exactly, same as _pp_nest_ep_outer above, so all three chip
        # boxes are the same height when empty (24px: 12 top + 12 bottom).
        self._pp_exp_ep_outer = tk.Frame(main, bg="#252525", padx=12, pady=0)
        self._pp_exp_ep_outer.pack(fill="x", pady=(0, 8))
        # Chips live inside a Canvas (not a plain Frame) so _pp_resize_window
        # can cap this area's height and let it scroll once the window would
        # otherwise grow past the bottom of the screen — SHOW LOG and
        # everything below it stays visible instead of getting pushed off.
        # The scrollbar itself is only packed in when actually needed (see
        # _pp_resize_window), not shown for a queue that fits naturally.
        # height=1 here matters: unlike a plain Frame, a Canvas doesn't
        # auto-shrink to fit empty content — left unset, it renders at
        # Tk's built-in default (198px) until _pp_resize_window() first
        # runs to shrink it, which only happens later (on connect/nest/
        # queue changes) — so a fresh, empty queue would otherwise show
        # as a big empty grey box for no reason. 1 (not 0) because Tk
        # enforces a hard 1px floor on Canvas height regardless — this
        # box ends up 1px taller than the other two (25px vs. 24px) for
        # that reason alone, as close a match as a Canvas structurally
        # allows.
        self._pp_exp_scroll_canvas = tk.Canvas(self._pp_exp_ep_outer, bg="#252525",
                                                highlightthickness=0, height=1)
        self._pp_exp_scrollbar = tk.Scrollbar(self._pp_exp_ep_outer, orient="vertical",
                                               command=self._pp_exp_scroll_canvas.yview)
        self._pp_exp_scroll_canvas.configure(yscrollcommand=self._pp_exp_scrollbar.set)
        self._pp_exp_scroll_canvas.pack(side="left", fill="both", expand=True, pady=12)
        self._pp_exp_chips_frame = tk.Frame(self._pp_exp_scroll_canvas, bg="#252525")
        self._pp_exp_chips_window = self._pp_exp_scroll_canvas.create_window(
            (0, 0), window=self._pp_exp_chips_frame, anchor="nw")
        tk.Frame(self._pp_exp_chips_frame, bg="#252525", width=0, height=0).pack()

        def _pp_exp_chips_configure(event):
            self._pp_exp_scroll_canvas.configure(scrollregion=self._pp_exp_scroll_canvas.bbox("all"))
        self._pp_exp_chips_frame.bind("<Configure>", _pp_exp_chips_configure)

        def _pp_exp_canvas_configure(event):
            # Keep the inner frame's width matching the canvas's actual
            # rendered width (which shrinks once the scrollbar is packed
            # in) — _pp_build_exp_chips wraps chips into rows based on
            # self._pp_exp_chips_frame.winfo_width(), same as before.
            self._pp_exp_scroll_canvas.itemconfig(self._pp_exp_chips_window, width=event.width)
        self._pp_exp_scroll_canvas.bind("<Configure>", _pp_exp_canvas_configure)

        def _pp_exp_mousewheel(event):
            self._pp_exp_scroll_canvas.yview_scroll(int(-1 * (event.delta)), "units")
        def _pp_exp_bind_wheel(_):
            self.bind_all("<MouseWheel>", _pp_exp_mousewheel)
        def _pp_exp_unbind_wheel(_):
            self.unbind_all("<MouseWheel>")
        self._pp_exp_ep_outer.bind("<Enter>", _pp_exp_bind_wheel)
        self._pp_exp_ep_outer.bind("<Leave>", _pp_exp_unbind_wheel)

        # ── LOG (hidden by default) — same as VFX tab ─────────────────────
        pp_log_header = tk.Frame(main, bg=BG_DARK)
        pp_log_header.pack(fill="x", pady=(28, 0))

        pp_btn_bg  = BG_INPUT
        pp_btn_hov = "#3a3a3a"
        pp_btn_prs = "#1a1a1a"
        self._pp_btn_log_toggle = tk.Label(pp_log_header, text="SHOW LOG",
                                            font=("SF Pro Display", 10, "bold"),
                                            bg=pp_btn_bg, fg=TEXT_PRIMARY,
                                            padx=10, pady=4)
        self._pp_btn_log_toggle.pack(side="left")
        self._pp_btn_log_toggle.bind("<Enter>",           lambda e: self._pp_btn_log_toggle.config(bg=pp_btn_hov))
        self._pp_btn_log_toggle.bind("<Leave>",           lambda e: self._pp_btn_log_toggle.config(bg=pp_btn_bg))
        self._pp_btn_log_toggle.bind("<ButtonPress-1>",   lambda e: self._pp_btn_log_toggle.config(bg=pp_btn_prs))
        self._pp_btn_log_toggle.bind("<ButtonRelease-1>", lambda e: (self._pp_btn_log_toggle.config(bg=pp_btn_hov), self._pp_toggle_log()))

        self._pp_log_win = None
        self._pp_log_box = None
        self._pp_log_visible = False
        self._pp_log_buffer = []  # (message, tag) pairs — persists across log window close/reopen, and across never having opened it at all

    def _pp_toggle_log(self):
        if self._pp_log_visible and self._pp_log_win is not None and self._pp_log_win.winfo_exists():
            self._pp_log_win.destroy()
            self._pp_log_win = None
            self._pp_log_box = None
            self._pp_btn_log_toggle.config(text="SHOW LOG")
            self._pp_log_visible = False
        else:
            self._pp_log_win, self._pp_log_box = self._build_log_window("Log — Episode Export")
            self._pp_log_win.protocol("WM_DELETE_WINDOW", self._pp_toggle_log)
            self._pp_btn_log_toggle.config(text="HIDE LOG")
            self._pp_log_visible = True
            if self._pp_log_buffer:
                self._pp_log_box.config(state="normal")
                for msg, tag in self._pp_log_buffer:
                    self._pp_log_box.insert("end", msg + "\n", tag)
                self._pp_log_box.see("end")
                self._pp_log_box.config(state="disabled")
            self._pp_log_win.lift()
        self._pp_resize_window()

    def _pp_resize_window(self):
        """Resize window to fit content, capping total height at the
        screen's available height once it would otherwise grow past the
        bottom of the screen. Export Queue's chip area absorbs the
        overflow — it gets a fixed, scrollable height instead of growing
        indefinitely — so SHOW LOG and everything below Export Queue stays
        visible on screen instead of getting pushed off. A queue that
        fits naturally gets no scrollbar at all. Window is locked
        resizable(False, False), which blocks geometry() from growing it
        on macOS unless briefly unlocked."""
        self.update_idletasks()
        self.resizable(False, True)

        # Always remeasure from the same clean baseline — scrollbar
        # unpacked, canvas at the chip area's own natural height — so
        # non_chip_height below isn't distorted by whatever the scrollbar
        # happened to be doing on the previous call (it takes width away
        # from the canvas once packed, which would otherwise throw off a
        # measurement taken after packing it).
        if self._pp_exp_scrollbar.winfo_ismapped():
            self._pp_exp_scrollbar.pack_forget()
        chips_natural = self._pp_exp_chips_frame.winfo_reqheight()
        self._pp_exp_scroll_canvas.configure(height=chips_natural)
        self.update_idletasks()

        full_height = self.winfo_reqheight()
        non_chip_height = full_height - chips_natural
        # 170, not a tighter value — menu bar/title bar/Dock account for
        # under half of that; the rest is slack for the platform quirk the
        # retry below already works around (a large single-call height
        # jump measures a few dozen px short of its own target the first
        # time it's actually applied), so the cap still holds even before
        # that retry lands.
        max_height = self.winfo_screenheight() - 170

        if full_height > max_height:
            capped_chip_height = max(60, max_height - non_chip_height)
            self._pp_exp_scroll_canvas.configure(height=capped_chip_height)
            self._pp_exp_scrollbar.pack(side="right", fill="y", pady=12)
            # A single update_idletasks() doesn't reliably finish
            # propagating a canvas height change into the actual rendered
            # layout when there are hundreds of chip widgets in the tree
            # (confirmed live — the canvas ends up configured to the right
            # height but rendered far shorter, only catching up on some
            # later, unrelated idle pass). Loop until it's actually caught
            # up before computing the final window geometry from it.
            for _ in range(10):
                self.update_idletasks()
                if abs(self._pp_exp_scroll_canvas.winfo_height() - capped_chip_height) <= 1:
                    break
            target_height = max_height
        else:
            target_height = full_height

        self.update_idletasks()
        self.geometry(f"{APP_WIDTH}x{target_height}")
        self.resizable(False, False)
        self.update_idletasks()
        # A single resizable(False,True) -> geometry() -> resizable(False,False)
        # pass doesn't reliably apply a LARGE jump in target height on macOS
        # (confirmed live — winfo_height() ends up far short of target_height
        # right after this call, even though winfo_reqheight() already
        # agrees with target_height) — the same underlying quirk this
        # function's resizable toggle already works around for growth in
        # general, just not fully covered for a big jump in one shot.
        # Retrying the apply step immediately, nested in this same call,
        # does NOT help (confirmed live) — only a genuinely separate,
        # later call does, once Tk's event queue has had a chance to fully
        # drain in between. Schedule one deferred retry rather than
        # looping in place; the pending-flag guards against ever
        # scheduling a second one on top of an already-pending retry.
        if (abs(self.winfo_height() - target_height) > 4
                and not getattr(self, "_pp_resize_retry_pending", False)):
            self._pp_resize_retry_pending = True

            def _pp_resize_retry():
                self._pp_resize_retry_pending = False
                self._pp_resize_window()
            self.after(50, _pp_resize_retry)

    def _pp_alert_dialog(self, title, message, show_illustration=False, tip=None):
        """Themed OK/Cancel modal for a heads-up that needs acknowledgment,
        with an easy way out (Cancel) if the user realizes they need to go
        do something first. Blocks until answered. Returns True if OK was
        clicked, False for Cancel or the window close box.

        show_illustration=True lays the header+message out next to the
        same (fairly tall — 170x198) track-targeting animation used in the
        Setup Guide, header and message both sitting level with the top of
        the animation rather than stacked above the whole row — for a
        reminder that benefits from a visual, not just text (currently
        only "Before You Start"). tip, if given, renders as a second,
        separate paragraph right below message, no divider between them —
        a practical follow-up ("what happens if I don't select a track")
        rather than a restatement of the header."""
        result = {"value": False}
        dlg = tk.Toplevel(self)
        # Withdrawn immediately — a Toplevel is visible the instant it's
        # created, at whatever position the OS/window manager picks by
        # default (top-left-ish on macOS), BEFORE the geometry() call
        # below ever runs. That was the corner-then-center glitch: a
        # real, briefly-visible frame at the wrong spot, then a jump.
        # Deiconified only once actually positioned correctly.
        dlg.withdraw()
        dlg.title(title)
        dlg.configure(bg=BG_DARK)
        dlg.resizable(False, False)
        dlg.transient(self)

        body = tk.Frame(dlg, bg=BG_DARK, padx=24, pady=20)
        body.pack(fill="both", expand=True)

        if show_illustration:
            cols = tk.Frame(body, bg=BG_DARK)
            cols.pack(fill="x", pady=(0, 16))
            left = tk.Frame(cols, bg=BG_DARK)
            left.pack(side="left", anchor="n", padx=(0, 16))
            self._pp_build_track_illustration(left)
            right = tk.Frame(cols, bg=BG_DARK)
            right.pack(side="left", anchor="n", fill="x", expand=True)
            tk.Label(right, text=title, font=("SF Pro Display", 16, "bold"),
                     bg=BG_DARK, fg=ACCENT, anchor="w").pack(anchor="w", pady=(0, 8))
            tk.Frame(right, bg=ACCENT, height=1).pack(fill="x", pady=(0, 8))
            tk.Label(right, text=message, font=FONT_LABEL, bg=BG_DARK, fg=TEXT_MUTED,
                     wraplength=220, justify="left", anchor="w").pack(anchor="w", pady=(0, 8 if tip else 0))
            if tip:
                tk.Label(right, text=tip, font=FONT_SMALL, bg=BG_DARK, fg=TEXT_MUTED,
                         wraplength=220, justify="left", anchor="w").pack(anchor="w")
        else:
            tk.Label(body, text=title, font=("SF Pro Display", 16, "bold"),
                     bg=BG_DARK, fg=ACCENT, anchor="w").pack(anchor="w", pady=(0, 8))
            tk.Label(body, text=message, font=FONT_LABEL, bg=BG_DARK, fg=TEXT_MUTED,
                     wraplength=380, justify="left", anchor="w").pack(anchor="w", pady=(0, 16))
            if tip:
                tk.Label(body, text=tip, font=FONT_SMALL, bg=BG_DARK, fg=TEXT_MUTED,
                         wraplength=380, justify="left", anchor="w").pack(anchor="w", pady=(0, 16))

        btn_row = tk.Frame(body, bg=BG_DARK)
        btn_row.pack(anchor="e", fill="x")

        def _answer(val):
            result["value"] = val
            dlg.destroy()

        cancel_btn = self._rounded_btn(btn_row, "Cancel", lambda: _answer(False))
        cancel_btn.pack(side="right")
        ok_btn = self._rounded_btn(btn_row, "OK", lambda: _answer(True), accent=True)
        ok_btn.pack(side="right", padx=(0, 8))

        dlg.protocol("WM_DELETE_WINDOW", lambda: _answer(False))
        dlg.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_reqheight()) // 2
        dlg.geometry(f"+{x}+{y}")
        dlg.deiconify()
        dlg.grab_set()
        dlg.wait_window()
        return result["value"]

    def _pp_format_timecodes(self, seq, ticks_list):
        """Format tick offsets (already zero-point-adjusted by the caller — see
        _pp_get_zero_ticks) as on-screen SMPTE timecodes (HH:MM:SS:FF), matching
        exactly what Premiere's timeline displays.

        Uses ticks throughout, not seconds: ticks are Premiere's exact native
        time unit (254016000000 per second) and Time.ticks is a STRING because
        tick counts exceed JS's safe-integer range — building Time objects via
        .seconds (a lossy float) instead is what caused off-by-a-frame errors
        and, in one case, an all-zero "00:00:00:00" result. Python ints are
        arbitrary-precision, so all the arithmetic (adding the zero-point
        offset) happens on the Python side; only final tick STRINGS cross into
        ExtendScript. Still a single round trip regardless of list length —
        delegates the actual SMPTE/drop-frame math to Premiere's own
        Time.getFormatted() rather than reimplementing it."""
        if not ticks_list:
            return []
        from pymiere.core import eval_script as _es
        seq_ref = "$._pymiere['{}']".format(seq._pymiere_id)
        vals = ",".join(f'"{int(t)}"' for t in ticks_list)
        script = (
            f"var __s={seq_ref}; var __set=__s.getSettings();"
            f"var __vals=[{vals}]; var __out=[];"
            "for (var __i=0; __i<__vals.length; __i++){"
            "var __t=new Time(); __t.ticks=__vals[__i];"
            "__out.push(__t.getFormatted(__set.videoFrameRate, __s.videoDisplayFormat));"
            "} __out.join('|');")
        return _es(script, decode_json=False).split("|")

    def _pp_get_zero_ticks(self, seq):
        """Sequence's start timecode (zero point) in ticks, as a Python int.

        Unlike TrackItem.start/.end (which pymiere wraps in a real Time object),
        Sequence.zeroPoint/.end return the raw ticks value directly — no nested
        .ticks to access. Confirmed by the actual 'int' object has no attribute
        'ticks' error this caused."""
        return int(seq.zeroPoint)

    def _pp_check_deps_on_start(self):
        try:
            import pymiere
            self._pp_log("All dependencies OK. Ready to connect.", "success")
        except ImportError:
            self._pp_log("Missing dependency: pymiere (pip install pymiere)", "warn")

    def _pp_log(self, message, tag=None):
        self._pp_log_buffer.append((message, tag or ""))
        def _do():
            if self._pp_log_box is None:
                return
            self._pp_log_box.config(state="normal")
            self._pp_log_box.insert("end", message + "\n", tag or "")
            self._pp_log_box.see("end")
            self._pp_log_box.config(state="disabled")
        self.after(0, _do)

    def _canvas_checkbox(self, parent, variable, text):
        """Canvas-drawn checkbox. tk.Checkbutton suffers the same macOS Tk
        indicator-widget bug that forced the radio buttons onto canvas-drawn
        widgets earlier (stays in the wrong/greyed-out color until it first
        receives focus) — this mirrors that same fix for checkboxes.
        Returns an unpacked Frame with a .set_enabled(bool) method."""
        bg = parent.cget("bg")
        frame = tk.Frame(parent, bg=bg)
        size = 16
        box = tk.Canvas(frame, width=size, height=size, bg=bg, highlightthickness=0)
        box.pack(side="left", padx=(0, 6))
        lbl = tk.Label(frame, text=text, font=("SF Pro Display", 11), bg=bg, fg=TEXT_MUTED)
        lbl.pack(side="left")
        frame._enabled = True

        def _redraw():
            box.delete("all")
            checked = variable.get()
            if not frame._enabled:
                outline, fill, check_col = "#444444", ("#444444" if checked else ""), "#888888"
                lbl.config(fg="#444444")
            else:
                outline = ACCENT if checked else "#777777"
                fill = ACCENT if checked else ""
                check_col = "#000000"
                lbl.config(fg=TEXT_PRIMARY if checked else TEXT_MUTED)
            box.create_rectangle(1, 1, size - 1, size - 1, outline=outline, width=1.5, fill=fill)
            if checked:
                box.create_line(3, size * 0.55, size * 0.42, size - 4,
                                 width=2, fill=check_col, capstyle="round")
                box.create_line(size * 0.42, size - 4, size - 3, 3,
                                 width=2, fill=check_col, capstyle="round")

        def _toggle(e=None):
            if not frame._enabled:
                return
            variable.set(not variable.get())
            _redraw()

        box.bind("<ButtonRelease-1>", _toggle)
        lbl.bind("<ButtonRelease-1>", _toggle)

        def _set_enabled(enabled):
            frame._enabled = enabled
            _redraw()
        frame.set_enabled = _set_enabled
        frame.refresh = _redraw

        _redraw()
        return frame

    def _canvas_dropdown(self, parent, variable, placeholder="Select...", command=None,
                          readonly=False, text_color=None, width=220):
        """Canvas-drawn dropdown. Replaces tk.OptionMenu, whose native popup
        ignores our dark theme colors on macOS. With readonly=True, draws the
        same box (for visual parity with the interactive version) but isn't
        clickable and has no popup — used for the Auto-mode display."""
        w, h, r = width, 30, 6
        c = tk.Canvas(parent, width=w, height=h, bg=parent.cget("bg"), highlightthickness=0)
        points = [r, 0, w - r, 0, w, 0, w, r,
                  w, h - r, w, h, w - r, h, r, h,
                  0, h, 0, h - r, 0, r, 0, 0]

        c._dd_options = []
        c._popup = None
        c._locked = False

        def _redraw_label():
            # Full delete-and-redraw rather than itemconfig on existing items —
            # macOS Tk canvases sometimes leave stale pixels after a
            # pack_forget()/pack() cycle that only an unrelated redraw
            # elsewhere would clear; recreating everything from scratch avoids
            # relying on itemconfig touching an already-correct-but-unpainted item.
            c.delete("all")
            box_fill = "#252525" if c._locked else BG_INPUT
            c.create_polygon(points, fill=box_fill, outline="", smooth=True, tags="box")
            val = variable.get()
            if val:
                fill = text_color if (readonly and text_color) else (TEXT_MUTED if c._locked else TEXT_PRIMARY)
            else:
                fill = TEXT_MUTED
            c.create_text(12, h // 2, text=val if val else placeholder,
                           font=FONT_SMALL, fill=fill, anchor="w")
            if not readonly:
                # Small drawn triangle instead of a text glyph — "▾" rendered as
                # a dot at this size/font, a drawn shape is guaranteed to look right.
                ax, ay = w - 20, h // 2
                arrow_fill = "#555555" if c._locked else TEXT_MUTED
                c.create_polygon(ax - 4, ay - 3, ax + 4, ay - 3, ax, ay + 3,
                                  fill=arrow_fill, outline="", tags="arrow")

        c.refresh = _redraw_label
        _redraw_label()

        if readonly:
            def _set_locked_readonly(locked):
                c._locked = locked
                _redraw_label()
            c.set_locked = _set_locked_readonly
            variable.trace_add("write", lambda *_: _redraw_label())
            return c

        def _close_popup():
            if c._popup is not None and c._popup.winfo_exists():
                c._popup.destroy()
            c._popup = None

        def _open_popup():
            if c._locked:
                return
            if c._popup is not None:
                _close_popup()
                return
            c.update_idletasks()
            x = c.winfo_rootx()
            y = c.winfo_rooty() + c.winfo_height()

            options = c._dd_options
            row_h = 28
            rows_data = options if options else ["No tracks detected"]
            popup_w, popup_h = w, row_h * len(rows_data)

            popup = tk.Toplevel(self)
            popup.overrideredirect(True)
            popup.attributes("-topmost", True)
            popup.configure(bg=BORDER)
            # Drawn as ONE canvas with hover tracked via tag_bind — Tk tracks
            # canvas item hover internally (its own hit-testing), unlike
            # separate child widgets in a borderless Toplevel, whose
            # Enter/Leave/Motion events don't reliably reach us on macOS.
            pc = tk.Canvas(popup, width=popup_w, height=popup_h, bg=BG_PANEL,
                            highlightthickness=0)
            pc.pack(padx=1, pady=1)

            for i, label in enumerate(rows_data):
                y0 = i * row_h
                tag = f"row{i}"
                rect_id = pc.create_rectangle(0, y0, popup_w, y0 + row_h,
                                               fill=BG_PANEL, outline="", tags=tag)
                text_id = pc.create_text(10, y0 + row_h // 2, text=label, font=FONT_SMALL,
                                          fill=TEXT_PRIMARY if options else TEXT_MUTED,
                                          anchor="w", tags=tag)
                if options:
                    pc.tag_bind(tag, "<Enter>", lambda e, rid=rect_id, tid=text_id: (
                        pc.itemconfig(rid, fill=ACCENT), pc.itemconfig(tid, fill="#000000")))
                    pc.tag_bind(tag, "<Leave>", lambda e, rid=rect_id, tid=text_id: (
                        pc.itemconfig(rid, fill=BG_PANEL), pc.itemconfig(tid, fill=TEXT_PRIMARY)))
                    def _select(e, l=label):
                        variable.set(l)
                        _close_popup()
                        if command:
                            command(l)
                    pc.tag_bind(tag, "<ButtonRelease-1>", _select)

            popup.update_idletasks()
            popup.geometry(f"{popup_w + 2}x{popup_h + 2}+{x}+{y}")
            popup.bind("<Escape>", lambda e: _close_popup())
            popup.bind("<FocusOut>", lambda e: _close_popup())
            popup.focus_set()
            c._popup = popup

        c.bind("<ButtonRelease-1>", lambda e: _open_popup())
        c.bind("<Enter>", lambda e: None if c._locked else c.itemconfig("box", fill="#3a3a3a"))
        c.bind("<Leave>", lambda e: None if c._locked else c.itemconfig("box", fill=BG_INPUT))

        def _set_options(labels):
            c._dd_options = list(labels)
            _close_popup()  # options changed — don't leave a stale popup open
        c.set_options = _set_options

        def _set_locked(locked):
            c._locked = locked
            _close_popup()
            _redraw_label()
        c.set_locked = _set_locked

        variable.trace_add("write", lambda *_: _redraw_label())
        return c

    def _step_circle(self, parent, number, enabled=True):
        """Draw a step number circle matching VFX tab style."""
        size = 24
        c = tk.Canvas(parent, width=size, height=size, bg=BG_DARK, highlightthickness=0)
        c.pack(side="left", padx=(0, 8))
        col = ACCENT if enabled else "#555555"
        c.create_oval(1, 1, size-1, size-1, outline=col, width=2, fill="")
        c.create_text(size//2, size//2, text=number,
                      font=("SF Pro Display", 11, "bold"), fill=col)
        return c

    def _pp_set_circle_done(self, canvas):
        canvas.delete("all")
        size = 24
        canvas.create_oval(1, 1, size-1, size-1, outline=TEXT_SUCCESS, width=2, fill="")
        canvas.create_text(size//2, size//2, text="✓",
                           font=("SF Pro Display", 13, "bold"), fill=TEXT_SUCCESS)

    def _pp_set_circle_active(self, canvas, number):
        canvas.delete("all")
        size = 24
        canvas.create_oval(1, 1, size-1, size-1, outline=ACCENT, width=2, fill="")
        canvas.create_text(size//2, size//2, text=number,
                           font=("SF Pro Display", 11, "bold"), fill=ACCENT)

    def _pp_set_circle_disabled(self, canvas, number):
        """Reverts an already-drawn (active/done) circle back to its
        initial grey/unreachable look — the same "#555555" _step_circle
        draws at construction with enabled=False, which otherwise has no
        way to be restored once a circle's moved past that state."""
        canvas.delete("all")
        size = 24
        canvas.create_oval(1, 1, size-1, size-1, outline="#555555", width=2, fill="")
        canvas.create_text(size//2, size//2, text=number,
                           font=("SF Pro Display", 11, "bold"), fill="#555555")

    def _pp_update_nest_bar(self):
        self._pp_nest_progress_var2.set(self._pp_nest_progress_var.get() * 100)

    def _pp_on_show_mode_change(self):
        """Show/hide manual fields based on input mode."""
        if self._pp_show_mode_var.get() == "manual":
            self._pp_manual_frame.pack(fill="x", pady=(4, 0))
        else:
            self._pp_manual_frame.pack_forget()
        # Resize immediately (not deferred) — a 50ms-later resize left the
        # window briefly out of sync with its actual content, visible as a
        # jump/snap right after clicking.
        self._pp_resize_window()

    def _pp_on_track_mode_change(self):
        if hasattr(self, "_enable_reset_btn"): self._enable_reset_btn()
        mode = self._pp_track_mode.get()
        if mode == "auto":
            self._pp_track_manual_row.pack_forget()
            self._pp_track_auto_row.pack(anchor="w", pady=(6, 0))
            self.update_idletasks()
            for box in self._pp_track_auto_boxes.values():
                box["dropdown"].refresh()
        else:
            self._pp_track_auto_row.pack_forget()
            self._pp_track_manual_row.pack(anchor="w", pady=(6, 0), fill="x")
            self.update_idletasks()
            for box in self._pp_track_manual_boxes.values():
                box["dropdown"].refresh()
        # Resize immediately (not deferred) — same jump/snap fix as
        # _pp_on_show_mode_change.
        self._pp_resize_window()

    def _pp_build_manual_track_boxes(self):
        """(Re)builds Manual mode's per-reel track boxes — one per reel in
        _pp_stringout_map, side by side, each with its own "REEL N" label,
        a static "V1".."V20" track picker (no scan needed to populate it —
        the whole point of Manual mode is picking the track yourself), and
        a tails-detection line that fills in as soon as that reel's
        background scan finds it (see _pp_refresh_manual_track_box) —
        independent of which track is picked here, since title cards and
        the tail leader clip are usually on different tracks. Called once
        every reel's name is known (_pp_show_timeline_dropdown) and at
        reset; before that (pre-connect), falls back to a single inert
        "REEL 1" placeholder box so this section never sits empty,
        matching Auto mode's own "Not detected yet" placeholder box."""
        for w in list(self._pp_track_manual_row.winfo_children()):
            w.destroy()
        self._pp_track_manual_boxes = {}
        options = [f"V{i}" for i in range(1, 21)]
        labels = list(self._pp_stringout_map.keys()) or ["REEL 1"]
        for reel_label in labels:
            outer = tk.Frame(self._pp_track_manual_row, bg=BG_PANEL)
            outer.pack(side="left", anchor="n", padx=(0, 16), pady=(0, 4))
            tk.Label(outer, text=reel_label, font=("SF Pro Display", 10, "bold"),
                     bg=BG_PANEL, fg=TEXT_PRIMARY, anchor="w").pack(anchor="w", pady=(0, 2))
            var = tk.StringVar()
            # width=220 (the _canvas_dropdown default) matches Auto mode's
            # box exactly — was 140, noticeably narrower.
            dropdown = self._canvas_dropdown(
                outer, var, placeholder="Select track...",
                command=lambda choice, rl=reel_label: self._pp_on_manual_track_picked(rl, choice))
            dropdown.set_options(options)
            dropdown.pack(anchor="w")
            hint = tk.Label(outer, text="", font=FONT_SMALL, bg=BG_PANEL, fg=TEXT_MUTED,
                             anchor="w", wraplength=220, justify="left")
            hint.pack(anchor="w", pady=(4, 0))
            self._pp_track_manual_boxes[reel_label] = {
                "outer": outer, "var": var, "dropdown": dropdown, "hint": hint}
            # A reel already known this session (armed via Auto mode, a
            # background prescan, or a previous manual pick) should show
            # what's already known instead of reverting to blank every
            # rebuild.
            pair = self._pp_stringout_map.get(reel_label)
            if pair is not None:
                seq = pair[1]
                existing = next((r for r in self._pp_reels if r["seq_id"] == seq._pymiere_id), None)
                if existing is not None:
                    self._pp_refresh_manual_track_box(reel_label, existing)

    def _pp_refresh_manual_track_box(self, reel_label, reel):
        """Syncs one Manual-mode box to a reel's current known state —
        the picked/detected track (if any), the PICKABLE OPTIONS (once
        the full scan lands, narrowed to just the tracks that actually
        exist in this reel with a plausible clip count — see
        _pp_scan_tracks's manual_options — instead of the full V1-V20
        placeholder list), and, independently, the tails line, which
        always reflects whatever the full background scan
        (_pp_scan_tracks) found for THIS reel regardless of which track
        is selected here. Called both when a box is first built and every
        time _pp_rescan_current_reel's _apply() updates or creates a reel
        (silent background prescans included), so Manual mode's boxes
        stay live instead of only reflecting a one-time snapshot taken
        before any scanning had actually finished."""
        box = self._pp_track_manual_boxes.get(reel_label)
        if box is None or reel is None:
            return
        display = reel.get("track_display") or {}
        manual_options = display.get("manual_options")
        if manual_options:
            box["dropdown"].set_options([f"V{t + 1}" for t in manual_options])
        if reel.get("track_idx") is not None:
            box["var"].set(f"V{reel['track_idx'] + 1}")
        box["hint"].config(text=display.get("tails_text", ""), fg=display.get("tails_fg", TEXT_MUTED))

    def _pp_build_auto_track_boxes(self):
        """(Re)builds Auto mode's per-reel boxes — same layout/dimensions
        as _pp_build_manual_track_boxes (see its docstring for why), but
        each box's dropdown is readonly (no picking) and shows whatever
        track that reel's background scan auto-detected. Boxes appear as
        soon as each reel's own scan lands, via _pp_refresh_auto_track_box
        — not tied to which reel is "current" in the Phase 1 dropdown,
        unlike the old single-box version this replaced."""
        for w in list(self._pp_track_auto_row.winfo_children()):
            w.destroy()
        self._pp_track_auto_boxes = {}
        labels = list(self._pp_stringout_map.keys()) or ["REEL 1"]
        for reel_label in labels:
            outer = tk.Frame(self._pp_track_auto_row, bg=BG_PANEL)
            outer.pack(side="left", anchor="n", padx=(0, 16), pady=(0, 4))
            tk.Label(outer, text=reel_label, font=("SF Pro Display", 10, "bold"),
                     bg=BG_PANEL, fg=TEXT_PRIMARY, anchor="w").pack(anchor="w", pady=(0, 2))
            var = tk.StringVar()
            dropdown = self._canvas_dropdown(
                outer, var, placeholder="Not detected yet", readonly=True, text_color=ACCENT)
            dropdown.pack(anchor="w")
            hint = tk.Label(outer, text="Auto-detected on connect", font=FONT_SMALL, bg=BG_PANEL,
                             fg=TEXT_MUTED, anchor="w", wraplength=220, justify="left")
            hint.pack(anchor="w", pady=(4, 0))
            self._pp_track_auto_boxes[reel_label] = {
                "outer": outer, "var": var, "dropdown": dropdown, "hint": hint}
            pair = self._pp_stringout_map.get(reel_label)
            if pair is not None:
                seq = pair[1]
                existing = next((r for r in self._pp_reels if r["seq_id"] == seq._pymiere_id), None)
                if existing is not None:
                    self._pp_refresh_auto_track_box(reel_label, existing)

    def _pp_refresh_auto_track_box(self, reel_label, reel):
        """Syncs one Auto-mode box's readonly dropdown + tails hint to a
        reel's current known scan result. Called every time
        _pp_rescan_current_reel's _apply() updates or creates a reel
        (silent background prescans included), so a reel's box fills in
        the moment its own scan actually lands — not only once it
        happens to become the "current" reel in the Phase 1 dropdown."""
        box = self._pp_track_auto_boxes.get(reel_label)
        if box is None or reel is None:
            return
        display = reel.get("track_display") or {}
        box["var"].set(display.get("auto_label", ""))
        box["dropdown"].refresh()
        box["hint"].config(text=display.get("tails_text", ""), fg=display.get("tails_fg", TEXT_MUTED))

    def _pp_on_manual_track_picked(self, reel_label, track_choice):
        """A "V#" pick in one of Manual mode's per-reel boxes — collects
        that reel's title clips directly on the chosen track (no
        multi-track discovery pass, unlike Auto mode's scan) and
        registers/updates the reel exactly the way arming it normally
        would, by reusing _pp_rescan_current_reel — it already handles
        both "brand new reel" and "already-known reel, track changed"
        correctly, so there's no need to duplicate that here.

        Locks the REEL dropdown to "SCANNING" for the duration, same as
        _pp_arm_timeline — picking a track here can target ANY reel's
        box, not just whichever one is currently armed/displayed, so it
        needs the same guard against a second scan starting mid-flight."""
        pair = self._pp_stringout_map.get(reel_label)
        if pair is None:
            return
        name, seq = pair
        try:
            track_idx = int(track_choice[1:]) - 1
        except (ValueError, IndexError):
            return
        existing = next((r for r in self._pp_reels if r["seq_id"] == seq._pymiere_id), None)
        if existing is not None and any(existing["done"]):
            # Track choice only matters before this reel has actually
            # started nesting — once any episode is real, the track that
            # produced it is locked in.
            return
        if self._pp_prescan_active:
            self._pp_nest_status.config(
                text="Still loading reels — try again in a moment.", fg=TEXT_WARN)
            return
        box = self._pp_track_manual_boxes.get(reel_label)
        self._pp_scan_cancel = False
        prev_display = self._pp_timeline_var.get() if getattr(self, "_pp_timeline_var", None) is not None else ""
        if getattr(self, "_pp_timeline_var", None) is not None:
            self._pp_timeline_var.set("SCANNING")
        self._pp_set_timeline_dropdown_enabled(False)

        def _on_stop():
            self._pp_scan_cancel = True
            if getattr(self, "_pp_timeline_var", None) is not None:
                self._pp_timeline_var.set(prev_display)
            self._pp_set_timeline_dropdown_enabled(True)
            if box is not None:
                box["hint"].config(text="Stopped.", fg=TEXT_WARN)

        self._start_thinking(on_stop=_on_stop)
        import threading
        threading.Thread(target=lambda: self._pp_register_manual_track(seq, track_idx, reel_label),
                          daemon=True).start()

    def _pp_register_manual_track(self, seq, track_idx, reel_label):
        """Registers the chosen track as this reel's title-card track.
        Deliberately does NOT (re)detect tails here — title cards and the
        tail leader clip are usually on different tracks, so tying tails
        to whichever track was just picked for title cards would be
        wrong more often than not. Tails stays whatever the reel's own
        full background scan (_pp_scan_tracks, which sweeps every track
        independently of title-card selection) already found; this just
        reuses it. If that scan hasn't reached this reel yet, the hint
        says so instead of silently showing nothing."""
        try:
            existing = next((r for r in self._pp_reels if r["seq_id"] == seq._pymiere_id), None)
            if existing is not None:
                tails_tc = existing.get("tails_tc")
                display = dict(existing.get("track_display") or {})
            else:
                tails_tc = None
                display = {}
            if "tails_text" not in display:
                display["tails_text"] = "Still scanning for tail leader clip..."
                display["tails_fg"] = TEXT_MUTED
            display.pop("auto_label", None)  # Manual mode's box has no auto-label line

            box = self._pp_track_manual_boxes.get(reel_label)
            if box is not None:
                self.after(0, lambda: box["hint"].config(
                    text=display["tails_text"], fg=display["tails_fg"]))

            self._pp_track_idx = track_idx
            self._pp_tails_tc = tails_tc
            self._pp_seq = seq
            self._pp_rescan_current_reel(seq, silent=False, track_display=display,
                                          manage_thinking=True, reel_label=reel_label)
        except Exception as e:
            if self._pp_scan_cancel:
                return
            import traceback
            traceback.print_exc()
            self.after(0, self._stop_thinking)
            self.after(0, lambda: self._pp_set_timeline_dropdown_enabled(True))
            self._pp_log(f"⚠ Could not register {reel_label}'s V{track_idx + 1 if track_idx is not None else '?'} track: {e}", "warn")

    def _pp_on_out_mode_change(self):
        if hasattr(self, "_enable_reset_btn"): self._enable_reset_btn()
        is_auto = self._pp_out_mode.get() == "auto"
        self._set_widgets_enabled([self._pp_out_entry, self._pp_out_browse_btn], not is_auto)
        self._pp_out_hint.config(
            text="Auto-detected after nesting" if is_auto else "Browse to select your output folder")
        self._pp_refresh_manual_folder_rows()
        self._pp_resize_window()

    def _pp_build_out_folder_cell(self, parent, label_text, var, row, col):
        """One labeled bordered-entry + Browse cell, gridded at a fixed
        (row, col) — same look as the original LIVE output folder box.
        Always visible; see _pp_refresh_out_folder_enabled for how its
        enabled/disabled state gets decided. Label and entry row are
        grouped under a single outer frame. Returns (outer, entry,
        browse_btn)."""
        outer = tk.Frame(parent, bg=BG_PANEL)
        outer.grid(row=row, column=col, sticky="ew",
                    padx=(0, 8) if col == 0 else (8, 0), pady=(0, 8))
        tk.Label(outer, text=label_text, font=("SF Pro Display", 10, "bold"),
                 bg=BG_PANEL, fg=TEXT_PRIMARY, anchor="w").pack(anchor="w", pady=(8, 2))
        entry_row = tk.Frame(outer, bg=BG_PANEL)
        entry_row.pack(fill="x", pady=(0, 2))
        wrap = tk.Frame(entry_row, bg=BG_INPUT, highlightthickness=1,
                         highlightbackground=BORDER, highlightcolor=ACCENT)
        wrap.pack(side="left", fill="x", expand=True)
        tk.Frame(wrap, bg=BG_INPUT, width=2).pack(side="left")
        entry = tk.Entry(wrap, textvariable=var, font=FONT_SMALL, bg=BG_INPUT, fg=TEXT_PRIMARY,
                          insertbackground=TEXT_PRIMARY, relief="flat",
                          bd=0, highlightthickness=0, state="disabled")
        entry.pack(side="left", fill="x", expand=True, ipady=7)
        browse_btn = self._rounded_btn(
            entry_row, "Browse...",
            lambda: self._pp_browse_into(var, f"Select {label_text} Output Folder"),
            small=True, match_height=True, enabled=False)
        browse_btn.pack(side="left", padx=(8, 0), fill="y")
        return outer, entry, browse_btn

    def _pp_browse_into(self, var, title):
        from tkinter import filedialog
        # Auto-detection (_pp_detect_output) runs regardless of Auto/Manual
        # mode and already populates this same var — start the dialog
        # there so Manual mode doesn't mean clicking through folders from
        # scratch every time, just confirming/adjusting what was found.
        start_dir = var.get().strip() or None
        if start_dir and not os.path.isdir(start_dir):
            start_dir = None
        if not start_dir:
            # This specific folder (e.g. MARKETING/SRT) may not exist on
            # disk yet even though LIVE was found — its parent (the FINAL
            # folder) is still a far better starting point than nowhere,
            # since MARKETING/SRT/TRAILER are always siblings near it.
            live_dir = self._pp_out_dir.get().strip()
            if live_dir and os.path.isdir(live_dir):
                parent = os.path.dirname(live_dir)
                if os.path.isdir(parent):
                    start_dir = parent
        path = filedialog.askdirectory(title=title, initialdir=start_dir)
        if path:
            var.set(path)

    def _pp_refresh_manual_folder_rows(self):
        self._pp_refresh_out_folder_enabled()

    def _pp_refresh_out_folder_enabled(self):
        """All four Output Folder boxes (LIVE, SRT, MARKETING, TRAILER)
        stay gridded at their fixed positions at all times, in both Auto
        and Manual mode — only their enabled/disabled (greyed) look
        changes. In Auto mode every box is disabled (they'll all be
        auto-detected). In Manual mode: LIVE is always enabled; MARKETING/
        SRT/TRAILER are enabled only once actually relevant (MARKETING
        checked, SRT checked alongside LIVE, or a TRAILER entry sitting
        in the export queue) and otherwise stay visible but greyed out,
        instead of being hidden. Re-run any time relevance changes (mode
        toggle, a style checkbox, a trailer being found, RESET ALL...)."""
        is_manual = self._pp_out_mode.get() == "manual"
        show_marketing = is_manual and self._pp_style_vars["MARKETING"].get()
        show_srt = is_manual and self._pp_srt_var.get() and self._pp_style_vars["LIVE"].get()
        show_trailer = is_manual and self._pp_trailer_seq_name is not None

        for enabled, entry, browse_btn in (
            (show_marketing, self._pp_marketing_out_entry, self._pp_marketing_out_browse_btn),
            (show_srt, self._pp_srt_out_entry, self._pp_srt_out_browse_btn),
            (show_trailer, self._pp_trailer_out_entry, self._pp_trailer_out_browse_btn),
        ):
            self._set_widgets_enabled([entry, browse_btn], enabled)
        # LIVE's own enabled state is governed by Auto/Manual mode itself
        # (see _pp_on_out_mode_change) — don't stomp on that here.

        # Guarded — this runs once at initial tab construction (placing
        # LIVE for the first time), before EXPORT QUEUE further down has
        # built the scrollbar _pp_resize_window depends on.
        if hasattr(self, "_pp_exp_scrollbar"):
            self._pp_resize_window()

    def _pp_connect(self):
        """Connect to Premiere Pro and list every STRINGOUT timeline in the
        project — not just the active one — so reels can be picked directly
        from the app without touching Premiere's UI between them."""
        self._set_btn_state(self.btn_pp_connect, False)
        # No "Connecting..." status text here — the thinking dots already
        # show activity is happening; a redundant text label just to say
        # the same thing adds noise, not information.
        self._connect_status.config(text="Connecting to Premiere...")
        self._pp_connect_cancel = False

        def _on_stop():
            # The actual ExtendScript round trip below is a single call,
            # not a loop with a checkpoint to interrupt mid-flight — so
            # STOP reverts everything immediately here instead of waiting
            # for it to return, and _pp_connect_cancel tells the
            # background thread (once it eventually does return) to
            # quietly discard its result instead of applying it or
            # touching the thinking state again.
            self._pp_connect_cancel = True
            self._connect_status.config(text="")
            self._pp_nest_status.config(text="Stopped.", fg=TEXT_WARN)
            self._set_btn_state(self.btn_pp_connect, True)

        self._start_thinking(on_stop=_on_stop)
        self.update_idletasks()
        import threading
        threading.Thread(target=self._pp_connect_task, daemon=True).start()

    def _pp_connect_task(self):
        def _status(msg, color=TEXT_MUTED):
            self.after(0, lambda m=msg, c=color: self._pp_nest_status.config(text=m, fg=c))

        try:
            import pymiere
            from pymiere.core import eval_script as _es
            EXCLUDE_TOKENS = ("XML", "AAF", "REF", "VFX", "SUB", "NESTED", "TRAILER")
            self._pp_log("Connecting to Premiere Pro...", "muted")
            _status("Scanning project for STRINGOUT reels...")

            # Scope the scan to the DELIVERY > STRINGOUT bin when it exists,
            # so a loose/misplaced STRINGOUT-named sequence elsewhere in the
            # project (or an old one sitting in an Archive subfolder) can't
            # show up as a reel. Falls back to a whole-project scan (today's
            # behavior) if that bin isn't found, rather than silently
            # showing zero reels.
            project_root = pymiere.objects.app.project.rootItem
            delivery_bin = self._pp_find_bin_by_name(project_root, "DELIVERY")
            stringout_bin = (self._pp_find_bin_by_name(delivery_bin, "STRINGOUT")
                              if delivery_bin is not None else None)
            scope_to_bin = stringout_bin is not None
            if not scope_to_bin:
                msg = ("⚠ No DELIVERY > STRINGOUT bin found — scanning the "
                       "whole project for STRINGOUT timelines instead.")
                _status(msg, TEXT_WARN)
                self._pp_log(msg, "warn")

            # The name (+ optional bin path) filter runs inside a single
            # ExtendScript call instead of once per sequence in the whole
            # project — each property access (.name, .projectItem.treePath)
            # is its own round trip through the ExtendScript bridge, and a
            # season's worth of raw STRINGOUT reels/VFX exports/work-in-
            # progress cuts can easily push total sequence count into the
            # hundreds.
            exclude_check = "||".join(f"__nm.indexOf('{tok}')!==-1" for tok in EXCLUDE_TOKENS)
            tree_fetch = (
                "var __tp=''; try{__tp=String(__s[i].projectItem.treePath).toUpperCase();}"
                "catch(e){continue;}"
                if scope_to_bin else "")
            tree_check = (
                "if(__tp.indexOf('DELIVERY')===-1||__tp.indexOf('STRINGOUT')===-1"
                "||__tp.indexOf('ARCHIVE')!==-1)continue;"
                if scope_to_bin else "")
            raw = _es(
                "var __out=[]; var __s=app.project.sequences;"
                "for(var i=0;i<__s.numSequences;i++){"
                "var __nm=String(__s[i].name||'').toUpperCase();"
                "if(__nm.indexOf('STRINGOUT')===-1)continue;"
                f"if({exclude_check})continue;"
                f"{tree_fetch}"
                f"{tree_check}"
                "__out.push(i);}"
                "__out.join(',');",
                decode_json=False)
            idxs = [int(x) for x in raw.split(",")] if raw.strip() else []
            seqs = pymiere.objects.app.project.sequences
            stringout = [(str(seqs[i].name), seqs[i]) for i in idxs]

            # Order chronologically by the episode range embedded in the name
            # (e.g. "...EP01-21..." -> 1), so the dropdown lists reels in
            # story order and the auto-selected default is whichever reel
            # actually starts lowest — not just whatever order the project
            # bin happens to return. Names with no parseable EP number sort
            # to the end rather than guessing.
            def _ep_sort_key(name):
                match = re.search(r'EP(\d{2,})', name, re.IGNORECASE)
                return int(match.group(1)) if match else float("inf")
            stringout.sort(key=lambda pair: _ep_sort_key(pair[0]))

            if self._pp_connect_cancel:
                # STOP already reverted everything synchronously (see
                # _pp_connect's on_stop) — the ExtendScript call above
                # still ran to completion in the background regardless,
                # but its result is stale now, so just drop it.
                return

            if not stringout:
                # Zero STRINGOUT timelines left isn't a connection failure —
                # it's exactly the scenario Skip Nest exists for (everything
                # already nested in a previous session). Treat this as a
                # successful connect with nothing to nest, not an error.
                self._pp_log("Connected. No STRINGOUT timelines found.", "success")
                self.after(0, self._stop_thinking)
                self.after(0, lambda: self._connect_status.config(text=""))
                self._pp_connected = True
                self.after(0, self._enable_reset_btn)
                self.after(0, lambda: self._set_btn_state(self.btn_pp_connect, True))
                self.after(0, lambda: self._pp_mute_check.set_enabled(True))
                self.after(0, lambda: self._pp_nest_all_check.set_enabled(True))
                if not self._pp_skip_nest_mode:
                    self.after(0, lambda: self._set_btn_state(self.btn_pp_skip_nest, True))
                _status("✓ Connected — no STRINGOUT timelines found. Use Skip Nest "
                        "if episodes are already nested.", "#50e050")
                return

            self._pp_log(f"Connected. Found {len(stringout)} STRINGOUT timeline(s): "
                         f"{', '.join(name for name, _ in stringout)}.", "success")
            self.after(0, lambda: self._pp_show_timeline_dropdown(stringout))
            # _pp_connect()'s own _start_thinking() call (before this
            # background thread even started) still needs ITS OWN
            # matching _stop_thinking() here — _thinking_depth is a
            # reentrant counter (see _start_thinking's docstring), so
            # this decrement doesn't cut anything short: it just cancels
            # out this call's own increment. _pp_show_timeline_dropdown
            # triggers _pp_prescan_all_reels right after this, which
            # does its OWN separate _start_thinking()/_stop_thinking()
            # pair for the "SCANNING" period — omitting this one used to
            # permanently leave depth at 1 after every successful
            # connect, since nothing else was ever going to cancel out
            # THIS call's own increment (seen live: thinking dots/STOP
            # never fully clearing again for the rest of the session,
            # no matter what finished afterward).
            self.after(0, self._stop_thinking)
            self.after(0, lambda: self._connect_status.config(text=""))

        except ImportError:
            if self._pp_connect_cancel:
                return
            self.after(0, self._stop_thinking)
            self.after(0, self._restore_reset_btn)
            self.after(0, lambda: self._connect_status.config(text=""))
            self.after(0, lambda: self._set_btn_state(self.btn_pp_connect, True))
            _status("✗ Pymiere not installed. Run: pip install pymiere", TEXT_ERROR)
        except Exception as e:
            if self._pp_connect_cancel:
                return
            self.after(0, self._stop_thinking)
            self.after(0, self._restore_reset_btn)
            self.after(0, lambda: self._connect_status.config(text=""))
            self.after(0, lambda: self._set_btn_state(self.btn_pp_connect, True))
            _status(f"✗ Could not connect: {e}", TEXT_ERROR)

    def _pp_show_timeline_dropdown(self, stringout_pairs):
        """Replace the Connect to Premiere button with a persistent dropdown
        of every STRINGOUT timeline — stays in place (and keeps growing with
        newly discovered timelines on re-connect) until RESET ALL.

        Full timeline names are usually too long to fit, so the dropdown
        shows "REEL 1", "REEL 2", ... (numbered by the chronological EP-order
        sort already applied) instead — the real name still shows in the
        status line once a reel is selected."""
        reel_labels = [f"REEL {i + 1}" for i in range(len(stringout_pairs))]
        self._pp_stringout_map = dict(zip(reel_labels, stringout_pairs))
        self._pp_build_auto_track_boxes()
        self._pp_build_manual_track_boxes()

        if getattr(self, "_pp_timeline_dropdown", None) is None:
            self.btn_pp_connect.destroy()
            self._pp_timeline_var = tk.StringVar()
            # width=170 matches "Connect to Premiere"/"Rescan Reels", the
            # other two widgets that can occupy this same slot — so nothing
            # after it shifts when they swap.
            self._pp_timeline_dropdown = self._canvas_dropdown(
                self._pp_p1_row, self._pp_timeline_var, width=170,
                placeholder="Select timeline...", command=self._pp_on_timeline_selected)
            self._pp_timeline_dropdown.pack(side="left", padx=(0, 14), before=self._pp_circ_p1_2)

        self._pp_timeline_dropdown.set_options(reel_labels)
        self._pp_connected = True
        if self._thinking_active and getattr(self, "_thinking_owns_reset_btn", False):
            # Thinking (STOP) is still active from Connect and — for the
            # normal case below — about to be handed straight to
            # _pp_prescan_all_reels for the "SCANNING" period, which should
            # keep the button as STOP the whole time. _enable_reset_btn()
            # forces the button's text/bindings back to "RESET ALL" right
            # now, which _start_thinking()'s re-entrancy guard then treats
            # as "already handled" once prescan calls it — leaving the
            # button stuck on RESET ALL for the rest of the scan. Only mark
            # this tab's reset as meaningful for now; _stop_thinking() (via
            # _restore_reset_btn) redraws it for real once scanning
            # actually finishes.
            self._reset_armed[self._active_tab] = True
        else:
            self._enable_reset_btn()
        # Mute Master Clips/Nest All deliberately NOT enabled here — they'd
        # be clickable while every reel is still being pre-scanned
        # (dropdown shows "SCANNING"). _pp_finish_initial_scan enables them
        # once that background scan actually finishes.
        if not self._pp_skip_nest_mode:
            self._set_btn_state(self.btn_pp_skip_nest, True)

        if not self._pp_timeline_var.get() and reel_labels:
            # Real reel label + the "connected" checkmark both wait for
            # _pp_finish_initial_scan, once every reel has actually been
            # pre-loaded — showing "REEL 1" as already selectable (and the
            # step marked done) while it's still disabled mid-scan was confusing.
            self._pp_timeline_var.set("SCANNING")
            first_name, _ = stringout_pairs[0]
            # Show Info autofill normally happens inside _pp_arm_timeline,
            # which this flow bypasses (a single background task pre-loads
            # every reel instead — see _pp_prescan_all_reels — so nothing
            # here ever runs two Premiere-scanning threads at once).
            if not self._pp_show_info_locked:
                self._pp_autofill_from_name(first_name)
                self._pp_show_info_locked = True
                self._pp_nest_status.config(text="Detecting output folder...", fg=TEXT_MUTED)
                self._pp_detect_output()
                sc = self.pp_show_code.get().strip()
                ac = self.pp_acronym.get().strip()
                pill_text = f"{sc}_{ac}" if sc and ac else sc
                if pill_text:
                    self._update_show_pill(pill_text, tab="test")
            # Scans and displays Reel 1 first, then keeps going through
            # every other reel in the background — see
            # _pp_prescan_all_reels.
            self._pp_prescan_all_reels(arm_first=True)

    def _pp_on_timeline_selected(self, label):
        pair = self._pp_stringout_map.get(label)
        if pair is None:
            return
        name, seq = pair
        self._pp_arm_timeline(seq, name, reel_label=label)

    def _pp_arm_timeline(self, seq, name, reel_label=None):
        """Called whenever a timeline is picked from the dropdown — either a
        brand new one (scans it fresh) or one already nested/pending this
        session (just re-displays its existing reel)."""
        self._pp_seq = seq
        seq_id = seq._pymiere_id

        if not self._pp_show_info_locked:
            self._pp_autofill_from_name(name)
            self._pp_show_info_locked = True
            self._pp_nest_status.config(text="Detecting output folder...", fg=TEXT_MUTED)
            self._pp_detect_output()
            sc = self.pp_show_code.get().strip()
            ac = self.pp_acronym.get().strip()
            pill_text = f"{sc}_{ac}" if sc and ac else sc
            if pill_text:
                self._update_show_pill(pill_text, tab="test")

        existing_idx = next((i for i, r in enumerate(self._pp_reels) if r["seq_id"] == seq_id), None)
        if existing_idx is not None:
            self._pp_arm_existing_reel(existing_idx)
            return

        self._pp_nest_status.config(text=f"Scanning {name}...", fg=TEXT_MUTED)
        self._pp_track_idx = None
        self._pp_tails_tc = None
        self._pp_scan_cancel = False
        # A STOP click during an EARLIER batch prescan (_pp_prescan_all_reels_task)
        # leaves this True — _pp_scan_tracks's per-track loop checks it too
        # (not just _pp_scan_cancel), so without resetting it here, picking
        # a fresh, not-yet-scanned reel right after that STOP would trip
        # the abort check on its very first track and bail almost
        # instantly — before the STOP takeover below even got a chance to
        # render, let alone actually cancel anything of THIS scan's own.
        self._pp_prescan_abort = False
        # Whatever the dropdown showed before this scan started — restored
        # by STOP if the editor cancels partway through (see below)
        # instead of falling back to a full RESET ALL.
        prev_display = self._pp_timeline_var.get() if getattr(self, "_pp_timeline_var", None) is not None else ""
        if getattr(self, "_pp_timeline_var", None) is not None:
            self._pp_timeline_var.set("SCANNING")
        self._pp_set_timeline_dropdown_enabled(False)

        def _on_stop():
            self._pp_scan_cancel = True
            if getattr(self, "_pp_timeline_var", None) is not None:
                self._pp_timeline_var.set(prev_display)
            self._pp_set_timeline_dropdown_enabled(True)
            self._pp_nest_status.config(text="Stopped.", fg=TEXT_WARN)

        self._start_thinking(on_stop=_on_stop)
        import threading
        threading.Thread(target=lambda: self._pp_scan_tracks(seq, reel_label=reel_label), daemon=True).start()

    def _pp_refresh_nest_button_enabled(self):
        """Nest Episodes should only ever be clickable once the full
        multi-reel prescan batch has finished — not just as soon as the
        first (primary) reel's scan lands. While _pp_prescan_active is
        True, other reels may still be scanning silently in the
        background, and clicking Nest Episodes mid-batch could race that
        thread against the nest thread.

        Also checks _pp_skip_nest_mode — a background prescan started
        before Skip Nest was clicked finishes asynchronously and used to
        call this and blindly re-enable Nest Episodes, undoing Skip
        Nest's lockdown of Phase 1.

        Under Nest All, "clickable" means "something's still left to nest
        anywhere across every reel" — not just whichever reel happens to
        be currently armed. Without this, nesting one reel manually and
        THEN checking Nest All left this stuck disabled forever (the
        armed reel was already done, and the dropdown — the only other
        way to pick a different reel — is locked while Nest All is
        checked, so there was no way out short of unchecking it again).
        Relabels to "Nest Remaining" once at least one reel is already
        done, so it's clear this run picks up from wherever nesting left
        off rather than starting over; reverts to "Nest Episodes" once
        Nest All is unchecked, back to reflecting just the armed reel."""
        if self._pp_skip_nest_mode or self._pp_prescan_active or self._pp_current_reel is None:
            self._set_btn_state(self.btn_pp_run, False)
            return
        if self._pp_nest_all_var.get():
            next_idx = self._pp_find_next_unfinished_reel(0)
            if next_idx is None:
                self._set_btn_state(self.btn_pp_run, False)
                return
            any_done = any(r["nest_done"] for r in self._pp_reels)
            self.btn_pp_run._text = "Nest Remaining" if any_done else "Nest Episodes"
            self.btn_pp_run._command = self._pp_run_autonest
            self._set_btn_state(self.btn_pp_run, True)
            return
        reel = self._pp_reels[self._pp_current_reel]
        self.btn_pp_run._text = "Nest Episodes"
        self.btn_pp_run._command = self._pp_run_autonest
        self._set_btn_state(self.btn_pp_run, not reel["nest_done"])

    def _pp_arm_existing_reel(self, idx):
        """Re-display a timeline already scanned (and possibly nested or
        reset) earlier this session — no re-scan needed."""
        self._pp_current_reel = idx
        reel = self._pp_reels[idx]
        self._pp_seq = reel["seq"]
        self._pp_refresh_chip_visibility()
        self._pp_track_idx = reel.get("track_idx")
        # Auto/Manual mode boxes no longer need a "current reel" sync —
        # every reel's box already reflects its own scan result live
        # (see _pp_refresh_auto_track_box/_pp_refresh_manual_track_box),
        # regardless of which one is armed here.
        # Keep the dropdown's displayed label in sync even when it's locked
        # (Nest All arms reels programmatically, without a direct dropdown click).
        if getattr(self, "_pp_timeline_var", None) is not None:
            self._pp_timeline_var.set(reel.get("reel_label", ""))
        self.pp_start_ep.set(str(reel["start_ep"]))
        self._pp_nest_status.config(text=f"✓ Selected — {reel['seq_name']}", fg="#50e050")
        self._pp_sync_ep_entry_enabled()
        self.btn_pp_run._text = "Nest Episodes"
        self.btn_pp_run._command = self._pp_run_autonest
        self._pp_refresh_nest_button_enabled()
        self._set_btn_state(self.btn_pp_reset_nest, True)
        self._pp_set_circle_active(self._pp_circ_p1_2, "2")
        if reel["nest_done"]:
            self._pp_set_circle_done(self._pp_circ_p1_2)
        self._pp_resize_window()

    def _pp_collect_title_clips(self, seq, track_idx):
        """Read-only scan: the sorted list of title-card clip objects on the
        given track. Shared by chip pre-population and the real nest loop so
        the two can never disagree on episode count or order."""
        title_track = seq.videoTracks[track_idx]
        num_clips = title_track.clips.numItems
        if num_clips == 0:
            return []
        all_clips = [title_track.clips[i] for i in range(num_clips)]
        # Filter to just the title cards when present, matching the same two
        # patterns used to identify this track in the first place: clips
        # named "EPISODE...", or Motion Graphics Template clips (which
        # ExtendScript reports as "Graphic" regardless of the custom text
        # shown on the timeline). Falls back to using every clip only when
        # neither pattern matches anything.
        episode_named = [c for c in all_clips
                          if str(c.name or "").upper().startswith("EPISODE")]
        graphic_named = [c for c in all_clips
                          if str(c.name or "").strip().upper() == "GRAPHIC"]
        title_clips = episode_named or graphic_named or all_clips
        title_clips.sort(key=lambda c: int(c.start.ticks))
        return title_clips

    def _pp_rescan_current_reel(self, seq, silent=False, track_display=None, manage_thinking=True,
                                 reel_label=None):
        """(Re)scan the current, not-yet-nested reel's title cards on
        whichever track is now selected. Registers a brand new reel, or
        replaces an unstarted one's placeholder chips in place if the track
        choice changed before Nest Episodes was ever clicked.

        silent=True means this reel isn't the one being displayed (a
        background pre-load of some OTHER reel, e.g. via
        _pp_prescan_all_reels) — its data still gets stored, and its chips
        still get built (just hidden, per the normal visibility rules), but
        nothing about the currently-displayed reel's widgets is touched.
        manage_thinking=False means the caller owns its own thinking-dots
        start/stop bracket (used when this is one of several scans in a
        batch, e.g. _pp_prescan_all_reels_task) — an individual scan inside
        that batch shouldn't stop the animation while its siblings are
        still running.

        May be called from a background thread (manual track re-pick) or
        already-backgrounded scan (initial connect) — all Premiere/ExtendScript
        reads happen directly here, but every widget mutation is deferred into
        one self.after(0, _apply) closure so it always lands on the main thread
        regardless of which thread called this."""
        track_idx = self._pp_track_idx
        tails_tc = self._pp_tails_tc
        try:
            if track_idx is None:
                if not silent:
                    self.after(0, lambda: self._pp_nest_status.config(
                        text="✗ No title card track selected.", fg=TEXT_ERROR))
                else:
                    self._pp_log("⚠ Background scan skipped — no title card track detected.", "warn")
                if manage_thinking:
                    self.after(0, self._stop_thinking)
                return
            title_clips = self._pp_collect_title_clips(seq, track_idx)
            total = len(title_clips)
            if total == 0:
                if not silent:
                    self.after(0, lambda: self._pp_nest_status.config(
                        text="✗ No title cards found on selected track.", fg=TEXT_ERROR))
                else:
                    self._pp_log("⚠ Background scan skipped — no title cards found.", "warn")
                if manage_thinking:
                    self.after(0, self._stop_thinking)
                return

            def _apply():
                if self._pp_skip_nest_mode or not self._pp_connected or self._pp_prescan_abort or self._pp_scan_cancel:
                    # Skip Nest was clicked, RESET ALL ran, Reset Nest
                    # interrupted a still-running prescan (without
                    # disconnecting — see _pp_prescan_abort), or STOP
                    # cancelled just this one scan (see _pp_scan_cancel),
                    # while this background scan was still in flight —
                    # don't let a late-arriving result repopulate anything.
                    # Unlike the other three reasons, a scan cancel doesn't
                    # get its own stop_thinking() call anywhere else, so it
                    # needs one here.
                    if self._pp_scan_cancel and manage_thinking:
                        self._stop_thinking()
                    return
                # Looked up by seq_id across every known reel, NOT just
                # whichever one happens to be currently armed/displayed —
                # a Manual-mode track re-pick can target ANY reel's box,
                # not only self._pp_current_reel's. Comparing against only
                # the current reel used to silently miss an existing
                # match, falling into the "new reel" branch below and
                # appending a DUPLICATE entry for a reel that already
                # existed (seen live: picking a track for a reel other
                # than the one showing in the dropdown).
                existing_idx = next((i for i, r in enumerate(self._pp_reels)
                                      if r["seq_id"] == seq._pymiere_id), None)
                cur = self._pp_reels[existing_idx] if existing_idx is not None else None
                if cur is not None:
                    self._pp_destroy_reel_chips(cur)
                    cur.update(track_idx=track_idx, tails_tc=tails_tc,
                               title_clips=title_clips, total=total, done=[False] * total,
                               resume_idx=0, nest_done=False, setup_done=False,
                               track_display=track_display)
                    self._pp_build_reel_chips(cur)
                    self._pp_refresh_manual_track_box(cur.get("reel_label"), cur)
                    self._pp_refresh_auto_track_box(cur.get("reel_label"), cur)
                    if not silent:
                        self._pp_current_reel = existing_idx
                else:
                    next_ep = max((r["start_ep"] + r["total"] for r in self._pp_reels),
                                  default=int(self.pp_start_ep.get() or 1))
                    # reel_label is a real closure-captured parameter, not
                    # read from a shared instance attribute at apply-time
                    # — a batch prescan (_pp_prescan_all_reels_task) races
                    # ahead to the next reel's scan on the same background
                    # thread well before this deferred _apply() necessarily
                    # runs on the main thread, so a shared scratch attribute
                    # could already say a LATER reel's label by the time
                    # this reads it, scrambling which "REEL N" header each
                    # reel actually ends up under (append order/episode
                    # numbering were always correct — only the label text
                    # was racing).
                    this_reel_label = reel_label or f"REEL {len(self._pp_reels) + 1}"
                    new_reel = {"seq": seq, "seq_id": seq._pymiere_id, "seq_name": seq.name,
                                "track_idx": track_idx, "tails_tc": tails_tc,
                                "start_ep": next_ep, "original_start_ep": next_ep,
                                "title_clips": title_clips, "total": total,
                                "chips": [], "done": [False] * total, "resume_idx": 0, "nest_done": False,
                                "reel_label": this_reel_label, "label_widget": None, "setup_done": False,
                                "track_display": track_display}
                    self._pp_reels.append(new_reel)
                    self._pp_build_reel_chips(new_reel)
                    self._pp_refresh_manual_track_box(this_reel_label, new_reel)
                    self._pp_refresh_auto_track_box(this_reel_label, new_reel)
                    if not silent:
                        self._pp_current_reel = len(self._pp_reels) - 1
                        self.pp_start_ep.set(str(next_ep))

                self._pp_refresh_chip_visibility()
                if silent:
                    return
                self._pp_seq = seq
                self._pp_nest_status.config(text=f"✓ Connected — {seq.name}", fg="#50e050")
                self._pp_sync_ep_entry_enabled()
                self.btn_pp_run._text = "Nest Episodes"
                self.btn_pp_run._command = self._pp_run_autonest
                self._pp_refresh_nest_button_enabled()
                self._set_btn_state(self.btn_pp_reset_nest, True)
                # Step circle 1 (checkmark), step circle 2 (active), the
                # dropdown's "SCANNING" placeholder, and its lock all
                # declare the SAME thing — "Connect to Premiere is truly
                # finished" — so they're gated together.
                #
                # Gated on _pp_prescan_active: an eager batch prescan
                # (_pp_prescan_all_reels_task) calls this non-silently for
                # its FIRST reel only, while later reels' title-card tracks
                # are still being scanned in the background on the same
                # thread — declaring the step done/restoring the dropdown
                # here used to show a checkmark (and let a reel get picked)
                # mid-batch, before every reel (and its track scan) was
                # actually done. That task's own _finish() (via
                # _pp_finish_initial_scan) is the sole authority for both
                # once the whole batch is genuinely finished; skip all of
                # it here while it's still active.
                if not self._pp_prescan_active:
                    self._pp_set_circle_done(self._pp_circ_p1_1)
                    self._pp_set_circle_active(self._pp_circ_p1_2, "2")
                    if getattr(self, "_pp_timeline_var", None) is not None and self._pp_current_reel is not None:
                        self._pp_timeline_var.set(self._pp_reels[self._pp_current_reel].get("reel_label", ""))
                    self._pp_set_timeline_dropdown_enabled(True)
                if manage_thinking:
                    self._stop_thinking()
                self._pp_resize_window()
            self.after(0, _apply)
        except Exception as e:
            import traceback
            traceback.print_exc()
            if not silent:
                self.after(0, lambda err=str(e): self._pp_nest_status.config(
                    text=f"✗ Scan failed: {err}", fg=TEXT_ERROR))
                # Same reasoning as the success path above — a failed
                # scan shouldn't leave the dropdown stuck on "SCANNING"
                # and locked either.
                self.after(0, lambda: self._pp_set_timeline_dropdown_enabled(True))
                if getattr(self, "_pp_timeline_var", None) is not None and self._pp_current_reel is not None:
                    self.after(0, lambda: self._pp_timeline_var.set(
                        self._pp_reels[self._pp_current_reel].get("reel_label", "")))
            else:
                self._pp_log(f"⚠ Background scan of a reel failed: {e}", "warn")
            if manage_thinking:
                self.after(0, self._stop_thinking)

    def _pp_build_reel_chips(self, reel):
        """Create pending (grey) chips for a reel that doesn't have any yet —
        appended as a new row after whatever's already in the chip box, with
        a small "REEL N" header above them (same styling/color as a disabled
        chip) so multiple reels' episodes stay visually distinguishable."""
        label = reel.get("reel_label")
        if label:
            lbl = tk.Label(self._pp_nest_chips_frame, text=label,
                            font=("SF Pro Display", 10, "bold"),
                            bg="#252525", fg="#888888", anchor="w")
            lbl.pack(anchor="w", pady=(2, 2))
            reel["label_widget"] = lbl
        tw, th, r, gap = 54, 24, 6, 5
        cw, ch = tw + 2, th + 2
        avail = max(self._pp_nest_chips_frame.winfo_width() - 16, 800)
        per_row = max(1, avail // (cw + gap))
        chips = []
        row = None
        for i in range(reel["total"]):
            if i % per_row == 0:
                row = tk.Frame(self._pp_nest_chips_frame, bg="#252525")
                row.pack(anchor="w", pady=(0, 2))
            ep_num = reel["start_ep"] + i
            c = tk.Canvas(row, width=cw, height=ch, bg="#252525", highlightthickness=0)
            c.pack(side="left", padx=(0, gap), pady=2)
            x1, y1, x2, y2 = 1, 1, tw + 1, th + 1
            pts = [x1+r,y1, x2-r,y1, x2,y1, x2,y1+r,
                   x2,y2-r, x2,y2, x2-r,y2, x1+r,y2,
                   x1,y2, x1,y2-r, x1,y1+r, x1,y1]
            c.create_polygon(pts, fill="#3a3a3a", outline="", smooth=True, tags="bg")
            c.create_text(cw//2, ch//2, text=f"EP{ep_num:02d}",
                          font=("SF Pro Display", 10, "bold"), fill="#888888", tags="txt")
            chips.append(c)
        reel["chips"] = chips
        self._pp_resize_window()

    def _pp_destroy_reel_chips(self, reel):
        if reel.get("label_widget") is not None:
            reel["label_widget"].destroy()
            reel["label_widget"] = None
        rows = []
        for c in reel.get("chips", []):
            if c.master not in rows:
                rows.append(c.master)
        for r in rows:
            r.destroy()
        reel["chips"] = []

    def _pp_set_reel_chip_active(self, reel, idx):
        if idx < len(reel["chips"]):
            c = reel["chips"][idx]
            c.itemconfig("bg", fill="#1a3a12", outline="#6dff6d")
            c.itemconfig("txt", fill="#8dff8d")

    def _pp_set_reel_chip_done(self, reel, idx):
        if idx < len(reel["chips"]):
            c = reel["chips"][idx]
            c.itemconfig("bg", fill="#1a3a1a", outline="")
            c.itemconfig("txt", fill="#5ae05a")

    def _pp_set_reel_chip_pending(self, reel, idx):
        if idx < len(reel["chips"]):
            c = reel["chips"][idx]
            c.itemconfig("bg", fill="#3a3a3a", outline="")
            c.itemconfig("txt", fill="#888888")

    def _pp_refresh_chip_visibility(self):
        """When Nest All is unchecked, only the currently-selected reel's
        chips are shown — switching reels via the dropdown replaces the
        display instead of accumulating. Other reels' widgets/data are just
        hidden (pack_forget), not destroyed, so switching back is instant.
        When Nest All is checked, every reel's chips show at once. Does not
        affect EXPORT QUEUE chips, which always accumulate regardless."""
        show_all = self._pp_nest_all_var.get()
        for reel in self._pp_reels:
            lbl = reel.get("label_widget")
            if lbl is not None:
                lbl.pack_forget()
            for r in self._pp_reel_chip_rows(reel):
                r.pack_forget()
        for i, reel in enumerate(self._pp_reels):
            if not (show_all or i == self._pp_current_reel):
                continue
            lbl = reel.get("label_widget")
            if lbl is not None:
                lbl.pack(anchor="w", pady=(2, 2))
            for r in self._pp_reel_chip_rows(reel):
                r.pack(anchor="w", pady=(0, 2))
        self._pp_resize_window()

    def _pp_reel_chip_rows(self, reel):
        rows = []
        for c in reel.get("chips", []):
            if c.master not in rows:
                rows.append(c.master)
        return rows

    def _pp_set_timeline_dropdown_enabled(self, enabled):
        if getattr(self, "_pp_timeline_dropdown", None) is not None:
            self._pp_timeline_dropdown.set_locked(not enabled)

    def _pp_sync_ep_entry_enabled(self):
        """Starting Episode # is only editable when a specific reel is armed
        and nothing else is controlling numbering for you (Nest All, or
        having bypassed nesting entirely via Skip Nest). Single source of
        truth, called from every place that could change these conditions,
        instead of scattered .config() calls that could drift out of sync
        — e.g. unchecking Nest All mid-run used to leave this stuck
        disabled forever since nothing re-evaluated it once the run ended."""
        editable = (self._pp_current_reel is not None
                    and not self._pp_nest_all_var.get()
                    and not self._pp_skip_nest_mode)
        if editable:
            self._pp_ep_entry.config(state="normal", fg=TEXT_PRIMARY, bg=BG_INPUT)
        else:
            self._pp_ep_entry.config(state="disabled")

    def _pp_on_start_ep_edited(self):
        """Hand-editing Starting Episode # immediately relabels the current
        reel's not-yet-nested chips to match, instead of only taking effect
        once Nest Episodes is actually clicked. Already-nested chips are
        left alone — their real Premiere sequence names are already baked
        in with the old numbering, so relabeling their display would lie."""
        if self._pp_current_reel is None:
            return
        reel = self._pp_reels[self._pp_current_reel]
        try:
            new_start = int(self.pp_start_ep.get().strip())
        except ValueError:
            return
        if new_start == reel["start_ep"]:
            return
        reel["start_ep"] = new_start
        for i, c in enumerate(reel["chips"]):
            if not reel["done"][i]:
                c.itemconfig("txt", text=f"EP{new_start + i:02d}")

    def _pp_on_nest_all_toggled(self):
        """Nest All makes timeline selection moot — lock the dropdown while
        it's checked. No scan needed here: every reel is already known by
        the time this checkbox is even clickable — it stays disabled until
        _pp_finish_initial_scan's eager connect-time prescan has fully
        finished (see _pp_prescan_all_reels), which now always scans
        every reel up front rather than lazily on selection."""
        checked = self._pp_nest_all_var.get()
        if checked:
            self._pp_set_timeline_dropdown_enabled(False)
            self._pp_sync_ep_entry_enabled()
        elif not self._pp_nesting_active:
            self._pp_set_timeline_dropdown_enabled(True)
            self._pp_sync_ep_entry_enabled()
            # Safety net: make sure whatever the dropdown visibly shows is
            # actually the armed/displayed reel (prescans are always
            # silent and never change this on their own, but re-arm here
            # anyway in case something else left them out of sync).
            label = getattr(self, "_pp_timeline_var", None)
            label = label.get() if label is not None else None
            pair = self._pp_stringout_map.get(label) if label else None
            if pair is not None:
                _, seq = pair
                idx = next((i for i, r in enumerate(self._pp_reels)
                            if r["seq_id"] == seq._pymiere_id), None)
                if idx is not None and idx != self._pp_current_reel:
                    self._pp_arm_existing_reel(idx)
        # Re-evaluate Nest Episodes' enabled state/label against the new
        # Nest All setting — e.g. a reel nested manually before Nest All
        # was ever checked left this stuck disabled otherwise (see
        # _pp_refresh_nest_button_enabled).
        self._pp_refresh_nest_button_enabled()
        self._pp_refresh_chip_visibility()

    def _pp_prescan_all_reels(self, arm_first=False):
        """Background-scans every STRINGOUT timeline not yet armed as a
        reel, in the same chronological order the dropdown lists them,
        always eagerly through the whole list regardless of Nest All —
        every reel's title-card track, episode count, and tails are all
        known right after Connect to Premiere/Rescan Reels, not scanned
        lazily on demand. (An earlier on-demand-per-selection version of
        this made picking a track in Manual mode for a reel other than
        the currently-armed one meaningfully more complex than it needed
        to be, and made Nest All's own eager top-up scan a separate,
        redundant code path with its own bug surface — simpler and more
        predictable to just always scan everything up front once.)

        All scans here run strictly one at a time on a single background
        thread — deliberately, since scanning two sequences concurrently
        from separate threads is untested territory for the Premiere
        scripting bridge. The dropdown is locked for the duration so a
        manual pick can't spawn a second scan thread mid-prescan; it's
        restored to whatever the dropdown should actually be once this
        finishes. arm_first=True displays whichever reel is scanned
        first (used on initial connect/Rescan Reels) — every other reel
        in the list still gets scanned right after it, just silently
        (background, no screen-yanking).

        Re-entrancy guarded: Nest All is enabled the moment the dropdown
        appears, which is before the initial connect-time prescan (this
        same method, arm_first=True) has finished — checking it mid-scan
        used to start a second overlapping prescan thread, and since each
        thread's self._pp_reels append lands asynchronously via
        self.after(0, ...), both threads could race past the "already
        scanned" check for the same reel and append it twice (seen live
        as duplicate "REEL 1" chip rows)."""
        if not self._pp_stringout_map or self._pp_prescan_active:
            return
        # A previous prescan's Reset Nest interruption doesn't stay
        # armed for the NEXT one — see _pp_prescan_abort.
        self._pp_prescan_abort = False
        self._pp_set_timeline_dropdown_enabled(False)

        def _on_stop():
            # STOP's abort for this batch — reuses the same interrupt
            # flag Reset Nest already uses (see _pp_reset_nest/
            # _pp_prescan_abort); the loop below and _pp_scan_tracks's
            # own mid-track check both already honor it, and the
            # finally block below always runs _stop_thinking() (and
            # re-enables/relabels the dropdown appropriately) regardless
            # of how the loop actually exited, so nothing further is
            # needed here.
            self._pp_prescan_abort = True
            # Nest All implies "the full episode plan across every reel
            # is known" — if this scan got cut short, that's no longer
            # true, so leaving the checkbox checked would be a lie.
            if self._pp_nest_all_var.get():
                self._pp_nest_all_var.set(False)
                self._pp_nest_all_check.refresh()
                self._pp_on_nest_all_toggled()

        # Called synchronously here — every caller of this method
        # (Connect to Premiere's _pp_show_timeline_dropdown, Rescan
        # Reels) already runs on the main thread — rather than deferred
        # via self.after(0, ...) from inside the background task below.
        # Deferring it used to race against Connect to Premiere's own
        # _stop_thinking() call, queued right after this same launcher
        # returns from _pp_connect_task's OWN background thread: if
        # that already-queued _stop_thinking() ran before the newly
        # spawned prescan thread's deferred _start_thinking() request
        # even landed in Tk's queue, _thinking_depth could hit 0 and
        # tear the STOP takeover down immediately — visible live as
        # STOP flickering back to RESET ALL and STAYING there for the
        # rest of the scan (reproduced in
        # test_stop_flicker_during_connect_handoff.py). Calling it here
        # makes the ordering deterministic instead of a timing race.
        self._start_thinking(on_stop=_on_stop)
        import threading
        threading.Thread(target=self._pp_prescan_all_reels_task, args=(arm_first,), daemon=True).start()

    def _pp_prescan_all_reels_task(self, arm_first=False):
        self._pp_prescan_active = True
        try:
            primary_done = not arm_first
            for label, (name, seq) in list(self._pp_stringout_map.items()):
                if not self._pp_connected or self._pp_skip_nest_mode or self._pp_prescan_abort:
                    # RESET ALL ran while this was still working through
                    # the list (stop rather than repopulating self._pp_reels,
                    # which the reset just emptied, with stale scans), Skip
                    # Nest was clicked mid-scan — Phase 1's reel model is
                    # moot now, so stop making real ExtendScript calls to
                    # scan reels whose results would just get discarded
                    # anyway (see the _pp_skip_nest_mode check in
                    # _pp_rescan_current_reel's _apply()) — or Reset Nest
                    # interrupted this without disconnecting at all (see
                    # _pp_prescan_abort).
                    break
                seq_id = seq._pymiere_id
                if any(r["seq_id"] == seq_id for r in self._pp_reels):
                    continue
                self._pp_track_idx = None
                self._pp_tails_tc = None
                self.after(0, lambda l=label: self._pp_nest_status.config(
                    text=f"Scanning {l} for title card track...", fg=TEXT_MUTED))
                # Passed as a real parameter, not the old shared
                # self._pp_pending_reel_label scratch attribute — this
                # loop moves on to the next reel (on this same background
                # thread) well before the deferred _apply() for THIS one
                # necessarily runs on the main thread, so a shared
                # attribute could already say a later reel's label by the
                # time it got read, scrambling which "REEL N" header each
                # reel ended up under.
                self._pp_scan_tracks(seq, silent=primary_done, manage_thinking=False, reel_label=label)
                primary_done = True
        finally:
            self._pp_prescan_active = False

            def _finish():
                # All three land in the same callback (one paint cycle)
                # deliberately — previously scheduled as three separate
                # self.after(0, ...) calls, which left a visible gap
                # where e.g. the REEL dropdown was already clickable but
                # Nest Episodes hadn't enabled yet, or STOP had already
                # flipped back to RESET ALL a beat before either of
                # those. Now RESET ALL restores, Nest Episodes/circle
                # done, and the dropdown unlocking all happen together.
                self._stop_thinking()
                if arm_first:
                    self._pp_finish_initial_scan()
                else:
                    # arm_first's path re-evaluates this itself via
                    # _pp_finish_initial_scan — this covers the other case
                    # (Nest All's silent top-up scan), which otherwise never
                    # re-checked Nest Episodes' enabled state/label once new
                    # reels landed.
                    self._pp_refresh_nest_button_enabled()
                # Don't blindly re-unlock — a Skip Nest click mid-scan
                # already locked this down deliberately, and this
                # callback (queued before that click, running after it)
                # shouldn't undo it.
                self._pp_set_timeline_dropdown_enabled(
                    not self._pp_nest_all_var.get() and not self._pp_skip_nest_mode)
            self.after(0, _finish)

    def _pp_finish_initial_scan(self):
        """Called once the connect-time pre-load of EVERY reel — not just
        the first one — has finished. Swaps the dropdown's "SCANNING"
        placeholder for the actually-armed reel's real label, and only
        now marks Connect to Premiere's step circle done (checkmark) and
        Nest Episodes' circle active — showing the checkmark the moment
        the whole batch genuinely finished, not the instant the first
        reel's own scan happened to land while its siblings were still
        being scanned in the background (see _pp_rescan_current_reel's
        _apply(), which used to do exactly that)."""
        self._pp_set_circle_done(self._pp_circ_p1_1)
        if self._pp_current_reel is not None and getattr(self, "_pp_timeline_var", None) is not None:
            reel = self._pp_reels[self._pp_current_reel]
            self._pp_timeline_var.set(reel.get("reel_label", ""))
            self._pp_nest_status.config(text=f"✓ Connected — {reel.get('seq_name', '')}", fg="#50e050")
            self._pp_set_circle_active(self._pp_circ_p1_2, "2")
        elif getattr(self, "_pp_timeline_var", None) is not None:
            # STOP cancelled the very first reel's scan before anything
            # ever got armed — don't leave the dropdown stuck reading
            # "SCANNING" forever with nothing to show for it.
            self._pp_timeline_var.set("")
        # Whole prescan batch is done now (_pp_prescan_active just flipped
        # back to False) — only now is it actually safe to enable Nest
        # Episodes, since other reels are no longer scanning in the background.
        self._pp_refresh_nest_button_enabled()
        # Same reasoning applies to Mute Master Clips/Nest All — clickable
        # only once scanning is genuinely finished, not while the dropdown
        # still reads "SCANNING".
        if not self._pp_skip_nest_mode:
            self._pp_mute_check.set_enabled(True)
            self._pp_nest_all_check.set_enabled(True)

    def _pp_find_next_unfinished_reel(self, start_from=0):
        """First reel (in chronological order, at or after start_from) that
        still has episodes left to nest — or None if everything's done."""
        for i in range(start_from, len(self._pp_reels)):
            if not self._pp_reels[i]["nest_done"]:
                return i
        return None

    def _pp_scan_tracks(self, seq, silent=False, manage_thinking=True, reel_label=None):
        """Scan tracks — only samples first 3 clips per track for speed."""
        try:
            import pymiere
            num_tracks = seq.videoTracks.numTracks
            candidate_tracks = []
            tails_tc = None
            found_episode_track = None

            # Pass 1: Find episode track (sample 3 clips per track above V8)
            # Also do a quick tails scan on the last clip of each track above V4
            for t in range(num_tracks):
                if self._pp_skip_nest_mode or not self._pp_connected or self._pp_prescan_abort or self._pp_scan_cancel:
                    # Skip Nest was clicked, RESET ALL ran, Reset Nest
                    # interrupted a still-running prescan (without
                    # disconnecting — see _pp_prescan_abort), or STOP
                    # cancelled just this one scan (see _pp_scan_cancel),
                    # mid-scan —
                    # the batch loop in _pp_prescan_all_reels_task only
                    # checks this between reels, so without this a reel
                    # with a lot of tracks could keep making real
                    # ExtendScript calls for several more seconds after the
                    # dropdown already reads "SKIPPED", making the STOP
                    # button/thinking dots look stuck. This reel's result
                    # would just get discarded anyway (see the same check
                    # in _pp_rescan_current_reel's _apply()), so bail now
                    # instead of finishing the scan first.
                    if manage_thinking:
                        self.after(0, self._stop_thinking)
                    return
                track = seq.videoTracks[t]
                n_clips = track.clips.numItems
                if n_clips == 0:
                    continue

                # Check last clip for tails on all tracks above V4
                if t >= 4 and tails_tc is None:
                    last_clip = track.clips[n_clips - 1]
                    last_name = str(last_clip.name) if last_clip.name is not None else ""
                    if "tail" in last_name.lower():
                        tails_tc = int(last_clip.start.ticks)

                # Sample first 3 clips to identify episode title card track
                sample = min(3, n_clips)
                sample_names = []
                sample_durs = []
                for c in range(sample):
                    clip = track.clips[c]
                    name = str(clip.name) if clip.name is not None else ""
                    dur = clip.end.seconds - clip.start.seconds
                    sample_names.append(name)
                    sample_durs.append(dur)

                avg_dur = sum(sample_durs) / len(sample_durs) if sample_durs else 0
                label = f"V{t+1} — {n_clips} clips · avg {avg_dur:.1f}s"
                # Two patterns for a title-card track, both requiring EVERY
                # sampled clip to match (not just one — a single coincidentally
                # matching clip on an unrelated track was enough to false-
                # positive before) plus a clip-count floor so a handful of
                # clips can't outrank the real track:
                #  1. Clips literally named "EPISODE..." — works when the
                #     editor names each title-card clip on the timeline.
                #  2. Clips named exactly "Graphic" and each under 3s — Motion
                #     Graphics Template clips report their template's generic
                #     name ("Graphic") via ExtendScript, not the custom text
                #     visible on the timeline, so name-matching can't see
                #     "EPISODE 22" etc. even though that's what's displayed.
                is_episode_named = (sample_names
                                     and all(n.upper().startswith("EPISODE") for n in sample_names)
                                     and n_clips >= 10)
                is_graphic_title = (sample_names
                                     and all(n.strip().upper() == "GRAPHIC" for n in sample_names)
                                     and avg_dur < 3.0
                                     and n_clips >= 10)
                is_episode = is_episode_named or is_graphic_title

                candidate_tracks.append((t, label, avg_dur, n_clips, is_episode))

                # Among all matches, prefer whichever track's clip count is
                # closest to a typical reel size (~20 episodes) rather than
                # just the first or the largest — a stray short/unrelated
                # graphics-only track could otherwise still outrank the real one.
                # n_clips <= 60 is a hard sanity ceiling, not a tuning knob —
                # a captions/lower-third/graphics track can legitimately use
                # the same "Graphic"-named MOGRT clips as real title cards
                # (see is_graphic_title's comment) but with hundreds of tiny
                # clips instead of a season's worth of episodes; without this,
                # a false-positive on such a track wins by DEFAULT whenever
                # it's the only candidate matching the naming heuristic at
                # all, regardless of how implausible its count is (seen live:
                # a 460-clip captions track got auto-selected as "the episode
                # track" for a reel whose own STRINGOUT filename said EP01-20
                # — 20 real episodes).
                if is_episode and t >= 7 and n_clips <= 60:
                    score = abs(n_clips - 20)
                    if found_episode_track is None or score < abs(found_episode_track[3] - 20):
                        found_episode_track = (t, label, avg_dur, n_clips, sample_names)

            # Pick best track — computed into a local "display" dict rather
            # than written straight to the shared widgets, so a silent
            # (background-prescanned) reel's results can be stored on that
            # reel without ever touching whatever's currently on screen for
            # a different reel. _pp_rescan_current_reel applies this to the
            # live widgets only when the scan isn't silent.
            display = {}
            if found_episode_track:
                best = found_episode_track
                self._pp_track_idx = best[0]
                display["auto_label"] = f"V{best[0]+1} — {best[3]} clips · auto-detected"
            else:
                # Fallback: pick track above V5 with 15-30 clips
                best = None
                for t_idx, label, avg_dur, n_clips, _ in candidate_tracks:
                    if t_idx >= 4 and 15 <= n_clips <= 30:
                        if best is None or n_clips > best[3]:
                            best = (t_idx, label, avg_dur, n_clips, [])
                if best:
                    self._pp_track_idx = best[0]
                    display["auto_label"] = f"V{best[0]+1} — {best[3]} clips · auto-detected"
                else:
                    display["auto_label"] = "Could not auto-detect — please select manually"

            # Manual mode's per-reel picker only offers tracks that
            # actually exist in THIS reel (candidate_tracks already
            # excludes empty ones) and have a plausible clip count —
            # keeps a captions/graphics track with hundreds of clips (or
            # any other track that's obviously not title cards) from
            # ever being pickable at all, not just deprioritized in
            # auto-detection scoring above.
            display["manual_options"] = sorted(
                t_idx for t_idx, _, _, n_clips, _ in candidate_tracks if n_clips <= 30)

            # Tails
            if tails_tc is not None:
                self._pp_tails_tc = tails_tc
                try:
                    zero_ticks = self._pp_get_zero_ticks(seq)
                    tc = self._pp_format_timecodes(seq, [tails_tc + zero_ticks])[0]
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    tc = f"(couldn't format: {e})"
                display["tails_text"] = f"Tail Leader clip found at {tc}"
                display["tails_fg"] = TEXT_MUTED
            else:
                display["tails_text"] = "⚠ No tails clip found — last episode will end at sequence end"
                display["tails_fg"] = TEXT_WARN

            _label = reel_label or seq.name
            self._pp_log(f"{_label}: title card track — {display.get('auto_label', 'not detected')}.",
                         "success" if self._pp_track_idx is not None else "warn")
            self._pp_log(f"{_label}: {display['tails_text']}.",
                         "warn" if tails_tc is None else "muted")

            if not silent:
                self.after(0, lambda: self._connect_status.config(text=""))
            # Track is known now (or manual selection is still pending) —
            # hand off to the shared prescan step, which builds the reel's
            # placeholder chips and unlocks Nest Episodes.
            self._pp_rescan_current_reel(seq, silent=silent, track_display=display,
                                          manage_thinking=manage_thinking, reel_label=reel_label)

        except Exception as e:
            if manage_thinking:
                self.after(0, self._stop_thinking)
            if not silent:
                self.after(0, lambda: self._connect_status.config(text=""))
                self.after(0, lambda err=str(e): self._pp_nest_status.config(
                    text=f"✗ Scan failed: {err}", fg=TEXT_ERROR))
                box = self._pp_track_auto_boxes.get(reel_label) if reel_label else None
                if box is not None:
                    self.after(0, lambda err=str(e): box["hint"].config(
                        text=f"Scan failed: {err}", fg=TEXT_ERROR))
            else:
                self._pp_log(f"⚠ Background scan of a reel failed: {e}", "warn")

    def _pp_detect_output(self):
        """Auto-detect the LIVE, MARKETING, SRT, and TRAILER delivery
        folders. MARKETING and SRT sit next to LIVE inside the same FINAL
        folder; TRAILER is a sibling of FINAL directly under DELIVERY.
        Stops at the first DELIVERY/FINAL combo that yields a LIVE folder
        (matching the original single-folder behavior) but still checks
        for MARKETING/SRT/TRAILER siblings before returning.

        Logs each step the same way the VFX tab's find_output_folder
        does (which volumes matched, what was ultimately found) so a
        failed auto-detect is debuggable from the log instead of the
        Output Folder section just silently staying empty."""
        import glob
        sc = self.pp_show_code.get().strip()
        if not sc:
            return
        import re
        base = re.sub(r'^[A-Z]-', '', sc)
        all_vols = []
        for code in [sc, f"V-{base}", f"I-{base}", base]:
            all_vols.extend(glob.glob(f"/Volumes/{code}_*"))
        all_vols = sorted(set(all_vols))
        self._pp_log(
            f"Output folder auto-detect: show code \"{sc}\" -> "
            f"{len(all_vols)} volume(s) matched" +
            (f" ({', '.join(all_vols)})" if all_vols else ""), "muted")
        if not all_vols:
            self._pp_log("Output folder not found — no matching volume. Please browse to "
                          "select your output folder manually.", "warn")
            return
        for code in [sc, f"V-{base}", f"I-{base}", base]:
            vols = glob.glob(f"/Volumes/{code}_*")
            for vol in sorted(vols):
                edit_folders = glob.glob(f"{vol}/*_EDIT") or glob.glob(f"{vol}/{code}_*_EDIT")
                for edit in sorted(edit_folders):
                    try:
                        delivery = [os.path.join(edit, d) for d in os.listdir(edit)
                                    if "DELIVERY" in d.upper()
                                    and os.path.isdir(os.path.join(edit, d))]
                        for deliv in sorted(delivery):
                            finals = [os.path.join(deliv, d) for d in os.listdir(deliv)
                                      if "FINAL" in d.upper()
                                      and os.path.isdir(os.path.join(deliv, d))]
                            for final in sorted(finals):
                                lives = [os.path.join(final, d) for d in os.listdir(final)
                                         if "LIVE" in d.upper()
                                         and os.path.isdir(os.path.join(final, d))]
                                if not lives:
                                    continue
                                found = lives[0]
                                self.after(0, lambda p=found: self._pp_out_dir.set(p))
                                self._pp_log(f"Output folder: {found}", "success")
                                markets = [os.path.join(final, d) for d in os.listdir(final)
                                           if "MARKETING" in d.upper()
                                           and os.path.isdir(os.path.join(final, d))]
                                if markets:
                                    mfound = markets[0]
                                    self.after(0, lambda p=mfound: self._pp_marketing_out_dir.set(p))
                                    self._pp_log(f"Marketing output folder: {mfound}", "success")
                                srts = [os.path.join(final, d) for d in os.listdir(final)
                                        if "SRT" in d.upper()
                                        and os.path.isdir(os.path.join(final, d))]
                                if srts:
                                    sfound = srts[0]
                                    self.after(0, lambda p=sfound: self._pp_srt_out_dir.set(p))
                                    self._pp_log(f"SRT output folder: {sfound}", "success")
                                trailers = [os.path.join(deliv, d) for d in os.listdir(deliv)
                                            if "TRAILER" in d.upper()
                                            and os.path.isdir(os.path.join(deliv, d))]
                                if trailers:
                                    tfound = trailers[0]
                                    self.after(0, lambda p=tfound: self._pp_trailer_out_dir.set(p))
                                    self._pp_log(f"Trailer output folder: {tfound}", "success")
                                return
                    except Exception:
                        continue
        self._pp_log("Output folder not found — no DELIVERY > FINAL > LIVE folder under any "
                      "matched volume. Please browse to select your output folder manually.", "warn")

    def _pp_autofill_from_name(self, name):
        """Parse timeline name and fill show info."""
        import re
        parts = name.split("_")
        if len(parts) >= 1:
            self.pp_show_code.set(parts[0])
        if len(parts) >= 2:
            self.pp_acronym.set(parts[1])
        date_match = re.search(r'\b(\d{6})\b', name)
        if date_match:
            self.pp_date.set(date_match.group(1))

    def _pp_extract_ep_num(self, name):
        """Episode number embedded in a sequence/chip name (e.g.
        "...EP07..." -> 7), or None if it doesn't match. Matching on this
        instead of the full name is what lets duplicate detection work
        across different nest sessions/dates — "...EP07_260101..." and
        "...EP07_260707..." are the same episode, just nested on different
        days, so a raw string comparison would never catch the overlap."""
        match = re.search(r'EP(\d+)', name, re.IGNORECASE)
        return int(match.group(1)) if match else None

    def _pp_scan_project_final_ep_seqs(self):
        """Every sequence with "FINAL_EP" in its name that actually lives
        inside the DELIVERY > FINAL bin — not the whole project — since
        that's the only place nesting ever puts things (see
        sub.projectItem.moveBin(output_bin) in the nest loop). Scoping to
        that bin specifically avoids a false collision from some unrelated
        "FINAL_EP"-named sequence sitting in a scratch/archive bin
        elsewhere. Explicitly excludes anything further nested inside an
        "Archive" bin within FINAL — those are old/superseded versions the
        editor deliberately kept, not live duplicates to warn about or
        (worse) delete on overwrite. Reflects actual project state, not
        just this session's self._pp_created_seqs. Used both by Skip
        Nest's rescan and by the pre-nest overwrite-collision check, so an
        episode nested in a prior session (never touched this session) is
        still caught instead of silently overwritten. Returns (name, seq)
        pairs — seq is the real Sequence object, usable with
        deleteSequence().

        The name+treePath filter runs inside a single ExtendScript call
        instead of once per sequence in the whole project — each property
        access (.name, .projectItem.treePath) is its own round trip through
        the ExtendScript bridge, and a season's worth of raw STRINGOUT
        reels/VFX exports/work-in-progress cuts sitting elsewhere in the
        project can easily push total sequence count into the hundreds.
        Only the (usually much smaller) set of actual matches costs a
        Python-side round trip after that, to fetch a live Sequence object
        usable with deleteSequence()."""
        import pymiere
        from pymiere.core import eval_script as _es
        try:
            raw = _es(
                "var __out=[]; var __s=app.project.sequences;"
                "for(var i=0;i<__s.numSequences;i++){"
                "var __nm=String(__s[i].name||'');"
                "if(__nm.toUpperCase().indexOf('FINAL_EP')===-1)continue;"
                "var __tp='';"
                "try{__tp=String(__s[i].projectItem.treePath).toUpperCase();}catch(e){continue;}"
                "if(__tp.indexOf('DELIVERY')!==-1&&__tp.indexOf('FINAL')!==-1&&__tp.indexOf('ARCHIVE')===-1)"
                "{__out.push(i);}}"
                "__out.join(',');",
                decode_json=False)
            if not raw.strip():
                return []
            idxs = [int(x) for x in raw.split(",")]
            seqs = pymiere.objects.app.project.sequences
            return [(str(seqs[i].name), seqs[i]) for i in idxs]
        except Exception:
            return []

    def _pp_scan_project_trailer_seq(self):
        """First sequence in the current Premiere project with both FINAL
        and TRAILER in its name — the pre-existing, already-nested trailer
        timeline Skip Nest picks up for the export queue. Returns (name,
        seq) or None."""
        import pymiere
        try:
            seqs = pymiere.objects.app.project.sequences
            n = len(seqs)
            for i in range(n):
                s = seqs[i]
                nm = str(s.name) if s.name is not None else ""
                upper = nm.upper()
                if "FINAL" in upper and "TRAILER" in upper:
                    return (nm, s)
            return None
        except Exception:
            return None

    def _pp_find_bin_by_name(self, parent_item, name):
        """Searches parent_item's direct children for a bin whose name
        contains `name` (case-insensitive substring, not exact match —
        real project bins are often named things like "FINAL EPISODES"
        rather than just "FINAL"). Returns the first matching ProjectItem,
        or None."""
        try:
            children = parent_item.children
            for i in range(children.numItems):
                child = children[i]
                if name.strip().lower() in str(child.name or "").strip().lower():
                    return child
        except Exception:
            pass
        return None

    def _pp_find_delivery_final_bin(self, project_root):
        """Finds the existing DELIVERY > FINAL bin in the project panel —
        nested episodes get moved there instead of the project root when
        it's found. Doesn't create it if missing; the caller falls back to
        the root bin and logs a warning instead."""
        delivery = self._pp_find_bin_by_name(project_root, "DELIVERY")
        if delivery is None:
            return None
        return self._pp_find_bin_by_name(delivery, "FINAL")

    def _pp_overwrite_dialog(self, ep_nums):
        """Yes/No modal warning that the episodes about to be nested would
        collide with ones already sitting in the project's DELIVERY > FINAL
        bin (from an earlier nest run, e.g. renested after QC notes) — not
        the AME export queue, despite the similar-sounding name; see
        _pp_scan_project_final_ep_seqs.

        "Apply to all reels" (Nest All only — a single-reel run only ever
        has one collision-check anyway) is a checkbox, not a separate
        pair of buttons: check it before clicking Yes or No and that
        answer is remembered and silently reused for every later
        collision, but ONLY for the rest of THIS nesting task — reset
        back to "ask again" (and the checkbox back to unchecked) at the
        start of every genuinely fresh (non-resume, non-auto_chained)
        Nest Episodes click, see _pp_run_autonest. Left unchecked (the
        default), Yes/No only answer for this one collision, same as
        always. This replaced an earlier "Yes to All"/"No to All"
        button-pair design — same remember-for-this-task mechanism
        underneath, just fewer buttons competing for room in the row.

        Cancel is a third, distinct choice from No — that just skips the
        colliding episode(s) and keeps going with the rest of the nest;
        Cancel abandons the nest run entirely, same as hitting STOP right
        here would. It just sets self._pp_stop_nest (the same flag STOP
        itself sets) before closing — this method's caller,
        _pp_precheck_and_start_nest's _finish(), already checks that flag
        first thing and routes to _pp_abort_precheck_stop() in that case,
        so no separate cancel-handling path is needed here. Closing the
        dialog via its window-close button behaves the same way, not as
        an implicit "No" — silently skipping episodes on a stray click
        felt riskier than just stopping and letting the editor decide
        again.

        Returns True (overwrite) or False (skip the collisions) — the
        return value is meaningless when Cancel was chosen, since the
        caller bails out on self._pp_stop_nest before ever reading it."""
        ep_list = ", ".join(f"EP{n:02d}" for n in sorted(ep_nums))
        result = {"value": False}
        dlg = tk.Toplevel(self)
        dlg.withdraw()  # see _pp_alert_dialog's withdraw for why
        dlg.title("Overwrite?")
        dlg.configure(bg=BG_DARK)
        dlg.resizable(False, False)
        dlg.transient(self)

        body = tk.Frame(dlg, bg=BG_DARK, padx=24, pady=20)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="Overwrite?", font=("SF Pro Display", 16, "bold"),
                 bg=BG_DARK, fg=ACCENT, anchor="w").pack(anchor="w", pady=(0, 8))
        tk.Frame(body, bg=ACCENT, height=1).pack(fill="x", pady=(0, 8))
        tk.Label(body, text="Nested episode sequences with these numbers already exist in "
                             "the project's DELIVERY > FINAL bin:",
                 font=FONT_LABEL, bg=BG_DARK, fg=TEXT_MUTED,
                 wraplength=380, justify="left", anchor="w").pack(anchor="w", pady=(0, 8))
        tk.Label(body, text=ep_list, font=("SF Pro Display", 12, "bold"),
                 bg=BG_DARK, fg=self.EXP_LIVE_TXT,
                 wraplength=380, justify="left", anchor="w").pack(anchor="w")
        # Two blank lines of breathing room before the question — real
        # empty Labels at the body font's own size, not a fixed-pixel
        # margin, so the gap scales the same way actual text would.
        tk.Label(body, text="", font=FONT_LABEL, bg=BG_DARK).pack(anchor="w")
        tk.Label(body, text="", font=FONT_LABEL, bg=BG_DARK).pack(anchor="w")
        tk.Label(body, text="Would you like to overwrite them?",
                 font=FONT_LABEL, bg=BG_DARK, fg=TEXT_MUTED,
                 wraplength=380, justify="left", anchor="w").pack(anchor="w", pady=(0, 16))

        btn_row = tk.Frame(body, bg=BG_DARK)
        btn_row.pack(anchor="e", fill="x")

        # Always exists (even for a single-reel run, where the checkbox
        # itself is never shown/packed below) so Yes/No's lambdas can
        # read it unconditionally — it just always reads False in that
        # case, which is exactly the old plain-Yes/No behavior.
        apply_all_var = tk.BooleanVar(value=False)

        def _answer(val, remember=False):
            result["value"] = val
            if remember:
                self._pp_hide_overwrite_prompt = True
                self._pp_overwrite_remembered_answer = val
            dlg.destroy()

        def _cancel():
            self._pp_stop_nest = True
            dlg.destroy()

        # Sits apart from the Yes/No/checkbox group, at the opposite
        # edge — a separate, higher-stakes choice (abandon the whole
        # nest run), not another answer to "would you like to overwrite
        # them?".
        cancel_btn = self._rounded_btn(btn_row, "Cancel", _cancel)
        cancel_btn.pack(side="left")

        # The checkbox only makes sense when there's more than one
        # reel's worth of collisions actually coming — a single-reel run
        # only ever shows this dialog once regardless.
        show_apply_all = self._pp_nest_all_var.get()

        # side="right" stacks right-to-left — the FIRST widget packed
        # ends up at the absolute right edge, each next one lands to its
        # left. Packed in this order (Yes, No, checkbox) to get the
        # visual result reading left to right: [Apply to all reels] No
        # Yes — the checkbox sits just left of No, modifying whichever
        # of the two you go on to click.
        yes_btn = self._rounded_btn(btn_row, "Yes", lambda: _answer(True, remember=apply_all_var.get()), accent=True)
        yes_btn.pack(side="right")
        no_btn = self._rounded_btn(btn_row, "No", lambda: _answer(False, remember=apply_all_var.get()))
        no_btn.pack(side="right", padx=(0, 8))
        if show_apply_all:
            apply_all_check = self._canvas_checkbox(btn_row, apply_all_var, "Apply to all reels")
            apply_all_check.pack(side="right", padx=(0, 12))

        dlg.protocol("WM_DELETE_WINDOW", _cancel)
        dlg.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_reqheight()) // 2
        dlg.geometry(f"+{x}+{y}")
        dlg.deiconify()
        dlg.grab_set()
        dlg.wait_window()
        return result["value"]

    def _pp_run_autonest(self, resume=False, auto_chained=False):
        """Validate and start the auto-nest background task. resume=True skips
        the "Before You Start" reminder and reuses the Mute Master Clips
        checkbox's value from when the reel started, picking up where the current reel's
        resume_idx left off instead of starting over. auto_chained=True marks
        a Nest-All-triggered continuation onto the next reel — same as a fresh
        start, but without re-showing the "Before You Start" reminder."""
        if self._pp_nest_all_var.get() and not resume:
            # Nest All always starts from the first not-yet-nested reel,
            # regardless of whatever the (locked) dropdown currently shows.
            if len(self._pp_reels) < len(self._pp_stringout_map):
                self._pp_nest_status.config(
                    text="Still scanning timelines — try again in a moment.", fg=TEXT_WARN)
                return
            next_idx = self._pp_find_next_unfinished_reel(0)
            if next_idx is None:
                self._pp_nest_status.config(text="✗ All reels are already nested.", fg=TEXT_ERROR)
                return
            if next_idx != self._pp_current_reel:
                self._pp_arm_existing_reel(next_idx)

        try:
            start_ep = int(self.pp_start_ep.get().strip())
        except ValueError:
            self._pp_nest_status.config(text="✗ Starting Episode # must be a number.", fg=TEXT_ERROR)
            return

        if self._pp_current_reel is None:
            self._pp_nest_status.config(text="✗ No timeline selected.", fg=TEXT_ERROR)
            return

        reel = self._pp_reels[self._pp_current_reel]

        if not resume:
            if not auto_chained:
                # "Yes to All"/"No to All" (see _pp_overwrite_dialog) only
                # apply for the duration of a single nesting task — reset
                # here, at the same "genuinely fresh start" boundary the
                # "Before You Start" reminder already uses (not on every
                # reel within a Nest All chain, just the first one), so a
                # LATER, unrelated Nest Episodes click always asks again
                # instead of silently inheriting a choice from before.
                self._pp_hide_overwrite_prompt = False
                self._pp_overwrite_remembered_answer = True
            if not auto_chained:
                proceed = self._pp_alert_dialog(
                    "Before You Start",
                    "Make sure all your tracks — including captions — are selected "
                    "(highlighted blue) in Premiere.",
                    show_illustration=True,
                    tip="Tracks left deselected won't be included in the nested "
                        "episodes — leave out anything you don't need.")
                if not proceed:
                    return

            # Disable immediately — closes the gap where this stayed
            # clickable while the FINAL-bin collision scan below (a
            # background thread) was still running; previously it only got
            # disabled once _pp_begin_nest_thread ran, after that scan
            # finished. Also covers Nest All's reel-to-reel auto-chain
            # (_advance, inside _pp_autonest_task's _restore_after_nest):
            # _pp_arm_existing_reel re-enables this button for the newly-
            # armed reel right before that calls back in here, so this
            # keeps it disabled the whole way through Nest All instead of
            # it going clickable again between reels.
            self._set_btn_state(self.btn_pp_run, False)

            self._pp_mute_ref = self._pp_mute_var.get()
            reel["resume_idx"] = 0
            reel["done"] = [False] * reel["total"]
            # Field may have been hand-edited after the chips were pre-labeled
            # with the auto-filled default — keep the chips in sync.
            if start_ep != reel["start_ep"]:
                reel["start_ep"] = start_ep
                for i, c in enumerate(reel["chips"]):
                    c.itemconfig("txt", text=f"EP{start_ep + i:02d}")

            # The FINAL-bin collision scan below touches Premiere and can
            # be slow on a project with a lot of existing sequences —
            # runs in the background instead of blocking the UI thread
            # right after the "Before You Start" dialog closes (that used
            # to leave its OK button looking stuck-pressed for however
            # long the scan took, with zero visible feedback — the dialog
            # can't actually finish closing/repainting, and the thinking
            # dots hadn't even started yet since that call came later).
            self._start_thinking(manage_reset_btn=False)
            # RESET ALL becomes STOP right here, not just once
            # _pp_begin_nest_thread eventually runs — otherwise this whole
            # precheck phase (thinking-dots already animating) had RESET
            # ALL still showing as a plain reset instead of a graceful stop.
            self._pp_swap_reset_to_nest_stop()
            self._pp_nest_status.config(text="Checking for existing episodes...", fg=TEXT_MUTED)
            import threading
            threading.Thread(target=self._pp_precheck_and_start_nest,
                              args=(reel, start_ep), daemon=True).start()
            return

        sc = self.pp_show_code.get().strip() or "SHOWCODE"
        ac = self.pp_acronym.get().strip() or "ACRONYM"
        dt = self.pp_date.get().strip() or datetime.now().strftime("%y%m%d")
        self._pp_begin_nest_thread(reel, sc, ac, dt)

    def _pp_abort_precheck_stop(self):
        """STOP clicked during the FINAL-bin collision precheck — before
        nesting itself ever started, so there's nothing to interrupt
        mid-run, just the commitment to start at all. Undoes exactly what
        _pp_run_autonest's fresh-click path had already done: thinking
        dots, RESET ALL -> STOP, and disabling Nest Episodes."""
        self._stop_thinking()
        self._restore_reset_btn()
        self._pp_nest_status.config(text="Stopped before nesting started.", fg=TEXT_WARN)
        self._pp_refresh_nest_button_enabled()

    def _pp_precheck_and_start_nest(self, reel, start_ep):
        """Background: the actual FINAL-bin collision scan (see
        _pp_run_autonest for why this needs to not block the UI thread),
        followed by the overwrite-confirmation dialog if needed — a real
        Tk window, so that part is marshaled back to the main thread —
        then starts the real nest run. Runs once per fresh (non-resume)
        Nest Episodes click, right after the "Before You Start" dialog
        closes.

        Warn if any episode number this run is about to create already
        exists in the DELIVERY > FINAL bin (e.g. renested on a different
        day after QC notes) — matched by episode NUMBER, not exact name,
        since the date in the name would otherwise hide the overlap.
        Checked directly against the project (not self._pp_created_seqs)
        — anything actually in the FINAL bin would be found here
        regardless of whether this session's memory happens to know
        about it, so a separate in-memory check would only ever be
        redundant with this one. Now that RESET ALL becomes a real STOP
        the moment this precheck starts (see _pp_swap_reset_to_nest_stop),
        it's also reachable/clickable during this window — honored below
        rather than silently proceeding to nest anyway."""
        if self._pp_stop_nest:
            self.after(0, self._pp_abort_precheck_stop)
            return
        target_ep_nums = set(range(start_ep, start_ep + reel["total"]))
        existing_ep_nums = {self._pp_extract_ep_num(nm)
                             for nm, _ in self._pp_scan_project_final_ep_seqs()} - {None}
        colliding = target_ep_nums & existing_ep_nums

        def _finish(overwrite_queue):
            if self._pp_stop_nest:
                self._pp_abort_precheck_stop()
                return
            reel["overwrite_queue"] = overwrite_queue
            sc = self.pp_show_code.get().strip() or "SHOWCODE"
            ac = self.pp_acronym.get().strip() or "ACRONYM"
            dt = self.pp_date.get().strip() or datetime.now().strftime("%y%m%d")
            self._pp_begin_nest_thread(reel, sc, ac, dt)

        if colliding and not self._pp_hide_overwrite_prompt:
            def _ask():
                overwrite = self._pp_overwrite_dialog(colliding)
                _finish(overwrite)
            self.after(0, _ask)
        elif colliding:
            # "Don't show this again" was already checked on an earlier
            # reel — reuse that same Yes/No answer instead of re-asking.
            self.after(0, lambda: _finish(self._pp_overwrite_remembered_answer))
        else:
            self.after(0, lambda: _finish(True))

    def _pp_swap_reset_to_nest_stop(self):
        """RESET ALL becomes a graceful STOP for the nest run (exactly like
        VFX tab) — factored out so it can run as soon as a fresh Nest
        Episodes click is committed to (see _pp_run_autonest), not just
        once _pp_begin_nest_thread actually starts nesting. Without this,
        the async FINAL-bin collision precheck in between ("Checking for
        existing episodes...") was a window where thinking-dots were
        already animating but RESET ALL hadn't swapped to STOP yet —
        clicking it there would've done a full reset instead of a graceful
        stop. Safe to call twice in a row (idempotent re-draw/re-bind);
        _pp_begin_nest_thread still calls this itself too, since the
        Continue Nesting/resume path never goes through the early call."""
        self._disable_reset_btn()
        self.btn_reset._text = "STOP"
        self.btn_reset._draw("#6e1a1a")
        self.btn_reset.unbind("<Enter>")
        self.btn_reset.unbind("<Leave>")
        self.btn_reset.unbind("<ButtonPress-1>")
        self.btn_reset.unbind("<ButtonRelease-1>")

        def _enable_pp_stop():
            self.btn_reset._draw("#8e2a2a")
            self.btn_reset.unbind("<Enter>")
            self.btn_reset.unbind("<Leave>")
            self.btn_reset.unbind("<ButtonPress-1>")
            self.btn_reset.unbind("<ButtonRelease-1>")
            self.btn_reset.bind("<Enter>", lambda e: self.btn_reset._draw("#ae3a3a"))
            self.btn_reset.bind("<Leave>", lambda e: self.btn_reset._draw("#8e2a2a"))
            self.btn_reset.bind("<ButtonPress-1>", lambda e: self.btn_reset._draw("#6e1a1a"))
            def _on_pp_stop(e):
                self.btn_reset.unbind("<Enter>")
                self.btn_reset.unbind("<Leave>")
                self.btn_reset.unbind("<ButtonPress-1>")
                self.btn_reset.unbind("<ButtonRelease-1>")
                self.btn_reset._draw("#6e1a1a")
                self._pp_nest_status.config(text="Stopping...", fg=TEXT_MUTED)
                self._pp_stop_nest = True
            self.btn_reset.bind("<ButtonRelease-1>", _on_pp_stop)
        self.after(0, _enable_pp_stop)

    def _pp_begin_nest_thread(self, reel, sc, ac, dt):
        """Shared tail that actually kicks off the real nest background
        task — called either right after resume=True skips the pre-check
        above, or from _pp_precheck_and_start_nest once that's done."""
        self._pp_stop_nest = False
        self._pp_nesting_active = True
        self._start_thinking(manage_reset_btn=False)
        self._pp_set_timeline_dropdown_enabled(False)
        # Locked for the whole run, including while paused — only
        # re-enabled once nesting is genuinely, fully done (see
        # _restore_after_nest's "reel done, nothing left to chain to"
        # branch), never mid-run or after a stop, so nobody can flip
        # these while episodes are actively being created.
        self._pp_mute_check.set_enabled(False)
        self._pp_nest_all_check.set_enabled(False)
        self._pp_swap_reset_to_nest_stop()
        # Disable Nest Episodes button while running
        self._set_btn_state(self.btn_pp_run, False)
        self._set_btn_state(self.btn_pp_reset_nest, False)
        self._pp_nest_status.config(text="Preparing nest...", fg=TEXT_MUTED)

        import threading
        threading.Thread(target=self._pp_autonest_task,
                         args=(reel, sc, ac, dt, self._pp_mute_ref),
                         daemon=True).start()

    def _pp_continue_nest(self):
        self._pp_run_autonest(resume=True)

    def _pp_show_continue_nest_button(self):
        """Switch the Nest Episodes button into "Continue Nesting" mode after
        a stop — same idea as the VFX tab's Export/Continue button, but done
        by swapping _text/_command rather than manually rebinding every
        hover/press event, since _set_btn_state already redraws from _text
        and rebinds <ButtonRelease-1> to call _command for us."""
        self.btn_pp_run._text = "Continue Nesting"
        self.btn_pp_run._command = self._pp_continue_nest
        self._set_btn_state(self.btn_pp_run, True)

    def _pp_stop_nest_now(self):
        self._pp_stop_nest = True
        self._pp_nest_status.config(text="Stopping...", fg=TEXT_MUTED)

    def _pp_destroy_all_nest_chips(self):
        """Full teardown for RESET ALL — destroys every reel's chips, not
        just the current one. Recreates the permanent zero-size placeholder
        afterward (macOS Tk quirk: a frame's winfo_reqheight() doesn't shrink
        back down after all its children are destroyed unless at least one
        small child always survives)."""
        for w in list(self._pp_nest_chips_frame.winfo_children()):
            w.destroy()
        tk.Frame(self._pp_nest_chips_frame, bg="#252525", width=0, height=0).pack()

    def _pp_destroy_all_exp_chips(self):
        for w in list(self._pp_exp_chips_frame.winfo_children()):
            w.destroy()
        tk.Frame(self._pp_exp_chips_frame, bg="#252525", width=0, height=0).pack()
        self._pp_exp_chip_canvases = []

    def _pp_full_reset(self, keep_show_info=False, keep_output_folder=False,
                        keep_premiere_connection=False):
        """Full reset of Episode Export tab — called by RESET ALL, and (with
        keep_show_info/keep_output_folder=True) by Unskip Nest, which resets
        everything else but leaves those two sections exactly as the user
        already set them. keep_premiere_connection=True (also Unskip Nest)
        additionally leaves self._pp_connected and self._pp_stringout_map
        alone — Phase 1's reel model/chips still get cleared (nothing in
        them survives Skip Nest anyway), but the connection itself
        doesn't need re-establishing; see _pp_swap_dropdown_for_rescan_button
        for how the dropdown becomes a "Rescan Reels" button in that case."""
        if not keep_premiere_connection:
            self._pp_connected = False
            self._pp_stringout_map = {}
        self._pp_seq = None
        self._pp_track_idx = None
        self._pp_tails_tc = None
        self._pp_reels = []
        self._pp_current_reel = None
        if not keep_show_info:
            self._pp_show_info_locked = False
        self._pp_created_seqs = []
        self._pp_trailer_seq_name = None
        self._pp_stop_nest = False
        self._pp_stop_export = False
        self._pp_nest_done = False
        self._pp_nest_resume_idx = 0
        self._pp_mute_ref = False
        self._pp_mute_var.set(False)
        self._pp_mute_check.set_enabled(False)
        self._pp_nesting_active = False
        self._pp_nest_all_var.set(False)
        self._pp_nest_all_check.set_enabled(False)
        self._pp_hide_overwrite_prompt = False
        self._pp_overwrite_remembered_answer = True
        self._pp_style_vars["LIVE"].set(True)
        self._pp_style_vars["MARKETING"].set(False)
        self._pp_style_vars["SOCIAL MEDIA"].set(False)
        for chk in self._pp_style_checks.values():
            chk.set_enabled(False)
        self._pp_srt_var.set(False)
        self._pp_srt_check.set_enabled(False)
        self._pp_update_date_var.set(False)
        self._pp_update_date_check.set_enabled(False)
        self._pp_ame_preset_paths = {}
        self._pp_exp_resume_idx = 0
        self._pp_exp_disabled = set()
        self._pp_exp_all_disabled = False
        self._pp_exp_started = False
        self._pp_exporting_active = False
        self._pp_exp_queued = set()
        self._pp_exp_done_style = {}
        self._pp_ame_connected = False
        if keep_premiere_connection:
            # Connection stays up — self._pp_reels is always empty by this
            # point (nothing in it survives Skip Nest being engaged), so
            # the dropdown has nothing left to show/select from. Swap it
            # for a "Rescan Reels" button in the same slot rather than
            # tearing everything down and forcing a fresh Connect to
            # Premiere click.
            self._pp_swap_dropdown_for_rescan_button()
            self._pp_mute_check.set_enabled(True)
            self._pp_nest_all_check.set_enabled(True)
        else:
            # Reset connect step — tear down whatever's currently in that
            # slot (the timeline dropdown, or a "Rescan Reels" button if
            # Unskip Nest ran but Rescan Reels was never actually clicked)
            # and restore the Connect button.
            if getattr(self, "_pp_rescan_reels_btn", None) is not None:
                self._pp_rescan_reels_btn.destroy()
                self._pp_rescan_reels_btn = None
            if getattr(self, "_pp_timeline_dropdown", None) is not None:
                self._pp_timeline_dropdown.destroy()
                self._pp_timeline_dropdown = None
            if not (getattr(self, "btn_pp_connect", None) and self.btn_pp_connect.winfo_exists()):
                self.btn_pp_connect = self._rounded_btn(self._pp_p1_row, "Connect to Premiere", self._pp_connect,
                                                         min_width=170)
                self.btn_pp_connect.pack(side="left", padx=(0, 14), before=self._pp_circ_p1_2)
            # No "Not connected." status text — that's the obvious default
            # state, redundant to spell out (_pp_nest_status gets cleared
            # to "" a few lines below regardless).
            self._set_btn_state(self.btn_pp_connect, True)
        self._pp_set_circle_active(self._pp_circ_p1_1, "1")
        # Reset title card track (always-enabled section, independent of
        # connect state — unless Skip Nest locked it, which this undoes)
        self._pp_track_mode.set("auto")
        # Rebuilds from self._pp_stringout_map — a single default "REEL 1"
        # placeholder box in each mode unless keep_premiere_connection
        # left it populated, in which case these show fresh, blank boxes
        # for the still-known reels (their individual track picks were
        # just wiped above along with self._pp_reels, same as everything
        # else this reset clears).
        self._pp_build_auto_track_boxes()
        self._pp_build_manual_track_boxes()
        if self._pp_track_radios:
            self._pp_track_radios[0].set_locked(False)
        for box in self._pp_track_auto_boxes.values():
            box["dropdown"].set_locked(False)
        self._pp_on_track_mode_change()
        # Reset nest step
        self._pp_set_circle_active(self._pp_circ_p1_2, "2")
        self._pp_sync_ep_entry_enabled()
        self.pp_start_ep.set("1")
        self.btn_pp_run._text = "Nest Episodes"
        self.btn_pp_run._command = self._pp_run_autonest
        self._set_btn_state(self.btn_pp_run, False)
        self._set_btn_state(self.btn_pp_stop_nest, False)
        self._set_btn_state(self.btn_pp_reset_nest, False)
        self._pp_skip_nest_mode = False
        self.btn_pp_skip_nest._text = "Skip Nest"
        self.btn_pp_skip_nest._command = self._pp_skip_nest_click
        self._set_btn_state(self.btn_pp_skip_nest, keep_premiere_connection)
        self._pp_nest_progress_var.set(0)
        self._pp_update_nest_bar()
        self._pp_nest_status.config(text="", fg=TEXT_MUTED)
        self._pp_destroy_all_nest_chips()
        if not keep_output_folder:
            # Reset output folder (always-enabled section, independent of nest state)
            self._pp_out_mode.set("auto")
            self._pp_out_dir.set("")
            self._pp_marketing_out_dir.set("")
            self._pp_trailer_out_dir.set("")
            self._pp_srt_out_dir.set("")
            self._pp_out_hint.config(text="Auto-detected after nesting", fg=TEXT_MUTED)
            self._set_widgets_enabled([self._pp_out_entry, self._pp_out_browse_btn], False)
            self._set_widgets_enabled([self._pp_marketing_out_entry, self._pp_marketing_out_browse_btn], False)
            self._set_widgets_enabled([self._pp_trailer_out_entry, self._pp_trailer_out_browse_btn], False)
            self._set_widgets_enabled([self._pp_srt_out_entry, self._pp_srt_out_browse_btn], False)
        self._pp_refresh_manual_folder_rows()
        # Reset phase 2 — Connect to AME goes back to disabled/original label;
        # it's only unlocked again by a nest completing or Skip Nest.
        self._pp_set_circle_active(self._pp_circ_p2_1, "1")
        self._pp_set_circle_active(self._pp_circ_p2_2, "2")
        self.btn_pp_connect_ame._text = "Connect to AME"
        self.btn_pp_connect_ame._command = self._pp_connect_ame
        self._set_btn_state(self.btn_pp_connect_ame, False)
        self.btn_pp_export._text = "Queue Episodes"
        self.btn_pp_export._command = self._pp_run_export
        self._set_btn_state(self.btn_pp_export, False)
        self._set_btn_state(self.btn_pp_stop_exp, False)
        self._set_btn_state(self.btn_pp_reset_exp, False)
        self._pp_exp_progress_var.set(0)
        self._pp_reset_exp_progress_color()
        self._pp_exp_status.config(text="", fg=TEXT_MUTED)
        self._pp_destroy_all_exp_chips()
        self.btn_toggle_exp_all.config(text="DISABLE ALL")
        self._pp_exp_run_complete = False
        self._pp_refresh_exp_util_buttons()
        self.btn_clear_all_exp.pack_forget()
        if not keep_show_info:
            # Reset show info
            self._pp_show_mode_var.set("auto")
            self._pp_on_show_mode_change()
            self._pp_preview_label.config(text="Connect to Premiere to auto-fill...")
            self.pp_show_code.set("")
            self.pp_acronym.set("")
            self.pp_date.set(datetime.now().strftime("%y%m%d"))
            if hasattr(self, '_show_pill'):
                self._update_show_pill("")
        # Chip boxes just got emptied above — shrink the window back down to fit.
        self._pp_resize_window()

    def _pp_reset_nest(self, force_all=False):
        """Resets nest progress for the currently-selected reel — or, when
        Nest All is checked (or force_all, used by Skip Nest to abandon
        every reel at once), every reel. Already-created Premiere
        subsequences are left alone (just untracked/unqueued). Also purges
        the reset reel(s)' episode names from the export queue's permanent
        queued/disabled tracking, so a reset-and-renest doesn't wrongly
        inherit a stale "already queued" state from before the reset.

        Also a safety valve for _thinking_depth (see _start_thinking/
        _stop_thinking) — Reset Nest is the way to bail out of a reel
        prescan that looks stuck, so it should always be able to clear
        the shared spinner/STOP state outright. And, critically,
        _pp_prescan_abort=True actually interrupts a still-running
        background prescan thread itself — without this, that thread
        just kept running to completion in the background regardless of
        anything Reset Nest did visually, appending reels to
        self._pp_reels at unpredictable times and racing against whatever
        the editor did next (seen live as reels ending up out of order
        after Nest All)."""
        self._thinking_depth = 0
        self._thinking_active = False
        self._pp_prescan_abort = True
        if self._pp_current_reel is None:
            return
        target_idxs = set(range(len(self._pp_reels))) if (force_all or self._pp_nest_all_var.get()) \
            else {self._pp_current_reel}

        reset_names = {name for name, sub, reel_idx in self._pp_created_seqs
                       if reel_idx in target_idxs}
        self._pp_created_seqs = [e for e in self._pp_created_seqs if e[2] not in target_idxs]
        self._pp_exp_queued -= reset_names
        self._pp_exp_disabled -= reset_names
        for n in reset_names:
            self._pp_exp_done_style.pop(n, None)

        for idx in target_idxs:
            reel = self._pp_reels[idx]
            reel["done"] = [False] * reel["total"]
            reel["resume_idx"] = 0
            reel["nest_done"] = False
            # Redo should re-evaluate the Mute Master Clips checkbox's
            # current value (it may have changed since the first attempt),
            # not skip past it.
            reel["setup_done"] = False
            # Revert to whatever Starting Episode # this reel was first
            # scanned with — undoes any hand-edit made before the reset.
            reel["start_ep"] = reel.get("original_start_ep", reel["start_ep"])
            for i in range(reel["total"]):
                self._pp_set_reel_chip_pending(reel, i)
                if i < len(reel["chips"]):
                    reel["chips"][i].itemconfig("txt", text=f"EP{reel['start_ep'] + i:02d}")

        self._pp_nest_done = False
        self._pp_stop_nest = False
        self._pp_nest_resume_idx = 0
        self._pp_nest_progress_var.set(0)
        self._pp_update_nest_bar()
        self._pp_nest_status.config(text="", fg=TEXT_MUTED)
        self.pp_start_ep.set(str(self._pp_reels[self._pp_current_reel]["start_ep"]))
        self.btn_pp_run._text = "Nest Episodes"
        self.btn_pp_run._command = self._pp_run_autonest
        self._set_btn_state(self.btn_pp_run, True)
        self._set_btn_state(self.btn_pp_stop_nest, False)
        self._set_btn_state(self.btn_pp_reset_nest, True)
        self._pp_set_timeline_dropdown_enabled(not self._pp_nest_all_var.get())
        self._pp_build_exp_chips()
        self._pp_refresh_export_button()

    def _pp_reset_nest_click(self):
        """The Reset Nest button itself — goes further than the plain
        progress reset _pp_reset_nest does for Skip Nest's
        internal use (recolor chips back to pending, keep the reel
        armed). Runs _pp_reset_nest(force_all=True) first for its
        export-queue purge (untracks every reel's episodes, not just the
        current one), then destroys the EPISODES NESTED chip box entirely
        and swaps the timeline dropdown for a "Rescan Reels" button (see
        _pp_swap_dropdown_for_rescan_button) — so nesting can pick back up
        from a fresh scan instead of reusing whatever track/tails data
        was already scanned, without needing to reconnect to Premiere.

        Also resets the step circles and the Title Card Track section
        (both modes) back to their default look — previously left
        showing whatever they last displayed even though self._pp_reels
        (every reel's scanned track/tails data) was just wiped, which
        was stale/misleading until the next Rescan Reels landed."""
        if not self._pp_reels:
            return
        self._pp_reset_nest(force_all=True)
        self._pp_destroy_all_nest_chips()
        self._pp_reels = []
        self._pp_current_reel = None
        self._set_btn_state(self.btn_pp_run, False)
        self._set_btn_state(self.btn_pp_reset_nest, False)
        self._pp_sync_ep_entry_enabled()
        self._pp_swap_dropdown_for_rescan_button()
        # Step circles: circle 1's slot needs a new action (Rescan Reels)
        # before circle 2 (Nest Episodes) is reachable again, same as a
        # fresh Connect to Premiere would.
        self._pp_set_circle_active(self._pp_circ_p1_1, "1")
        self._pp_set_circle_disabled(self._pp_circ_p1_2, "2")
        self._pp_track_mode.set("auto")
        self._pp_build_auto_track_boxes()
        self._pp_build_manual_track_boxes()
        self._pp_on_track_mode_change()
        # Nest All's "the full plan across every reel is known" premise
        # no longer holds once self._pp_reels has just been wiped —
        # back to unchecked, matching a fresh connect's default.
        if self._pp_nest_all_var.get():
            self._pp_nest_all_var.set(False)
            self._pp_nest_all_check.refresh()
        self._pp_resize_window()

    def _pp_autonest_task(self, reel, sc, ac, dt, mute_ref=True):
        """Background thread: create subsequences for each episode in the
        given reel. Title cards were already found during the prescan step
        (_pp_collect_title_clips, cached on reel["title_clips"]) — nesting
        never re-scans, so it can't disagree with what the chips already show."""
        def _status(msg, color=TEXT_MUTED):
            self.after(0, lambda m=msg, c=color: self._pp_nest_status.config(text=m, fg=c))
            tag = "success" if color == "#50e050" else ("error" if color == TEXT_ERROR else ("warn" if color == TEXT_WARN else "muted"))
            self._pp_log(msg, tag)

        try:
            import pymiere
            seq = reel["seq"]
            track_idx = reel["track_idx"]
            start_ep = reel["start_ep"]
            title_clips = reel["title_clips"]
            total = reel["total"]
            resume_from = reel["resume_idx"]

            # One scan of the FINAL bin for the whole reel (not per-episode)
            # — the real, authoritative source for collisions, catching an
            # episode nested in a prior session just as well as one from
            # this one. Keyed by episode number so a renested episode (same
            # number, different date in the name) is still found as "the
            # same" episode. Computed up front (not just before the nest
            # loop below) so all_declined can check it before track
            # targeting/muting even runs.
            #
            # Each key maps to a LIST, not a single (name, seq) pair — if
            # the project already has more than one sequence sharing the
            # same episode number (e.g. a prior double-nest, or one
            # manually duplicated), a single-value dict would silently
            # keep only the last one scanned and forget the rest existed
            # at all. That meant overwriting could only ever delete ONE
            # of the duplicates, leaving the other permanently invisible
            # to every future collision check — seen live as a demo where
            # every other episode replaced cleanly but one stayed
            # doubled no matter how many times it was renested. Every
            # entry in the list gets deleted below when overwriting.
            existing_final_seqs = {}
            for nm, s in self._pp_scan_project_final_ep_seqs():
                n = self._pp_extract_ep_num(nm)
                if n is not None:
                    existing_final_seqs.setdefault(n, []).append((nm, s))

            # If every remaining episode on this reel already exists in
            # FINAL and the overwrite prompt was declined, nothing here is
            # actually going to get created — skip the reel outright
            # (including track targeting/muting, which would otherwise
            # run for a reel about to produce zero new sequences) instead
            # of running full setup and then a wall of individual
            # "Skipping EP##" messages.
            all_declined = (not reel.get("overwrite_queue", True)) and all(
                (start_ep + idx) in existing_final_seqs for idx in range(resume_from, total))

            # Track targeting + muting only need to happen once per reel — on
            # a resume after a stop, this used to re-run unconditionally,
            # which for a large project meant waiting through the whole
            # (uninterruptible, single ExtendScript call) mute step a second
            # time for no reason. Gate it on the reel itself.
            if all_declined:
                _status(f"⏭ Skipping reel — every remaining episode already nested in "
                        f"DELIVERY > FINAL, not overwriting.", TEXT_WARN)
            elif not reel.get("setup_done"):
                # createSubsequence(False) below determines which tracks' clips get
                # included by TRACK TARGETING (the blue-highlighted track-header
                # state, Track.setTargeted) whenever there's no active clip
                # selection — confirmed via Adobe's own scripting docs. This is
                # separate from a track's mute/eye state, which only affects
                # output visibility, not subsequence inclusion. So: target every
                # track (video AND audio) here, in addition to un-muting them, to
                # guarantee nothing gets left out of the nest. Target this exact
                # sequence (not app.project.activeSequence) — if Premiere's
                # active-sequence focus has shifted (e.g. to a subsequence created
                # by an earlier reel), that would silently touch the wrong timeline.
                from pymiere.core import eval_script as _es
                seq_ref = "$._pymiere['{}']".format(seq._pymiere_id)
                enabled_counts = _es(
                    f"var __s={seq_ref}; var __v=0; var __a=0;"
                    "for(var t=0;t<__s.videoTracks.numTracks;t++){"
                    "__s.videoTracks[t].setMute(0);__s.videoTracks[t].setTargeted(true,false);__v++;}"
                    "for(var t=0;t<__s.audioTracks.numTracks;t++){"
                    "__s.audioTracks[t].setMute(0);__s.audioTracks[t].setTargeted(true,false);__a++;}"
                    "[__v,__a].join(',');",
                    decode_json=False)
                try:
                    v_count, a_count = enabled_counts.split(",")
                    _status(f"Enabled + targeted {v_count} video + {a_count} audio track(s).", TEXT_MUTED)
                except Exception:
                    _status(f"⚠ Track-enable step returned unexpected result: {enabled_counts!r}", TEXT_WARN)

                if not mute_ref:
                    _status("Skipped muting master clips (per your choice) — all tracks left as-is.", TEXT_MUTED)
                    reel["setup_done"] = True
                else:
                    # Find the Video Reference track: scan tracks below the title card
                    # track (never above it) for a clip whose name contains both the show
                    # code and either a "REF" token or a "PIC LOCK" variant. REF sits right
                    # after an underscore but isn't always followed by one (e.g. "..._REF.mov"
                    # or "..._REF2"), so match on underscore-delimited segments that start
                    # with "ref" rather than a raw substring, which was matching unrelated
                    # tracks. "PIC LOCK"/"PIC LOC"/"PICLOCK"/"PICLOC"/"PIC LOCKED"/"PICLOCKED"
                    # all reduce to the same "picloc" substring once spaces are stripped, so
                    # editors who skip the REF suffix and use a pic-lock naming convention
                    # instead are still detected.
                    ref_track_idx = None
                    ref_clip_name = ""
                    for t2 in range(track_idx):
                        tr2 = seq.videoTracks[t2]
                        n2 = tr2.clips.numItems
                        sample = min(3, n2)
                        for ci in range(sample):
                            clip = tr2.clips[ci]
                            cname = str(clip.name) if clip.name is not None else ""
                            name_parts = cname.lower().split("_")
                            has_ref = any(p.startswith("ref") for p in name_parts)
                            has_piclock = "picloc" in cname.lower().replace(" ", "")
                            if sc.lower() in cname.lower() and (has_ref or has_piclock):
                                ref_track_idx = t2
                                ref_clip_name = cname
                                break

                    # Mute that track and every track below it (Track.setMute — the
                    # whole-track toggle, not a per-clip one); leave everything above
                    # it, up to but not including the title card track, untouched. A
                    # single ExtendScript call across the whole range is fine here
                    # (unlike the old per-clip "disabled" approach this replaced)
                    # because muting a track is O(1) regardless of how many clips it
                    # holds — no per-track chunking or STOP-mid-loop handling needed.
                    # Because each episode is created as a subsequence of this
                    # timeline, the muted state carries into every nested episode
                    # automatically.
                    if ref_track_idx is not None:
                        _status(f"Reference track detected: V{ref_track_idx + 1} ('{ref_clip_name}')", TEXT_MUTED)
                        _es(f"var __s={seq_ref};"
                            f"for(var t=0;t<={ref_track_idx};t++){{__s.videoTracks[t].setMute(1);}}")
                        _status(f"Muted V1–V{ref_track_idx + 1}.", TEXT_MUTED)
                        reel["setup_done"] = True
                    else:
                        _status("⚠ No reference track found below title card track — nothing muted.", TEXT_WARN)
                        reel["setup_done"] = True
            else:
                _status("Tracks already targeted/muted for this reel — skipping.", TEXT_MUTED)

            # Find tails / reel end. Internal math stays in ticks (Premiere's
            # exact native time unit, arbitrary-precision as a Python int) for
            # precise sorting/comparison — but Sequence.setInPoint/setOutPoint
            # specifically want SECONDS (confirmed via Adobe's own scripting
            # docs: "Integer or Time object - a new time in seconds"), unlike
            # some other Premiere time APIs (e.g. setPlayerPosition, which
            # does take ticks). Passing ticks there was the actual cause of
            # episodes being skipped and landing on the wrong in/out points —
            # ticks are ~254 billion per second, so misreading them as seconds
            # would put the point wildly out of range. Ticks are converted to
            # seconds only right at the setInPoint/setOutPoint call sites below.
            TICKS_PER_SECOND = 254016000000.0
            tails_start = reel["tails_tc"]  # already ticks (int), cached from prescan
            reel_end = tails_start if tails_start is not None else int(seq.end)

            # Store original in/out — handle case where none set
            try:
                old_in  = int(seq.getInPointAsTime().ticks)
                old_out = int(seq.getOutPointAsTime().ticks)
            except Exception:
                old_in, old_out = 0, int(seq.end)

            # Precompute every episode's start/end, then format all of them as
            # on-timeline timecodes in a single ExtendScript call up front,
            # rather than one call per episode inside the loop below.
            ep_windows = []
            for idx, tc in enumerate(title_clips):
                ep_start = int(tc.end.ticks)
                ep_end = (int(title_clips[idx + 1].start.ticks)
                          if idx + 1 < len(title_clips) else reel_end)
                ep_windows.append((ep_start, ep_end))
            try:
                zero_ticks = self._pp_get_zero_ticks(seq)
                flat_ticks = [s + zero_ticks for pair in ep_windows for s in pair]
                flat_tcs = self._pp_format_timecodes(seq, flat_ticks)
                tc_pairs = [(flat_tcs[2 * i], flat_tcs[2 * i + 1]) for i in range(len(ep_windows))]
            except Exception as e:
                import traceback
                traceback.print_exc()
                _status(f"⚠ Couldn't format episode timecodes: {e}", TEXT_WARN)
                tc_pairs = [(f"(fmt error: {e})", f"(fmt error: {e})") for _ in ep_windows]

            created = []
            reel_idx = self._pp_current_reel
            project_root = pymiere.objects.app.project.rootItem
            delivery_final_bin = self._pp_find_delivery_final_bin(project_root)
            output_bin = delivery_final_bin if delivery_final_bin is not None else project_root
            if delivery_final_bin is None:
                _status("⚠ Couldn't find a DELIVERY > FINAL bin — nested episodes will "
                        "go to the project root instead.", TEXT_WARN)
            # existing_final_seqs already scanned up front, before the
            # track targeting/muting step above.

            for idx, tc in enumerate(title_clips):
                if self._pp_stop_nest:
                    break

                ep_num = start_ep + idx

                if idx < resume_from:
                    # Already handled in an earlier (stopped) run — chip is
                    # already showing done from that run, nothing to redo.
                    continue

                ep_start, ep_end = ep_windows[idx]

                if ep_end <= ep_start:
                    _status(f"⚠ Skipping EP{ep_num:02d} — end before start.", TEXT_WARN)
                    reel["done"][idx] = True
                    reel["resume_idx"] = idx + 1
                    self._pp_nest_resume_idx = idx + 1
                    self.after(0, lambda r=reel, i=idx: self._pp_set_reel_chip_done(r, i))
                    continue

                # If this episode number already exists in the FINAL bin
                # (renested after QC notes, on a different day — matched by
                # number since the date in the name differs) and the
                # overwrite prompt shown before this run started was
                # declined, skip this episode entirely — no orphaned
                # subsequence gets created for a chip that'll never show it.
                existing_matches = existing_final_seqs.get(ep_num) or []
                if existing_matches and not reel.get("overwrite_queue", True):
                    if not all_declined:
                        # Every-episode-declined already got one reel-level
                        # "Skipping reel..." status above — an individual
                        # message per episode on top of that would just be
                        # noise. Still shown for a PARTIAL decline (some
                        # episodes overwritten/new, only some declined),
                        # where it's the only signal for which ones.
                        _status(f"⏭ Skipping EP{ep_num:02d} — already nested in DELIVERY > FINAL, "
                                f"not overwriting.", TEXT_WARN)
                    reel["done"][idx] = True
                    reel["resume_idx"] = idx + 1
                    self._pp_nest_resume_idx = idx + 1
                    # Track every kept (declined-overwrite) sequence the same
                    # way a freshly-created one is tracked — this reel's
                    # Phase 1 chip just turned green for it (done above), so
                    # Phase 2 should treat it as already-accounted-for too,
                    # instead of Connect to AME's later scan "discovering"
                    # it as an unrelated pre-existing episode and defaulting
                    # it to disabled/grey (see _pp_skip_nest_scan_task). If
                    # more than one sequence already shares this episode
                    # number, every one of them gets tracked here — not just
                    # the first — so Phase 2 knows about all of them instead
                    # of only the one this dict happened to keep.
                    for old_name, old_sub in existing_matches:
                        if not any(nm == old_name for nm, _, _ in self._pp_created_seqs):
                            self._pp_created_seqs.append((old_name, old_sub, reel_idx))
                        self._pp_exp_disabled.discard(old_name)
                    self.after(0, lambda r=reel, i=idx: self._pp_set_reel_chip_done(r, i))
                    continue

                tc_in, tc_out = tc_pairs[idx]
                _status(f"Creating EP{ep_num:02d} ({idx+1}/{total}) — {tc_in} to {tc_out}", TEXT_MUTED)
                self.after(0, lambda r=reel, i=idx: self._pp_set_reel_chip_active(r, i))

                # ignoreTrackTargeting=False: with no active clip selection
                # (true here), this falls back to track targeting regardless —
                # which is exactly what we set up above by targeting every
                # track before this loop started.
                name = f"{sc}_{ac}_FINAL_EP{ep_num:02d}_{dt}"
                try:
                    # One ExtendScript round trip instead of four (setInPoint,
                    # setOutPoint, createSubsequence, setZeroPoint, name=) —
                    # falls back to the original one-call-each approach below
                    # if anything about the batched path goes wrong, since it
                    # reaches one level deeper into pymiere's internals than a
                    # normal call.
                    sub = self._pp_create_subsequence_batched(
                        seq, ep_start / TICKS_PER_SECOND, ep_end / TICKS_PER_SECOND, name)
                except Exception:
                    seq.setInPoint(ep_start / TICKS_PER_SECOND)
                    seq.setOutPoint(ep_end / TICKS_PER_SECOND)
                    sub = seq.createSubsequence(False)
                    sub.setZeroPoint("0")
                    sub.name = name
                try:
                    sub.projectItem.moveBin(output_bin)
                except Exception as e:
                    _status(f"⚠ Couldn't move {name}: {e}", TEXT_WARN)
                created.append(name)
                if existing_matches:
                    # overwrite_queue is True here — the False case already
                    # skipped above via the continue. Actually delete the
                    # old sequence(s) from the project — this used to just
                    # drop one from self._pp_created_seqs, leaving the real
                    # old Premiere sequence behind as an orphaned duplicate
                    # sitting in the FINAL bin, invisible to this app but
                    # very much still there. Every existing match for this
                    # episode number gets deleted here, not just one — if
                    # the project already had more than one sequence
                    # sharing this number (e.g. from an earlier double-nest),
                    # a single-delete would leave the others behind forever,
                    # since nothing else in the project would ever flag them
                    # as duplicates again.
                    for old_name, old_sub in existing_matches:
                        try:
                            ok = pymiere.objects.app.project.deleteSequence(old_sub)
                            if not ok:
                                _status(f"⚠ Premiere reported it couldn't delete the old {old_name}.", TEXT_WARN)
                            else:
                                _status(f"↻ Overwrote EP{ep_num:02d} — old {old_name} deleted, replaced with {name}.", TEXT_MUTED)
                        except Exception as e:
                            _status(f"⚠ Couldn't delete the old {old_name}: {e}", TEXT_WARN)
                        stale = next((e for e in self._pp_created_seqs if e[0] == old_name), None)
                        if stale is not None:
                            self._pp_created_seqs.remove(stale)
                        self._pp_exp_queued.discard(old_name)
                        self._pp_exp_disabled.discard(old_name)
                    if len(existing_matches) > 1:
                        _status(f"⚠ EP{ep_num:02d} had {len(existing_matches)} existing sequences in "
                                f"DELIVERY > FINAL (not just one) — all of them were deleted and "
                                f"replaced with {name}.", TEXT_WARN)
                    self._pp_created_seqs.append((name, sub, reel_idx))
                else:
                    self._pp_created_seqs.append((name, sub, reel_idx))
                reel["done"][idx] = True
                reel["resume_idx"] = idx + 1
                self._pp_nest_resume_idx = idx + 1

                pct = (idx + 1) / total
                self.after(0, lambda p=pct: (self._pp_nest_progress_var.set(p),
                                              self._pp_update_nest_bar()))
                self.after(0, lambda r=reel, i=idx: self._pp_set_reel_chip_done(r, i))

            # Restore in/out
            try:
                seq.setInPoint(old_in / TICKS_PER_SECOND)
                seq.setOutPoint(old_out / TICKS_PER_SECOND)
            except Exception:
                pass

            paused = self._pp_stop_nest and reel["resume_idx"] < total

            if paused:
                reel["nest_done"] = False
                _status(f"⏸ Paused after {reel['resume_idx']}/{total} — "
                        f"{len(created)} sequence(s) created this run.", TEXT_WARN)
            else:
                reel["nest_done"] = True
                self._pp_nest_done = True
                _status(f"✓ {reel['resume_idx']}/{total} episode sequences created.", "#50e050")
                self.after(0, self._pp_on_nest_complete)

            self.after(0, self._stop_thinking)

            def _restore_after_nest():
                self._restore_reset_btn()
                self._pp_nesting_active = False
                if reel["nest_done"]:
                    if self._pp_nest_all_var.get() and not self._pp_stop_nest:
                        next_idx = self._pp_find_next_unfinished_reel(self._pp_current_reel + 1)
                        if next_idx is not None:
                            self._pp_log(f"→ {reel.get('reel_label', 'reel')} done — "
                                         f"continuing to the next reel...", "muted")
                            self._set_btn_state(self.btn_pp_reset_nest, True)
                            def _advance():
                                self._pp_arm_existing_reel(next_idx)
                                self._pp_run_autonest(resume=False, auto_chained=True)
                            self.after(150, _advance)
                            return
                        else:
                            self._pp_log("✓ Nest All complete — every reel has been nested.", "success")
                    # Reached only when there's genuinely nothing left to
                    # chain to (single reel done, or Nest All fully
                    # finished) — the Nest-All-continues branch above
                    # returns early instead of falling through here, so
                    # these stay locked for every reel in between.
                    self._pp_mute_check.set_enabled(True)
                    self._pp_nest_all_check.set_enabled(True)
                    self._set_btn_state(self.btn_pp_run, False)
                    self._pp_set_timeline_dropdown_enabled(not self._pp_nest_all_var.get())
                    self._pp_log(f"✓ Done — {len(created)} sequence(s) created this run.", "success")
                elif paused:
                    self._pp_show_continue_nest_button()
                    # Dropdown stays locked while paused — Reset Nest is the
                    # way to abandon a paused reel and pick something else.
                    self._pp_log(f"⏸ Paused — {len(created)} sequence(s) created this run, "
                                 f"{reel['resume_idx']}/{total} episodes done.", "warn")
                else:
                    self._set_btn_state(self.btn_pp_run, True)
                    self._pp_set_timeline_dropdown_enabled(not self._pp_nest_all_var.get())
                self._set_btn_state(self.btn_pp_reset_nest, True)
                # Re-evaluate against current Nest All state, not whatever it
                # was when this run started — catches Nest All being
                # unchecked mid-run, which used to leave this stuck disabled.
                self._pp_sync_ep_entry_enabled()
            self.after(0, _restore_after_nest)

        except Exception as e:
            import traceback
            traceback.print_exc()
            self.after(0, self._stop_thinking)
            self.after(0, self._restore_reset_btn)
            self.after(0, lambda: setattr(self, "_pp_nesting_active", False))
            self.after(0, lambda: self._pp_set_timeline_dropdown_enabled(True))
            self.after(0, self._pp_sync_ep_entry_enabled)
            _status(f"✗ {e}\n{traceback.format_exc()}", TEXT_ERROR)
            self.after(0, self._pp_resize_window)

    def _pp_on_nest_complete(self):
        """Called when a reel finishes nesting — refresh the (always-active)
        output folder, and unlock Connect to AME (the other unlock path is
        Skip Nest). Deliberately does NOT build the EXPORT QUEUE chips
        here — self._pp_created_seqs already has the just-nested episodes
        (the nest loop appends to it directly), but the chip box itself
        stays empty/hidden until AME is actually connected. Connect to
        AME's first click always runs a project scan (_pp_skip_nest_scan_task)
        regardless of Skip Nest, which calls _pp_build_exp_chips() itself
        and correctly picks up everything nested so far — nesting more
        reels before that first click just keeps growing
        self._pp_created_seqs silently in the meantime."""
        self._pp_set_circle_done(self._pp_circ_p1_2)
        self.after(100, self._pp_resize_window)
        self._pp_detect_output()
        # Fresh episodes just got nested — the utility buttons' "run
        # complete, needs a rescan" lock no longer applies.
        self._pp_exp_run_complete = False
        self._pp_refresh_export_button()
        if not self._pp_ame_connected:
            self._pp_set_circle_active(self._pp_circ_p2_1, "1")
            self._set_btn_state(self.btn_pp_connect_ame, True)

    def _pp_skip_nest_click(self):
        """Bypass Phase 1 entirely — abandons any in-progress nest (every
        reel, not just whichever's selected), locks the whole phase down so
        it can't be mixed with manual nesting later this session, and
        unlocks Connect to AME. Connect to AME then does double duty: it
        scans the project for already-nested FINAL_EP sequences instead of
        just launching AME (see _pp_connect_ame). Relabels itself into
        "Unskip Nest" (stays clickable, doesn't self-disable like most
        buttons here) — clicking it again undoes all of this, see
        _pp_unskip_nest_click."""
        self._pp_skip_nest_mode = True
        if self._pp_current_reel is not None:
            self._pp_reset_nest(force_all=True)
        # Phase 1's reel model is entirely irrelevant once Skip Nest is
        # active (Connect to AME scans the project directly instead) —
        # clear EPISODES NESTED back to its pre-connect empty state rather
        # than leaving whatever reels/chips happened to exist at click time.
        self._pp_destroy_all_nest_chips()
        self._pp_reels = []
        self._pp_current_reel = None
        # Overwrite whatever the dropdown was showing (a reel label, or
        # still "SCANNING" if a prescan was mid-flight) directly here
        # rather than relying on _pp_finish_initial_scan to fix it once
        # the background scan notices and winds down — that only updates
        # the label inside an "if self._pp_current_reel is not None"
        # branch, which we just made false, so it would otherwise stay
        # stuck on "SCANNING" forever even after scanning had genuinely
        # stopped. "SKIPPED" reads clearer locked/greyed than either a
        # stale label or a blank "pick one" placeholder.
        if getattr(self, "_pp_timeline_var", None) is not None:
            self._pp_timeline_var.set("SKIPPED")
        self._pp_resize_window()
        self._pp_set_timeline_dropdown_enabled(False)
        if getattr(self, "_pp_rescan_reels_btn", None) is not None:
            # Rescan Reels (not the dropdown, not Connect to Premiere) is
            # occupying the slot — e.g. Reset Nest or an as-yet-unclicked
            # Unskip Nest left it there. Disable it in place; re-enabled by
            # Unskip Nest or RESET ALL (see _pp_swap_dropdown_for_rescan_button).
            self._set_btn_state(self._pp_rescan_reels_btn, False)
        elif getattr(self, "_pp_timeline_dropdown", None) is None:
            self._set_btn_state(self.btn_pp_connect, False)
        self._set_btn_state(self.btn_pp_run, False)
        self._set_btn_state(self.btn_pp_reset_nest, False)
        self._pp_mute_check.set_enabled(False)
        self._pp_nest_all_check.set_enabled(False)
        self._pp_sync_ep_entry_enabled()
        # Title Card Track only matters for nesting — irrelevant once Phase
        # 1 is skipped, so lock it down the same way (unlike the always-
        # enabled default it had before this).
        if self._pp_track_radios:
            self._pp_track_radios[0].set_locked(True)
        for box in self._pp_track_manual_boxes.values():
            box["dropdown"].set_locked(True)
        for box in self._pp_track_auto_boxes.values():
            box["dropdown"].set_locked(True)
        self._pp_set_circle_active(self._pp_circ_p2_1, "1")
        self._set_btn_state(self.btn_pp_connect_ame, True)
        self._pp_exp_status.config(
            text="Click Connect to AME to scan the project for nested episodes.", fg=TEXT_MUTED)
        self.btn_pp_skip_nest._text = "Unskip Nest"
        self.btn_pp_skip_nest._command = self._pp_unskip_nest_click
        self._set_btn_state(self.btn_pp_skip_nest, True)

    def _pp_unskip_nest_click(self):
        """Undoes Skip Nest — full reset of Phase 1's reel model (nothing
        in it survives Skip Nest being engaged anyway) and all of Phase 2
        (un-connects AME, clears the EXPORT QUEUE and its checkboxes),
        same scope RESET ALL uses for those, except Show Info, Output
        Folder, AND the Premiere connection itself are left exactly as
        they already were. Instead of forcing a fresh Connect to Premiere
        click, the timeline dropdown gets swapped for a "Rescan Reels"
        button in the same slot (see _pp_swap_dropdown_for_rescan_button)
        — click it to pick a reel back up if you want to nest normally
        again. Purely an app-side tracking reset, same caveat as CLEAR
        ALL — anything already sitting in AME's own render queue is
        untouched, this only resets what this app remembers about it."""
        self._pp_full_reset(keep_show_info=True, keep_output_folder=True,
                             keep_premiere_connection=True)

    def _pp_swap_dropdown_for_rescan_button(self):
        """Replaces the timeline dropdown (or, in the rare case Skip Nest
        was used on a connection with zero STRINGOUT timelines, the
        original Connect to Premiere button, which never got destroyed)
        with a "Rescan Reels" button in the same slot — used by Unskip
        Nest, whose reel model is always empty by the time this runs
        (nothing survives Skip Nest being engaged), so there's nothing
        left to select from until a rescan repopulates it."""
        if getattr(self, "_pp_timeline_dropdown", None) is not None:
            self._pp_timeline_dropdown.destroy()
            self._pp_timeline_dropdown = None
        elif getattr(self, "btn_pp_connect", None) is not None and self.btn_pp_connect.winfo_exists():
            self.btn_pp_connect.destroy()
        if getattr(self, "_pp_rescan_reels_btn", None) is not None:
            # Already occupying the slot (e.g. Skip Nest disabled it in
            # place rather than destroying it) — just re-enable it rather
            # than returning early and leaving it stuck disabled.
            self._set_btn_state(self._pp_rescan_reels_btn, True)
            return
        self._pp_rescan_reels_btn = self._rounded_btn(
            self._pp_p1_row, "Rescan Reels", self._pp_rescan_reels_click, min_width=170)
        self._pp_rescan_reels_btn.pack(side="left", padx=(0, 14), before=self._pp_circ_p1_2)

    def _pp_swap_rescan_button_for_dropdown(self):
        """Replaces the "Rescan Reels" button with the real timeline
        dropdown again, once a rescan actually finds reel data —
        repopulated from self._pp_stringout_map, which Unskip Nest never
        touches."""
        if getattr(self, "_pp_rescan_reels_btn", None) is not None:
            self._pp_rescan_reels_btn.destroy()
            self._pp_rescan_reels_btn = None
        if getattr(self, "_pp_timeline_dropdown", None) is None:
            self._pp_timeline_var = tk.StringVar()
            # width=170 — see _pp_show_timeline_dropdown's matching
            # construction for why.
            self._pp_timeline_dropdown = self._canvas_dropdown(
                self._pp_p1_row, self._pp_timeline_var, width=170,
                placeholder="Select timeline...", command=self._pp_on_timeline_selected)
            self._pp_timeline_dropdown.pack(side="left", padx=(0, 14), before=self._pp_circ_p1_2)
        self._pp_timeline_dropdown.set_options(list(self._pp_stringout_map.keys()))

    def _pp_rescan_reels_click(self):
        """Picks the still-open Premiere connection back up after Unskip
        Nest cleared the reel model — the exact same prescan a normal
        Connect to Premiere click triggers, just without needing to
        reconnect first. Swaps to the real dropdown immediately (showing
        "SCANNING", same as a normal Connect) rather than leaving the
        "Rescan Reels" button sitting there disabled for the whole scan —
        _pp_finish_initial_scan then updates it to the real reel label
        once the scan lands, via the exact same path a normal Connect
        already uses (no special-casing needed there for this)."""
        self._pp_swap_rescan_button_for_dropdown()
        self._pp_timeline_var.set("SCANNING")
        self._pp_nest_status.config(text="Scanning reels...", fg=TEXT_MUTED)
        self._pp_prescan_all_reels(arm_first=True)

    def _pp_connect_ame(self):
        """Launch/foreground AME — only actually happens on the very first
        click, every click after just re-triggers the scan below — and
        scan the project for FINAL_EP/trailer sequences not already
        tracked in self._pp_created_seqs, adding any as new EXPORT QUEUE
        chips (unselected). Runs unconditionally on every click, first or
        repeat, regardless of Skip Nest: this is the one mechanism behind
        Skip Nest's initial scan, every "Rescan Episodes" click after,
        AND recovering episodes that CLEAR ALL untracked
        — they're still real, already-nested Premiere sequences, this
        just finds them again instead of requiring a re-nest. Relabels
        itself from "Connect to AME" to "Rescan Episodes" after the first
        successful launch.

        The LIVE/MARKETING/SOCIAL MEDIA/SRT/UPDATE DATE checkboxes are
        deliberately NOT enabled here — _pp_skip_nest_scan_task enables
        them itself, once its scan (and _pp_build_exp_chips) has actually
        run, whether or not this is the first click. They ARE disabled
        here, though — a "Rescan Episodes" click can happen after an
        earlier scan already left them enabled, and they shouldn't stay
        clickable while this new scan is in flight."""
        first_click = not self._pp_ame_connected
        self._set_btn_state(self.btn_pp_connect_ame, False)
        self._pp_style_checks["LIVE"].set_enabled(False)
        self._pp_style_checks["MARKETING"].set_enabled(False)
        self._pp_style_checks["SOCIAL MEDIA"].set_enabled(False)
        self._pp_srt_check.set_enabled(False)
        self._pp_update_date_check.set_enabled(False)
        if first_click:
            self._pp_exp_status.config(text="Launching Adobe Media Encoder...", fg=TEXT_MUTED)
            import subprocess
            try:
                subprocess.Popen(["open", "-a", "Adobe Media Encoder"])
            except Exception as e:
                self._set_btn_state(self.btn_pp_connect_ame, True)
                self._pp_exp_status.config(text=f"✗ Could not launch AME: {e}", fg=TEXT_ERROR)
                return
            self._pp_ame_connected = True
            self._pp_set_circle_done(self._pp_circ_p2_1)
            self._pp_set_circle_active(self._pp_circ_p2_2, "2")
            self.btn_pp_connect_ame._text = "Rescan Episodes"
            self.btn_pp_connect_ame._command = self._pp_connect_ame
        self._pp_exp_status.config(text="Scanning project for nested episodes...", fg=TEXT_MUTED)
        self._pp_ame_scan_cancel = False

        def _on_stop():
            # AME itself was already launched (if this was the first
            # click) before the scan started — that can't be undone and
            # doesn't need to be, only the scan itself is what STOP
            # should cancel. The scan is a single ExtendScript round
            # trip with no mid-flight checkpoint, so this reverts
            # immediately rather than waiting for it to return; the flag
            # tells _pp_skip_nest_scan_task's _apply() to discard its
            # result once it eventually does.
            self._pp_ame_scan_cancel = True
            self._set_btn_state(self.btn_pp_connect_ame, True)
            self._pp_exp_status.config(text="Stopped.", fg=TEXT_WARN)

        self._start_thinking(on_stop=_on_stop)
        import threading
        threading.Thread(target=self._pp_skip_nest_scan_task, daemon=True).start()

    def _pp_skip_nest_scan_task(self):
        """Background scan behind every "Connect to AME"/"Rescan Episodes"
        click, Skip Nest or normal flow alike: finds every sequence in the
        project with "FINAL_EP" in its name and adds any not already
        tracked as new, unadded (grey) EXPORT QUEUE chips — matches the
        naming this app itself uses when nesting episodes. Also looks for
        a trailer timeline (FINAL + TRAILER in the name) and adds it as a
        TRAILER chip at the end of the queue — the trailer is always
        already-nested by the time this runs, so it never goes through
        the normal nest flow, only this scan."""
        def _status(msg, color=TEXT_MUTED):
            self.after(0, lambda m=msg, c=color: self._pp_exp_status.config(text=m, fg=c))
            tag = "success" if color == "#50e050" else ("error" if color == TEXT_ERROR else ("warn" if color == TEXT_WARN else "muted"))
            self._pp_log(msg, tag)
        try:
            # Match on episode NUMBER, not the exact name — names include
            # the nest date, so the same episode renested on a different
            # day would never string-match an entry already in the queue.
            existing_names = {name for name, _, _ in self._pp_created_seqs}
            existing_ep_nums = {self._pp_extract_ep_num(n) for n in existing_names} - {None}
            found = []
            for nm, s in self._pp_scan_project_final_ep_seqs():
                ep_num = self._pp_extract_ep_num(nm)
                if ep_num is not None:
                    if ep_num not in existing_ep_nums:
                        found.append((nm, s))
                elif nm not in existing_names:
                    found.append((nm, s))

            found.sort(key=lambda pair: (self._pp_extract_ep_num(pair[0]) if self._pp_extract_ep_num(pair[0]) is not None else float("inf")))

            trailer_found = None
            if not self._pp_trailer_seq_name or self._pp_trailer_seq_name not in existing_names:
                pair = self._pp_scan_project_trailer_seq()
                if pair is not None and pair[0] not in existing_names:
                    trailer_found = pair

            def _apply():
                if not self._pp_connected or self._pp_ame_scan_cancel:
                    # RESET ALL ran while this background project scan was
                    # still in flight — self._pp_created_seqs was already
                    # emptied by that reset; don't silently repopulate it
                    # with a stale scan's results (surfaced live as phantom
                    # chips appearing after Reset All with nothing
                    # connected) — or STOP cancelled just this scan (see
                    # _pp_ame_scan_cancel), already reverted synchronously,
                    # so there's nothing further to do here either way.
                    return
                for nm, s in found:
                    self._pp_created_seqs.append((nm, s, None))
                    self._pp_exp_disabled.add(nm)
                if trailer_found is not None:
                    nm, s = trailer_found
                    self._pp_created_seqs.append((nm, s, None))
                    self._pp_exp_disabled.add(nm)
                    self._pp_trailer_seq_name = nm
                    self._pp_refresh_social_media_enabled()
                    self._pp_refresh_manual_folder_rows()
                self.btn_pp_connect_ame._text = "Rescan Episodes"
                self.btn_pp_connect_ame._command = self._pp_connect_ame
                self._set_btn_state(self.btn_pp_connect_ame, True)
                # A rescan is the deliberate action that unlocks the
                # utility buttons again after a completed run — see
                # _pp_exp_util_locked.
                self._pp_exp_run_complete = False
                self._pp_build_exp_chips()
                # Only now do the export-queue chips actually exist — safe
                # to unlock the style/SRT/Update Date checkboxes that act on them.
                self._pp_style_checks["LIVE"].set_enabled(True)
                self._pp_style_checks["MARKETING"].set_enabled(True)
                self._pp_refresh_social_media_enabled()
                self._pp_refresh_srt_enabled()
                self._pp_update_date_check.set_enabled(True)
                self._pp_refresh_export_button()
                self._stop_thinking()
                total_new = len(found) + (1 if trailer_found is not None else 0)
                if total_new:
                    _status(f"✓ Found {total_new} new item(s) — added below, unselected. "
                            f"Enable the ones you want to queue.", "#50e050")
                else:
                    _status("✓ Rescanned — no new FINAL_EP episodes or trailer found.", "#50e050")
            self.after(0, _apply)
        except Exception as e:
            if self._pp_ame_scan_cancel:
                return
            import traceback
            traceback.print_exc()
            self.after(0, self._stop_thinking)
            _status(f"✗ Scan failed: {e}", TEXT_ERROR)
            self._set_btn_state(self.btn_pp_connect_ame, True)

    def _pp_refresh_social_media_enabled(self):
        """SOCIAL MEDIA only ever applies to the TRAILER entry (never
        episodes) — enabled only once AME is connected AND a trailer has
        actually been found in the export queue via Skip Nest's scan.
        Forces the checkbox back off (not just visually disabled) when it
        loses eligibility, so a stray previously-checked state can't still
        get exported after the trailer entry is cleared/removed."""
        enabled = self._pp_ame_connected and self._pp_trailer_seq_name is not None
        self._pp_style_checks["SOCIAL MEDIA"].set_enabled(enabled)
        if not enabled:
            self._pp_style_vars["SOCIAL MEDIA"].set(False)

    def _pp_refresh_srt_enabled(self):
        """SRT only ever does anything alongside a checked LIVE pass (see
        _pp_run_export) — enabled only once AME is connected AND LIVE is
        currently checked. Forces the checkbox back off (not just visually
        disabled) the moment LIVE gets unchecked, so a stray previously-
        checked SRT can't silently persist once its dependency is gone."""
        enabled = self._pp_ame_connected and self._pp_style_vars["LIVE"].get()
        self._pp_srt_check.set_enabled(enabled)
        if not enabled:
            self._pp_srt_var.set(False)

    def _pp_refresh_export_button(self):
        """Queue Episodes should be enabled whenever AME is connected and
        there's at least one nested episode not yet successfully queued —
        re-evaluated every time more episodes get nested, not just at the
        moment Connect to AME was clicked, so nesting additional reels after
        an initial queue-all doesn't leave the button stuck disabled."""
        if not self._pp_ame_connected or self._pp_exp_started:
            return
        has_unqueued = any(name not in self._pp_exp_queued for name, _, _ in self._pp_created_seqs)
        self._set_btn_state(self.btn_pp_export, has_unqueued)

    def _pp_find_ame_preset(self, style="LIVE"):
        """Locate the {style}.epr preset, without prompting (e.g. LIVE.epr,
        MARKETING.epr, "SOCIAL MEDIA.epr", "LIVE WITH SRTs.epr"). Checks the
        copy bundled alongside this app first — same folder as main.py
        (or, once packaged, Contents/Resources — same lookup already used
        for thinking.gif) — so the app is self-sufficient regardless of
        whether AME's own preset folder has ever seen it. Only falls back
        to AME's own per-user preset folder (version-agnostic glob, so it
        survives AME updates) if no bundled copy exists."""
        script_dir = os.path.dirname(os.path.abspath(__file__)) if "__file__" in dir() else None
        if script_dir:
            bundled = os.path.join(script_dir, f"{style}.epr")
            if os.path.exists(bundled):
                return bundled
        import glob
        matches = sorted(glob.glob(os.path.expanduser(
            f"~/Documents/Adobe/Adobe Media Encoder/*/Presets/{style}.epr")))
        return matches[-1] if matches else None

    # ── Per-style export rules (LIVE / MARKETING / SOCIAL MEDIA) ────────────
    # Detection runs on an already-nested episode subsequence at queue time,
    # not on the master stringout timeline — by the time an episode has been
    # nested down to its own subsequence, there's no episode-title-card
    # track to confuse with the lower-third titlecards track the way there
    # would be on the master timeline, so a simpler position-relative scan
    # (relative to a "_COLOR" reference clip) is enough to find it.

    def _pp_find_color_ref_track(self, seq):
        """Video track index containing a clip with "_COLOR" in its name —
        the reference point the lower-third titlecards track is found
        relative to (it sits somewhere above this)."""
        num_tracks = seq.videoTracks.numTracks
        for t in range(num_tracks):
            track = seq.videoTracks[t]
            for c in range(track.clips.numItems):
                name = str(track.clips[c].name or "")
                if "_color" in name.lower():
                    return t
        return None

    def _pp_find_lower_third_track(self, seq):
        """Video track above the _COLOR reference clip's track where every
        one of the first 4 clips is a "Graphic" (MOGRT) clip — the
        lower-third character-name-card track MARKETING/SOCIAL MEDIA need
        kept visible."""
        color_track = self._pp_find_color_ref_track(seq)
        if color_track is None:
            return None
        num_tracks = seq.videoTracks.numTracks
        for t in range(color_track + 1, num_tracks):
            track = seq.videoTracks[t]
            n_clips = track.clips.numItems
            if n_clips == 0:
                continue
            sample = min(4, n_clips)
            names = [str(track.clips[c].name or "").strip().upper() for c in range(sample)]
            if names and all(n == "GRAPHIC" for n in names):
                return t
        return None

    def _pp_find_music_track(self, seq):
        """Audio track containing a clip with "mx" in its name (matched as
        an underscore-delimited segment, same convention as REF detection,
        to avoid matching an unrelated clip that merely contains "mx"
        somewhere in a longer word) — one matching clip is enough to call
        the whole track Music."""
        num_tracks = seq.audioTracks.numTracks
        for t in range(num_tracks):
            track = seq.audioTracks[t]
            for c in range(track.clips.numItems):
                name = str(track.clips[c].name or "")
                parts = name.lower().split("_")
                if any(p == "mx" or p.startswith("mx") for p in parts):
                    return t
        return None

    def _pp_find_watermark_track(self, seq):
        """Video track containing a clip whose name starts with "watermark"
        (case-insensitive, stripped) — the overlay track SOCIAL MEDIA's two
        variants (_SM / _SM_WM) toggle on and off. The real clip is a .png
        still whose name is the source filename (e.g. "Watermark.png"), not
        the bare word — startswith (not an exact match) so the extension
        and any versioning suffix don't break detection.

        Logs a warning when nothing matches (e.g. the .png could be sitting
        inside a NESTED sequence on that track rather than as a direct
        top-level clip, which this scan — top-level clips only — wouldn't
        see)."""
        num_tracks = seq.videoTracks.numTracks
        for t in range(num_tracks):
            track = seq.videoTracks[t]
            for c in range(track.clips.numItems):
                name = str(track.clips[c].name or "").strip()
                if name.lower().startswith("watermark"):
                    return t
        self._pp_log(f"  ⚠ No Watermark clip found on {seq.name} — "
                      f"Social Media export will run without it.", "warn")
        return None

    def _pp_find_marketing_trim_point(self, seq):
        """Earliest start tick, across every video track, of a clip whose
        name (case-insensitive) contains "poster", "coming soon", or
        "coming-soon" AND whose filename ends in .jpg/.jpeg/.png — the
        point MARKETING's out point gets trimmed to. Returns None if no
        such clip exists, in which case MARKETING falls back to exporting
        the entire episode."""
        markers = ("poster", "coming soon", "coming-soon")
        exts = (".jpg", ".jpeg", ".png")
        earliest = None
        num_tracks = seq.videoTracks.numTracks
        for t in range(num_tracks):
            track = seq.videoTracks[t]
            for c in range(track.clips.numItems):
                clip = track.clips[c]
                name = str(clip.name or "").strip().lower()
                if not name.endswith(exts):
                    continue
                if not any(m in name for m in markers):
                    continue
                start = int(clip.start.ticks)
                if earliest is None or start < earliest:
                    earliest = start
        return earliest

    def _pp_build_export_variants(self, styles, is_trailer):
        """Expands checked styles into (style, suffix, variant_key) triples
        to queue for one queue entry. LIVE/MARKETING apply to either an
        episode or the trailer, one variant each. SOCIAL MEDIA only ever
        applies to the trailer (is_trailer=True) — it expands into two
        variants there, _SM (Watermark track muted) and _SM_WM (Watermark
        track unmuted) — and is skipped entirely for episodes."""
        variants = []
        for style in styles:
            if style == "LIVE":
                variants.append(("LIVE", "", "live"))
            elif style == "MARKETING":
                variants.append(("MARKETING", "_M", "marketing"))
            elif style == "SOCIAL MEDIA" and is_trailer:
                variants.append(("SOCIAL MEDIA", "_SM", "social_muted"))
                variants.append(("SOCIAL MEDIA", "_SM_WM", "social_unmuted"))
        return variants

    def _pp_set_track_muted(self, seq_obj, track_type, track_idx, muted):
        """Sets a single track's own mute state directly (Track.setMute) —
        a true whole-track toggle, not per-clip — same API already used
        to un-mute every track before nesting (see the track-targeting
        step in _pp_autonest_task), which works identically for
        "videoTracks" and "audioTracks" despite video tracks not having a
        visible "mute" icon in the UI the way audio does."""
        from pymiere.core import eval_script as _es
        ref = "$._pymiere['{}']".format(seq_obj._pymiere_id)
        val = "1" if muted else "0"
        _es(f"{ref}.{track_type}[{track_idx}].setMute({val});")

    def _pp_create_subsequence_batched(self, seq, in_sec, out_sec, name):
        """Sets in/out points, creates the subsequence, zeroes it, and
        names it — all in one ExtendScript round trip instead of five
        (setInPoint, setOutPoint, createSubsequence, setZeroPoint, name=)
        each being its own separate call/network round trip normally.
        Reuses pymiere's own (private but stable — every public call
        already goes through them internally) object-registration helpers
        so the returned Sequence is a normal, fully-usable pymiere object,
        not a hand-rolled stand-in. moveBin is deliberately left OUT of
        this batch and still called separately by the caller, same as
        before — it already degrades gracefully (a warning, nesting
        continues) and there's no need to risk that behavior for one more
        round trip saved.

        Raises on any failure — callers should fall back to the five
        separate calls this replaces if this raises, since it reaches one
        level deeper into pymiere's internals than a normal call and
        hasn't been exercised against every Premiere version."""
        import json
        from pymiere.core import _eval_script_returning_object, _format_object_to_py
        seq_ref = "$._pymiere['{}']".format(seq._pymiere_id)
        script = (
            "(function(){{"
            "var __s={seq_ref};"
            "__s.setInPoint({in_sec});"
            "__s.setOutPoint({out_sec});"
            "var __sub=__s.createSubsequence(false);"
            "__sub.setZeroPoint('0');"
            "__sub.name={name_json};"
            "return __sub;"
            "}})()"
        ).format(seq_ref=seq_ref, in_sec=repr(float(in_sec)), out_sec=repr(float(out_sec)),
                  name_json=json.dumps(name))
        result = _eval_script_returning_object(script)
        return _format_object_to_py(result)

    def _pp_set_video_tracks_muted_upto(self, seq_obj, track_idx_upto, muted):
        """Video tracks 0 through track_idx_upto inclusive — "mute this
        track and everything below it" for VIDEO, where track 0 (V1) is
        the bottom of the stack and higher indices are progressively
        higher layers, so "below" means lower indices."""
        for t in range(track_idx_upto + 1):
            self._pp_set_track_muted(seq_obj, "videoTracks", t, muted)

    def _pp_set_audio_tracks_muted_from(self, seq_obj, track_idx_from, muted):
        """Audio track track_idx_from through the last one — "mute this
        track and everything below it" for AUDIO, where track 0 (A1) sits
        at the TOP of the audio section and higher indices are
        progressively lower, so "below" means higher indices — the
        opposite of video."""
        num_tracks = seq_obj.audioTracks.numTracks
        for t in range(track_idx_from, num_tracks):
            self._pp_set_track_muted(seq_obj, "audioTracks", t, muted)

    def _pp_apply_variant_mute_state(self, sub, style, variant_key, is_trailer=False):
        """Sets up the mute state for one export variant on an
        already-nested episode subsequence, right before it gets queued.
        Always starts from a clean, fully-unmuted baseline (every video
        and audio track's own mute state, via Track.setMute — not a
        per-clip toggle) so a previous variant's pass on this same
        subsequence (earlier in the same queue run) can't leak into the
        next one, and LIVE always gets every track turned on regardless
        of what MARKETING/SOCIAL MEDIA left behind — except Watermark
        (see below). MARKETING additionally mutes the Music track (an
        AUDIO track) and everything below it, and also mutes the
        lower-third titlecards/graphics track (a VIDEO track).

        Watermark is muted by default for every style/variant — LIVE,
        MARKETING, and SOCIAL MEDIA's "_SM" (social_muted) — only the
        "_SM_WM" (social_unmuted) variant shows it. Only the trailer is
        ever expected to actually have this track — regular episodes
        never carry a Watermark clip at all — so is_trailer gates the
        whole check (an extra ExtendScript round trip per episode per
        style pass otherwise) instead of running it unconditionally and
        relying on _pp_find_watermark_track returning None as a no-op
        for every regular episode, every single pass."""
        num_video = sub.videoTracks.numTracks
        num_audio = sub.audioTracks.numTracks
        if num_video > 0:
            self._pp_set_video_tracks_muted_upto(sub, num_video - 1, False)
        if num_audio > 0:
            self._pp_set_audio_tracks_muted_from(sub, 0, False)
        if style == "MARKETING":
            music_track = self._pp_find_music_track(sub)
            if music_track is not None:
                self._pp_set_audio_tracks_muted_from(sub, music_track, True)
            lower_third_track = self._pp_find_lower_third_track(sub)
            if lower_third_track is not None:
                self._pp_set_track_muted(sub, "videoTracks", lower_third_track, True)
        if not is_trailer:
            return
        watermark_track = self._pp_find_watermark_track(sub)
        if watermark_track is not None:
            # Logged so it's visible (not silent) whether it was found and
            # which way it got set, since a wrong mute state here is easy
            # to miss until the render's already out.
            show_watermark = (style == "SOCIAL MEDIA" and variant_key == "social_unmuted")
            self._pp_set_track_muted(sub, "videoTracks", watermark_track, not show_watermark)
            self._pp_log(f"  watermark track V{watermark_track + 1} found on {sub.name} — "
                         f"{'shown' if show_watermark else 'muted'} for {style}"
                         f"{'/' + variant_key if style == 'SOCIAL MEDIA' else ''}.", "muted")

    # Ready (not yet queued) matches nest chips' done green. Each style
    # gets its own "currently being queued" color, since the export queue
    # processes one whole style pass at a time across every item (not one
    # item through all its styles) — an item's chip should only light up
    # while THAT style's pass is actually touching it, then drop to a
    # dimmed version of that SAME color (plus a lock glyph) once that
    # pass is done but more are still pending for this item — not a
    # generic grey, so it still reads as "this was the LIVE/MARKETING/etc
    # pass" at a glance. Once the LAST applicable pass finishes, it goes
    # to the final solid-blue "done" state with a checkmark, unrelated to
    # the lock glyph — a stronger, different signal (fully complete vs.
    # one pass down, more to go).
    EXP_READY_BG,  EXP_READY_TXT               = "#1a3a1a", "#5ae05a"
    EXP_ACTIVE_BG, EXP_ACTIVE_OUTLINE, EXP_ACTIVE_TXT = "#1a3355", "#7ac2ff", "#a8daff"
    EXP_LIVE_BG,   EXP_LIVE_OUTLINE,   EXP_LIVE_TXT   = "#3d3418", "#e0c040", "#f5dc7a"
    EXP_MKT_BG,    EXP_MKT_OUTLINE,    EXP_MKT_TXT    = "#1a3355", "#7ac2ff", "#a8daff"
    EXP_SM_BG,     EXP_SM_OUTLINE,     EXP_SM_TXT     = "#301a40", "#b57af0", "#dcb3f7"
    EXP_LIVE_DIM_BG, EXP_LIVE_DIM_TXT = "#332c14", "#a8903c"
    EXP_MKT_DIM_BG,  EXP_MKT_DIM_TXT  = "#16283f", "#5a86b0"
    EXP_SM_DIM_BG,   EXP_SM_DIM_TXT   = "#241530", "#8a5cb0"
    EXP_DISABLED_BG, EXP_DISABLED_TXT          = "#3d3d3d", "#888888"

    def _pp_exp_style_colors(self, style):
        """(bg, outline, txt) for one style's chip color — shared by the
        active, done, and rebuild-from-_pp_exp_queued paths, so "done"
        never needs a separate uniform color: it's just this same palette,
        settled (no outline) plus a checkmark, for whichever style's pass
        actually finished the item last."""
        return {
            "LIVE": (self.EXP_LIVE_BG, self.EXP_LIVE_OUTLINE, self.EXP_LIVE_TXT),
            "MARKETING": (self.EXP_MKT_BG, self.EXP_MKT_OUTLINE, self.EXP_MKT_TXT),
            "SOCIAL MEDIA": (self.EXP_SM_BG, self.EXP_SM_OUTLINE, self.EXP_SM_TXT),
        }.get(style, (self.EXP_ACTIVE_BG, self.EXP_ACTIVE_OUTLINE, self.EXP_ACTIVE_TXT))

    EXP_PROGRESS_DEFAULT = "#e0c040"

    def _pp_set_exp_progress_color(self, style):
        """Recolors the Phase 2 progress bar to match whichever style is
        currently being queued — same outline color already used for that
        style's export chips (_pp_exp_style_colors), so the bar and the
        chips it's tracking read consistently: gold while queueing LIVE,
        blue for MARKETING, purple for SOCIAL MEDIA."""
        _, outline, _ = self._pp_exp_style_colors(style)
        ttk.Style().configure("PPExp.Horizontal.TProgressbar", background=outline)

    def _pp_reset_exp_progress_color(self):
        ttk.Style().configure("PPExp.Horizontal.TProgressbar", background=self.EXP_PROGRESS_DEFAULT)

    def _pp_build_exp_chips(self):
        """Rebuilds every EXPORT QUEUE chip from self._pp_created_seqs +
        self._pp_exp_disabled/self._pp_exp_queued — mirrors the VFX tab's
        DETECTED EPISODES tag mechanics (toggle glyph, divider). Chips in
        self._pp_exp_queued were successfully queued in a prior run — they
        stay permanently "done" (solid blue) and non-interactive, even
        after more episodes are nested and this rebuilds again.

        Sorted by episode number every time, in place, before rendering —
        self._pp_created_seqs is appended to from more than one place (a
        fresh nest run, and a Skip Nest/Rescan Episodes scan that discovers
        already-nested episodes still sitting in the project), and each of
        those just appends to whatever's already there. An episode
        discovered later that numbers earlier than ones already in the
        list would otherwise render after them — this keeps the visible
        chip order (and the order episodes get queued to AME) chronological
        regardless of nest/discovery order. TRAILER (no parseable episode
        number) always sorts to the end, matching where it's meant to sit."""
        self._pp_created_seqs.sort(
            key=lambda e: self._pp_extract_ep_num(e[0])
            if self._pp_extract_ep_num(e[0]) is not None else float("inf"))
        for w in list(self._pp_exp_chips_frame.winfo_children()):
            w.destroy()
        # A Frame with zero packed children won't shrink back down even
        # after update_idletasks()/geometry() — same Tk quirk worked around
        # elsewhere in this file. A permanent zero-size child survives the
        # empty-queue case (e.g. right after CLEAR ALL) so the window
        # actually resizes instead of leaving a tall gap behind.
        tk.Frame(self._pp_exp_chips_frame, bg="#252525", width=0, height=0).pack()
        self._pp_exp_chip_canvases = []
        import tkinter.font as tkfont
        label_font = tkfont.Font(family="SF Pro Display", size=10, weight="bold")
        th, r, gap, glyph_zone = 24, 6, 5, 23
        ch = th + 2
        # -6, not the -16 the other two chip-wrapping spots in this file
        # use — that extra margin was leaving ~10px of genuinely unused
        # space to the right of a full row's last chip here specifically.
        # Reclaimed instead of just growing the window to cover it — see
        # APP_WIDTH's comment for how this feeds into that number.
        avail = max(self._pp_exp_chips_frame.winfo_width() - 6, 800)
        row = None
        row_used = 0
        for idx, (name, sub, reel_idx) in enumerate(self._pp_created_seqs):
            if name == self._pp_trailer_seq_name:
                ep_label = "TRAILER"
            else:
                ep_match = re.search(r'EP(\d+)', name)
                ep_label = f"EP{int(ep_match.group(1)):02d}" if ep_match else name
            # Size each chip to its own label — a fixed 72px worked fine for
            # "EP07" but clipped longer labels like "TRAILER" mid-character.
            tw = max(72, glyph_zone + label_font.measure(ep_label) + 18)
            cw = tw + 2
            if row is None or row_used + cw + gap > avail:
                row = tk.Frame(self._pp_exp_chips_frame, bg="#252525")
                row.pack(fill="x", pady=(0, 2))
                row_used = 0
            row_used += cw + gap
            queued = name in self._pp_exp_queued
            disabled = name in self._pp_exp_disabled
            interactive = not self._pp_exp_started and not queued

            if queued:
                tag_fill, _outline, text_fill = self._pp_exp_style_colors(
                    self._pp_exp_done_style.get(name, "LIVE"))
                outline, toggle_char = "", "✓"
            elif disabled:
                # Once Export has actually started, a manually-excluded
                # item is locked out for the rest of this run (never
                # gets processed) — same "⊘" glyph as the VFX tab uses
                # for "locked during export", instead of the pre-export
                # "+" (still toggleable) glyph.
                tag_fill, outline, text_fill = self.EXP_DISABLED_BG, "", self.EXP_DISABLED_TXT
                toggle_char = "⊘" if self._pp_exp_started else "+"
            else:
                tag_fill, outline, text_fill, toggle_char = self.EXP_READY_BG, "", self.EXP_READY_TXT, "×"

            c = tk.Canvas(row, bg="#252525", highlightthickness=0, width=cw, height=ch)
            c.pack(side="left", padx=(0, gap), pady=2)
            x1, y1, x2, y2 = 1, 1, tw + 1, th + 1
            pts = [x1+r,y1, x2-r,y1, x2,y1, x2,y1+r,
                   x2,y2-r, x2,y2, x2-r,y2, x1+r,y2,
                   x1,y2, x1,y2-r, x1,y1+r, x1,y1]
            c.create_polygon(pts, fill=tag_fill, outline=outline, width=1.5, smooth=True, tags="bg")
            c.create_text(13, ch//2, text=toggle_char,
                          font=("SF Pro Display", 12, "bold"), fill=text_fill, tags="glyph")
            c.create_line(glyph_zone, 5, glyph_zone, ch-5, fill=text_fill, tags="div")
            c.create_text(glyph_zone + (cw-glyph_zone)//2, ch//2, text=ep_label,
                          font=("SF Pro Display", 10, "bold"), fill=text_fill, tags="txt")

            if interactive:
                def _on_click(e, n=name):
                    if self._pp_exp_started or n in self._pp_exp_queued:
                        return
                    if n in self._pp_exp_disabled:
                        self._pp_exp_disabled.discard(n)
                    else:
                        self._pp_exp_disabled.add(n)
                    self._pp_build_exp_chips()
                c.bind("<ButtonRelease-1>", _on_click)
            self._pp_exp_chip_canvases.append(c)
        # Keep the DISABLE ALL/ENABLE ALL label truthful to what's actually
        # on screen — derived from the chips every rebuild instead of only
        # tracked through the bulk-toggle button, so paths that disable
        # chips another way (e.g. Skip Nest's scan defaulting everything to
        # unadded) don't leave the label showing the wrong next action.
        # An empty queue has nothing toggleable, so it reads DISABLE ALL
        # (the "nothing disabled yet" state) by default — the button
        # itself is greyed out/inert until there's actually something to
        # act on, via _pp_refresh_exp_util_buttons below, rather than the
        # label text trying to carry that signal.
        toggleable = [name for name, _, _ in self._pp_created_seqs if name not in self._pp_exp_queued]
        self._pp_exp_all_disabled = bool(toggleable) and all(n in self._pp_exp_disabled for n in toggleable)
        self.btn_toggle_exp_all.config(
            text="ENABLE ALL" if self._pp_exp_all_disabled else "DISABLE ALL")
        self._pp_refresh_exp_clear_buttons()
        self._pp_refresh_exp_util_buttons()
        self._pp_resize_window()

    def _pp_refresh_exp_util_buttons(self):
        """Greys out DISABLE ALL/ENABLE ALL and CLEAR ALL whenever there's
        nothing for them to safely act on: an empty queue, a run actively
        in flight (_pp_exporting_active — mutating self._pp_created_seqs
        mid-run would crash the background export thread, which snapshots
        it by index at the start), or a run that just finished completely
        (_pp_exp_run_complete) — that last one is a deliberate UX choice,
        not a safety one: once queueing is fully done, these require a
        fresh scan/nest before they unlock again, not just a Reset
        Export/Reset All click. Doesn't touch _pp_exp_started (that's what
        actually blocks the click — see each handler), just the visual
        affordance."""
        locked = (self._pp_exporting_active or self._pp_exp_run_complete
                  or not self._pp_created_seqs)
        bg, fg = ("#2a2a2a", "#555555") if locked else (BG_INPUT, TEXT_PRIMARY)
        self.btn_toggle_exp_all.config(bg=bg, fg=fg)
        self.btn_clear_all_exp.config(bg=bg, fg=fg)

    def _pp_set_exp_chip_active(self, idx, style="LIVE"):
        """Lights up with whichever style's pass is currently touching
        this item — LIVE yellow, MARKETING blue, SOCIAL MEDIA purple —
        rather than one generic "active" color, so it's visually obvious
        which pass is running across the whole queue at any moment."""
        if idx < len(self._pp_exp_chip_canvases):
            c = self._pp_exp_chip_canvases[idx]
            bg, outline, txt = self._pp_exp_style_colors(style)
            c.itemconfig("bg", fill=bg, outline=outline)
            c.itemconfig("txt", fill=txt)
            c.itemconfig("glyph", fill=txt)
            c.itemconfig("div", fill=txt)

    def _pp_set_exp_chip_waiting(self, idx, style="LIVE"):
        """Between passes — this item's current style just finished
        queueing but at least one more style is still pending for it.
        Dimmed version of that same style's color (solid fill, no
        outline, muted text — same relationship ACTIVE has to DONE) plus
        the same "⊘" locked glyph the VFX tab's episode chips already use
        while an export is running, rather than a generic grey or a new
        symbol — so it still reads as "this was the LIVE/MARKETING/etc
        pass" at a glance, and "locked/not interactive right now" matches
        an established convention instead of introducing a new one. Only
        ever shown mid-run — the final "every style done" state uses the
        checkmark instead, a stronger/different signal."""
        if idx < len(self._pp_exp_chip_canvases):
            c = self._pp_exp_chip_canvases[idx]
            bg, txt = {
                "LIVE": (self.EXP_LIVE_DIM_BG, self.EXP_LIVE_DIM_TXT),
                "MARKETING": (self.EXP_MKT_DIM_BG, self.EXP_MKT_DIM_TXT),
                "SOCIAL MEDIA": (self.EXP_SM_DIM_BG, self.EXP_SM_DIM_TXT),
            }.get(style, (self.EXP_DISABLED_BG, self.EXP_DISABLED_TXT))
            c.itemconfig("bg", fill=bg, outline="")
            c.itemconfig("txt", fill=txt)
            c.itemconfig("glyph", fill=txt, text="⊘")
            c.itemconfig("div", fill=txt)

    def _pp_set_exp_chip_done(self, idx, style="LIVE"):
        """Fully done — every applicable style pass for this item has
        finished. No separate uniform "done" color — uses whichever
        style's pass finished it last (same palette as the active state,
        settled/no outline, plus a checkmark), so e.g. an item only ever
        touched by LIVE ends up yellow+check, one touched by LIVE then
        MARKETING ends up blue+check (MARKETING ran last), and the
        trailer (last touched by SOCIAL MEDIA) ends up purple+check."""
        if idx < len(self._pp_exp_chip_canvases):
            c = self._pp_exp_chip_canvases[idx]
            if idx < len(self._pp_created_seqs):
                self._pp_exp_done_style[self._pp_created_seqs[idx][0]] = style
            bg, _outline, txt = self._pp_exp_style_colors(style)
            c.itemconfig("bg", fill=bg, outline="")
            c.itemconfig("txt", fill=txt)
            c.itemconfig("glyph", fill=txt, text="✓")
            c.itemconfig("div", fill=txt)

    def _pp_exp_util_locked(self):
        """Shared guard for DISABLE ALL/ENABLE ALL, CLEAR ALL, and CLEAR
        LAST REEL — nothing to safely act on (empty queue), a run
        actively in flight (mutating self._pp_created_seqs mid-run would
        crash the background export thread), or a run that just finished
        completely (deliberate — requires a fresh scan/nest to unlock
        again, not just Reset Queue/Reset All). Not the same as
        _pp_exp_started, which stays True permanently from the first
        Queue Episodes click onward and drives the separate "⊘ lock" on
        excluded chips."""
        return (self._pp_exporting_active or self._pp_exp_run_complete
                or not self._pp_created_seqs)

    def _pp_toggle_all_exp(self):
        if self._pp_exp_util_locked():
            return
        toggleable = {name for name, _, _ in self._pp_created_seqs if name not in self._pp_exp_queued}
        if self._pp_exp_all_disabled:
            self._pp_exp_disabled -= toggleable
        else:
            self._pp_exp_disabled |= toggleable
        self._pp_build_exp_chips()

    def _pp_clear_all_exp(self):
        """Wipes the entire export queue — every chip, including ones
        already successfully queued to AME. Purely an app-side tracking
        reset; has no effect on jobs already sitting in AME's own queue.
        See _pp_exp_util_locked for why this isn't gated on _pp_exp_started."""
        if self._pp_exp_util_locked():
            return
        self._pp_created_seqs = []
        self._pp_exp_queued = set()
        self._pp_exp_disabled = set()
        self._pp_exp_done_style = {}
        self._pp_exp_started = False
        self._pp_trailer_seq_name = None
        self._pp_refresh_social_media_enabled()
        self._pp_refresh_manual_folder_rows()
        self._pp_build_exp_chips()
        self._pp_refresh_export_button()

    def _pp_refresh_exp_clear_buttons(self):
        """Shows/hides CLEAR ALL based on whether there's actually anything
        in the queue to clear, and keeps DISABLE ALL/ENABLE ALL to its LEFT
        — both flush to the box's right edge, with a gap between them but
        no extra padding at the group's own outer edges.

        For side="right" packing, whichever widget is packed FIRST ends up
        visually RIGHTMOST — so both widgets get unpacked and re-packed in
        the correct order on every call: CLEAR ALL first (rightmost, flush
        to the edge), then DISABLE ALL/ENABLE ALL (ends up to its left)."""
        self.btn_clear_all_exp.pack_forget()
        if self._pp_created_seqs:
            self.btn_clear_all_exp.pack(side="right", padx=(8, 0))

        self.btn_toggle_exp_all.pack_forget()
        self.btn_toggle_exp_all.pack(side="right")

    def _pp_resolve_style_presets(self, styles):
        """Resolves (auto-find or prompt, then caches for the rest of the
        session) an AME preset path for each style in `styles`. A style
        whose preset can't be found automatically AND whose manual picker
        gets cancelled is dropped rather than aborting the whole run.
        Returns (resolved_styles, preset_paths, dropped_styles)."""
        from tkinter import filedialog
        preset_paths = {}
        dropped = []
        for style in styles:
            if style in self._pp_ame_preset_paths:
                preset_paths[style] = self._pp_ame_preset_paths[style]
                continue
            found = self._pp_find_ame_preset(style)
            if found:
                self._pp_ame_preset_paths[style] = found
                preset_paths[style] = found
                self._pp_log(f"Using auto-detected {style} preset: {found}", "muted")
            else:
                path = filedialog.askopenfilename(
                    title=f"Couldn't auto-find {style}.epr — select it manually",
                    filetypes=[("Encoder Preset", "*.epr")])
                if path:
                    self._pp_ame_preset_paths[style] = path
                    preset_paths[style] = path
                    self._pp_log(f"Using manually-selected {style} preset: {path}", "muted")
                else:
                    dropped.append(style)
        resolved = [s for s in styles if s not in dropped]
        return resolved, preset_paths, dropped

    def _pp_register_srt_watch_target(self, out_path_no_ext, srt_dest_dir):
        """Adds one expected .srt sidecar to the background watcher's list
        and makes sure the watcher is running. out_path_no_ext is the
        video's own output path with no extension (see _pp_export_task) —
        the caption sidecar's exact filename isn't fully predictable up
        front: AME names it by taking the FULL video filename (base name
        plus whatever real extension it picked, e.g. .mp4) and appending
        .srt on top of that whole thing (confirmed against a real export:
        NAME.mp4.srt, not NAME.srt) — so the watcher globs for it and
        strips that redundant middle extension on move, producing a clean
        {base}.srt instead of {base}.mp4.srt. Destination folder is
        captured now, at queue time — not read live from
        self._pp_srt_out_dir later — so a Reset All while the render is
        still pending in AME can't orphan or misroute this move."""
        with self._pp_srt_watch_lock:
            self._pp_srt_watch_targets.append((out_path_no_ext, srt_dest_dir))
        self._pp_start_srt_watcher()

    def _pp_start_srt_watcher(self):
        """Starts the one persistent background watcher, if it isn't
        already running. Never triggers rendering itself — this app only
        ever queues to AME, so it purely reacts to .srt files eventually
        showing up on disk once you render, whenever that happens to be,
        and moves each one to the SRT output folder as soon as it does."""
        if self._pp_srt_watcher_running:
            return
        self._pp_srt_watcher_running = True
        threading.Thread(target=self._pp_srt_watcher_task, daemon=True).start()

    def _pp_srt_watcher_task(self):
        """Polls every 5s for each expected .srt to appear — once AME has
        finished exporting it, it just shows up on disk — then moves it
        straight to the SRT output folder, renamed to strip the redundant
        video extension AME bakes into the sidecar name."""
        import time, glob, shutil
        while True:
            with self._pp_srt_watch_lock:
                targets = list(self._pp_srt_watch_targets)
            for out_path_no_ext, dest_dir in targets:
                matches = sorted(glob.glob(f"{out_path_no_ext}*.srt"))
                if not matches:
                    continue
                src = matches[0]
                clean_name = os.path.basename(out_path_no_ext) + ".srt"
                try:
                    os.makedirs(dest_dir, exist_ok=True)
                    dest_path = os.path.join(dest_dir, clean_name)
                    shutil.move(src, dest_path)
                    self.after(0, lambda n=clean_name: self._pp_log(
                        f"✓ Moved {n} to SRT folder.", "success"))
                    with self._pp_srt_watch_lock:
                        if (out_path_no_ext, dest_dir) in self._pp_srt_watch_targets:
                            self._pp_srt_watch_targets.remove((out_path_no_ext, dest_dir))
                except Exception as e:
                    self.after(0, lambda s=src, err=e: self._pp_log(
                        f"⚠ Could not move {os.path.basename(s)}: {err}", "warn"))
                    # Left in the list — retried next cycle rather than
                    # dropped, since the failure may be transient (e.g.
                    # the file still being written by AME).
            time.sleep(5)

    def _pp_run_export(self, resume=False):
        """Queue nested episode sequences (and the TRAILER entry, if Skip
        Nest found one) to Adobe Media Encoder via the pymiere Encoder
        bridge. Does not start rendering — per the documented workflow the
        user hits render in AME manually once everything's queued.

        Each queue entry gets queued once per checked style that applies
        to it — LIVE/MARKETING apply to episodes and the trailer alike;
        SOCIAL MEDIA only ever applies to the trailer, expanding into its
        two Watermark variants there. All variants run in turn on the same
        subsequence — mute state (and, for MARKETING, the in/out trim) is
        set for whichever variant's turn it is, queued, then the next
        variant's state gets applied, and so on. Never a separate
        subsequence copy per variant, so QC fixes only ever need to happen
        in one place. Episodes and the trailer route to different output
        folders — see _pp_marketing_out_dir / _pp_trailer_out_dir."""
        if not self._pp_created_seqs:
            self._pp_exp_status.config(text="✗ No nested episodes to queue yet.", fg=TEXT_ERROR)
            return

        styles = [s for s in ("LIVE", "MARKETING", "SOCIAL MEDIA") if self._pp_style_vars[s].get()]
        if not styles:
            self._pp_exp_status.config(
                text="✗ Check at least one export style (LIVE/MARKETING/SOCIAL MEDIA).", fg=TEXT_ERROR)
            return

        # Episodes and the TRAILER entry (if any) route to different output
        # folders — only require the folders actually needed for what's
        # about to be queued this run.
        included_names = [name for name, _, _ in self._pp_created_seqs
                           if name not in self._pp_exp_disabled and name not in self._pp_exp_queued]
        has_trailer = self._pp_trailer_seq_name is not None and self._pp_trailer_seq_name in included_names
        has_episode = any(nm != self._pp_trailer_seq_name for nm in included_names)
        # SRT only ever does anything alongside a checked LIVE pass — it
        # swaps which preset that pass uses (see below), it never adds a
        # separate pass of its own.
        srt_active = "LIVE" in styles and self._pp_srt_var.get()

        missing = []
        if has_episode and "LIVE" in styles and not self._pp_out_dir.get().strip():
            missing.append("LIVE output folder")
        if has_episode and "MARKETING" in styles and not self._pp_marketing_out_dir.get().strip():
            missing.append("MARKETING output folder")
        if has_trailer and not self._pp_trailer_out_dir.get().strip():
            missing.append("TRAILER output folder")
        # The trailer's SRT rides along in the TRAILER output folder
        # (already validated above) — only episodes need the separate SRT
        # output folder.
        if srt_active and has_episode and not self._pp_srt_out_dir.get().strip():
            missing.append("SRT output folder")
        if missing:
            self._pp_exp_status.config(text=f"✗ Missing: {', '.join(missing)}.", fg=TEXT_ERROR)
            return

        styles, preset_paths, dropped = self._pp_resolve_style_presets(styles)
        if dropped:
            self._pp_log(f"⚠ Skipping {', '.join(dropped)} — no preset selected.", "warn")
        if not styles:
            self._pp_exp_status.config(
                text="✗ No usable export style — every preset selection was cancelled.", fg=TEXT_ERROR)
            return

        # Swap the LIVE pass's preset for "LIVE WITH SRTs" — one pass, not
        # an extra one — so episodes/trailer that would've gotten a plain
        # LIVE export get this instead, which also renders an .srt sidecar
        # the background watcher (_pp_start_srt_watcher) picks up once AME
        # actually renders it. Falls back to the plain LIVE preset (with a
        # warning) if this one can't be found/selected, rather than
        # blocking the whole run.
        srt_out_dir = None
        srt_resolved_active = False
        if srt_active and "LIVE" in styles:
            srt_resolved, srt_preset_paths, srt_dropped = self._pp_resolve_style_presets(["LIVE WITH SRTs"])
            if srt_resolved:
                preset_paths["LIVE"] = srt_preset_paths["LIVE WITH SRTs"]
                srt_out_dir = self._pp_srt_out_dir.get().strip()
                srt_resolved_active = True
            else:
                self._pp_log("⚠ Skipping SRT — no \"LIVE WITH SRTs\" preset selected; "
                             "queueing plain LIVE instead.", "warn")

        if not resume:
            self._pp_exp_resume_idx = 0

        self._pp_exp_started = True
        self._pp_exporting_active = True
        # Locked for the whole run, including while paused — only
        # re-enabled once queueing is genuinely, fully done (see
        # _pp_export_task's _restore_after_export, the not-paused branch),
        # never mid-run or after a stop, so nobody can flip these while
        # items are actively being queued to AME.
        for chk in self._pp_style_checks.values():
            chk.set_enabled(False)
        self._pp_srt_check.set_enabled(False)
        self._pp_update_date_check.set_enabled(False)
        self._pp_build_exp_chips()
        self._pp_stop_export = False
        self._start_thinking(manage_reset_btn=False)
        # RESET ALL becomes STOP, same convention as nesting
        self._disable_reset_btn()
        self.btn_reset._text = "STOP"
        self.btn_reset._draw("#6e1a1a")
        self.btn_reset.unbind("<Enter>")
        self.btn_reset.unbind("<Leave>")
        self.btn_reset.unbind("<ButtonPress-1>")
        self.btn_reset.unbind("<ButtonRelease-1>")
        self._set_btn_state(self.btn_pp_export, False)
        self._set_btn_state(self.btn_pp_reset_exp, False)
        self._pp_exp_status.config(text="Queueing episodes to Adobe Media Encoder...", fg=TEXT_MUTED)

        def _enable_pp_stop():
            self.btn_reset._draw("#8e2a2a")
            self.btn_reset.unbind("<Enter>")
            self.btn_reset.unbind("<Leave>")
            self.btn_reset.unbind("<ButtonPress-1>")
            self.btn_reset.unbind("<ButtonRelease-1>")
            self.btn_reset.bind("<Enter>", lambda e: self.btn_reset._draw("#ae3a3a"))
            self.btn_reset.bind("<Leave>", lambda e: self.btn_reset._draw("#8e2a2a"))
            self.btn_reset.bind("<ButtonPress-1>", lambda e: self.btn_reset._draw("#6e1a1a"))
            def _on_pp_stop(e):
                self.btn_reset.unbind("<Enter>")
                self.btn_reset.unbind("<Leave>")
                self.btn_reset.unbind("<ButtonPress-1>")
                self.btn_reset.unbind("<ButtonRelease-1>")
                self.btn_reset._draw("#6e1a1a")
                self._pp_exp_status.config(text="Stopping...", fg=TEXT_MUTED)
                self._pp_stop_export = True
            self.btn_reset.bind("<ButtonRelease-1>", _on_pp_stop)
        self.after(0, _enable_pp_stop)

        import threading
        out_dirs = {
            "LIVE": self._pp_out_dir.get().strip(),
            "MARKETING": self._pp_marketing_out_dir.get().strip(),
            "TRAILER": self._pp_trailer_out_dir.get().strip(),
        }
        threading.Thread(target=self._pp_export_task,
                          args=(out_dirs, styles, preset_paths, srt_out_dir, srt_resolved_active),
                          daemon=True).start()

    def _pp_continue_export(self):
        self._pp_run_export(resume=True)

    def _pp_stop_export_now(self):
        self._pp_stop_export = True
        self._pp_exp_status.config(text="Stopping...", fg=TEXT_MUTED)

    def _pp_show_continue_export_button(self):
        self.btn_pp_export._text = "Continue Queueing"
        self.btn_pp_export._command = self._pp_continue_export
        self._set_btn_state(self.btn_pp_export, True)

    def _pp_apply_update_date(self, idx, name, sub, is_trailer):
        """Called once per queue entry, right before its first variant is
        queued, only when the Update Date checkbox is checked. Both
        episodes and the trailer always end in _YYMMDD (confirmed
        convention), so replacing that trailing date segment with today's
        date is safe for either — renames the Premiere sequence itself
        (sub.name), not just the output filename. Keeps the app's own
        bookkeeping (self._pp_created_seqs, self._pp_trailer_seq_name) in
        sync with the new name so later lookups by name don't go stale
        mid-run. Returns the (possibly unchanged) name to use from here on."""
        if not self._pp_update_date_var.get():
            return name
        today = datetime.now().strftime("%y%m%d")
        new_name = re.sub(r'_\d{6}$', f'_{today}', name)
        if new_name == name:
            return name
        sub.name = new_name
        self._pp_log(f"Renamed {name} → {new_name} (date refreshed).", "muted")
        self._pp_created_seqs[idx] = (new_name, sub, self._pp_created_seqs[idx][2])
        if is_trailer:
            self._pp_trailer_seq_name = new_name
        return new_name

    def _pp_export_task(self, out_dirs, styles, preset_paths, srt_out_dir=None, srt_active=False):
        import pymiere, os
        TICKS_PER_SECOND = 254016000000.0

        def _status(msg, color=TEXT_MUTED):
            self.after(0, lambda m=msg, c=color: self._pp_exp_status.config(text=m, fg=c))
            tag = "success" if color == "#50e050" else ("error" if color == TEXT_ERROR else ("warn" if color == TEXT_WARN else "muted"))
            self._pp_log(msg, tag)

        # Trailer identity tracked by position, not name — a name-based
        # comparison would break the moment Update Date renames the
        # trailer entry mid-run.
        trailer_idx = next((i for i, (nm, _, _) in enumerate(self._pp_created_seqs)
                             if nm == self._pp_trailer_seq_name), None)

        # Queued one STYLE PASS at a time across every item, rather than
        # one item at a time across every style — "everything LIVE" (and
        # the trailer) finishes queueing before "everything MARKETING"
        # starts, so it's obvious both here and in AME's queue which pass
        # is currently running. `passes` is the full ordered superset (as
        # if every item were the trailer); SOCIAL MEDIA's applicability is
        # filtered per-item below, not by omitting it from this list.
        passes = self._pp_build_export_variants(styles, is_trailer=True)

        def _applicable_passes(idx):
            return [p_i for p_i, (style, _, _) in enumerate(passes)
                    if style != "SOCIAL MEDIA" or idx == trailer_idx]

        # Resolve to chip-grid indices (not raw created_seqs indices) so
        # resume/progress numbers reflect what's actually being queued —
        # disabled entries, and items already queued in a prior run, are
        # skipped entirely, not just left uncounted. Stores (idx, sub) —
        # the stable identity — not name, since Update Date can rename an
        # item mid-run; name is always looked up fresh from
        # self._pp_created_seqs[idx] instead of trusted from this list.
        #
        # Items no checked style applies to at all (e.g. only SOCIAL MEDIA
        # is checked and this item isn't the trailer) are excluded here,
        # up front — left completely untouched (no chip recolor, not
        # marked as queued) instead of being force-marked "done" under a
        # style that never actually ran on them. Marking them queued was
        # also a correctness bug beyond the color: it made a later LIVE/
        # MARKETING run silently skip them, since _pp_exp_queued has no
        # concept of "queued under which style."
        included = [(i, sub) for i, (name, sub, _) in enumerate(self._pp_created_seqs)
                    if name not in self._pp_exp_disabled and name not in self._pp_exp_queued
                    and _applicable_passes(i)]
        total_items = len(included)
        work = []            # flat (pass_idx, item_pos) list, one per queue call
        remaining = {item_pos: len(_applicable_passes(idx))
                     for item_pos, (idx, sub) in enumerate(included)}
        for p_i, (style, _, _) in enumerate(passes):
            for item_pos, (idx, sub) in enumerate(included):
                if style == "SOCIAL MEDIA" and idx != trailer_idx:
                    continue
                work.append((p_i, item_pos))
        total = len(work)
        queued_items_this_run = 0
        style_label = " + ".join(styles)
        renamed_this_run = set()
        try:
            if total_items == 0:
                _status("✗ Nothing to queue — everything is disabled, already queued, "
                        "or not applicable to the checked style(s).", TEXT_ERROR)
                paused = False
                return

            encoder = pymiere.objects.app.encoder
            _status(f"Found {total_items} item(s) to queue ({style_label})", TEXT_MUTED)

            for pos in range(self._pp_exp_resume_idx, total):
                if self._pp_stop_export:
                    break
                p_i, item_pos = work[pos]
                style, suffix, variant_key = passes[p_i]
                idx, sub = included[item_pos]
                name = self._pp_created_seqs[idx][0]
                is_trailer = (idx == trailer_idx)
                self.after(0, lambda i=idx, st=style: self._pp_set_exp_chip_active(i, st))
                self.after(0, lambda st=style: self._pp_set_exp_progress_color(st))

                # Refreshes the date in the name (Premiere sequence rename
                # included) the first time this item is touched this run —
                # not once per pass, just once per item.
                if idx not in renamed_this_run:
                    name = self._pp_apply_update_date(idx, name, sub, is_trailer)
                    renamed_this_run.add(idx)

                self._pp_apply_variant_mute_state(sub, style, variant_key, is_trailer=is_trailer)
                out_dir = out_dirs["TRAILER"] if is_trailer else out_dirs[style]
                # Leads with the style/version (LIVE, MARKETING, SOCIAL
                # MEDIA) and the actual output filename — the previous
                # "{style}{suffix}" mashup (e.g. "MARKETING_M") read like
                # one garbled token instead of a version name next to a
                # separate filename. No extension shown here since AME
                # appends its own based on the preset's real output
                # format (see the out_path comment below) — this is
                # exactly what gets handed to encodeSequence().
                _status(f"Queueing {style} — {name}{suffix} — {pos+1}/{total} → {out_dir}",
                        TEXT_MUTED)
                # No extension here — every preset used so far has actually
                # produced .mp4, but AME appends its own extension based on
                # the preset's real output format regardless of what we
                # pass, so a hardcoded ".mp4" here just becomes literal text
                # baked into the filename for any preset that isn't
                # actually mp4 (e.g. an SRT-producing preset).
                out_path = os.path.join(out_dir, f"{name}{suffix}")
                if style == "MARKETING":
                    # In point is the beginning of the timeline; out point
                    # goes just before the poster/coming-soon still. Falls
                    # back to the full item if no such clip is found (e.g.
                    # the trailer may not have one). Always restored
                    # afterward, even on failure, so the trim can never
                    # leak into the next pass/item on this reused
                    # subsequence.
                    trim_ticks = self._pp_find_marketing_trim_point(sub)
                    if trim_ticks is not None:
                        full_out_ticks = int(sub.end)
                        try:
                            sub.setInPoint(0)
                            sub.setOutPoint(trim_ticks / TICKS_PER_SECOND)
                            encoder.encodeSequence(sub, out_path, preset_paths[style],
                                                    encoder.ENCODE_IN_TO_OUT, 0, False)
                        finally:
                            sub.setInPoint(0)
                            sub.setOutPoint(full_out_ticks / TICKS_PER_SECOND)
                    else:
                        encoder.encodeSequence(sub, out_path, preset_paths[style],
                                                encoder.ENCODE_ENTIRE, 0, False)
                else:
                    encoder.encodeSequence(sub, out_path, preset_paths[style],
                                            encoder.ENCODE_ENTIRE, 0, False)
                    if style == "LIVE" and srt_active and (is_trailer or srt_out_dir):
                        # This LIVE pass used "LIVE WITH SRTs" (see
                        # _pp_run_export) — register the expected .srt
                        # sidecar for the background watcher to pick up
                        # once AME actually renders it. AME names caption
                        # sidecars by taking the video's own output name
                        # and swapping in .srt. The trailer's SRT goes to
                        # the TRAILER output folder, alongside its video —
                        # not the general SRT folder every episode uses
                        # (which the trailer doesn't require being set).
                        srt_dest = out_dirs["TRAILER"] if is_trailer else srt_out_dir
                        self._pp_register_srt_watch_target(out_path, srt_dest)

                remaining[item_pos] -= 1
                self._pp_exp_resume_idx = pos + 1
                pct = self._pp_exp_resume_idx / total
                self.after(0, lambda p=pct: self._pp_exp_progress_var.set(p * 100))
                if remaining[item_pos] == 0:
                    queued_items_this_run += 1
                    self._pp_exp_queued.add(name)
                    self.after(0, lambda i=idx, st=style: self._pp_set_exp_chip_done(i, st))
                    # Names the style that JUST finished this item (not
                    # style_label, every checked style combined) — this
                    # fires once per item, right after whichever pass
                    # was its last remaining one, so saying "LIVE +
                    # MARKETING" here when only MARKETING just finished
                    # was misleading about what actually just happened.
                    _status(f"✓ Queued {name}{suffix} — {style} — "
                            f"{queued_items_this_run}/{total_items} item(s) fully queued", TEXT_MUTED)
                else:
                    # More styles still pending for this item — drop back
                    # to "waiting" (dimmed style color + lock) rather than
                    # staying lit in this pass's active color for the
                    # rest of the run.
                    self.after(0, lambda i=idx, st=style: self._pp_set_exp_chip_waiting(i, st))

            paused = self._pp_stop_export and self._pp_exp_resume_idx < total
            if paused:
                _status(f"⏸ Paused after {self._pp_exp_resume_idx}/{total} pass-item(s) — "
                        f"{queued_items_this_run} item(s) fully queued this run.", TEXT_WARN)
            else:
                self.after(0, lambda: self._pp_set_circle_done(self._pp_circ_p2_2))
                self._pp_exp_run_complete = True
                self.after(0, self._pp_refresh_exp_util_buttons)
                _status(f"✓ {queued_items_this_run}/{total_items} item(s) queued to AME — "
                        f"hit render in Media Encoder when ready.", "#50e050")
        except Exception as e:
            import traceback
            traceback.print_exc()
            paused = False
            _status(f"✗ Queue failed: {e}", TEXT_ERROR)
        finally:
            self._pp_exporting_active = False
            self.after(0, self._stop_thinking)
            self.after(0, self._pp_refresh_exp_util_buttons)

            def _restore_after_export():
                self._restore_reset_btn()
                if paused:
                    self._pp_show_continue_export_button()
                else:
                    # Back to a fresh "Queue Episodes" state (not the stale
                    # "Continue Queueing" resume command) so a later re-enable
                    # via _pp_refresh_export_button (more episodes nested)
                    # starts a clean run instead of resuming a finished one.
                    self._pp_exp_resume_idx = 0
                    self.btn_pp_export._text = "Queue Episodes"
                    self.btn_pp_export._command = self._pp_run_export
                    self._pp_refresh_export_button()
                    # Only re-enabled here — genuinely done (or the run
                    # errored out, also not-paused) — never while paused,
                    # matching each checkbox's own gating logic rather than
                    # blindly forcing them on (e.g. SOCIAL MEDIA/SRT stay
                    # off if their own preconditions aren't met).
                    self._pp_style_checks["LIVE"].set_enabled(True)
                    self._pp_style_checks["MARKETING"].set_enabled(True)
                    self._pp_refresh_social_media_enabled()
                    self._pp_refresh_srt_enabled()
                    self._pp_update_date_check.set_enabled(True)
                self._set_btn_state(self.btn_pp_reset_exp, True)
            self.after(0, _restore_after_export)

    def _pp_reset_export(self):
        """Resets the export run's progress and clears the export queue's
        permanent queued/disabled tracking — every chip goes back to its
        original ready (green) look and becomes toggleable again. Does not
        touch the AME connection itself — that's what RESET ALL is for;
        Connect to AME / Rescan Episodes keeps whatever state it was in."""
        self._pp_stop_export = False
        self._pp_exp_resume_idx = 0
        self._pp_exp_started = False
        self._pp_exp_run_complete = False
        self._pp_exp_queued = set()
        self._pp_exp_disabled = set()
        self._pp_exp_done_style = {}
        self._pp_exp_all_disabled = False
        self.btn_toggle_exp_all.config(text="DISABLE ALL", bg=BG_INPUT, fg=TEXT_PRIMARY)
        self._pp_set_circle_active(self._pp_circ_p2_2, "2")
        self.btn_pp_export._text = "Queue Episodes"
        self.btn_pp_export._command = self._pp_run_export
        self._pp_exp_progress_var.set(0)
        self._pp_reset_exp_progress_color()
        self._pp_exp_status.config(text="", fg=TEXT_MUTED)
        self._pp_build_exp_chips()
        self._pp_refresh_export_button()

    def _build_vfx_tab(self, main):
        # ── SHOW INFO ──────────────────────────────────────────────────────
        self._section_label(main, "SHOW INFO")
        show_frame = self._panel(main)

        self._mode_row(show_frame, self.show_mode, self._on_show_mode_change)

        prev_row = tk.Frame(show_frame, bg=BG_PANEL)
        prev_row.pack(fill="x", pady=4)
        tk.Label(prev_row, text="Filename Preview", font=FONT_LABEL, bg=BG_PANEL,
                 fg=TEXT_PRIMARY, width=22, anchor="w").pack(side="left")
        self.preview_label = tk.Label(prev_row, textvariable=self.filename_preview,
                                       font=FONT_SMALL, bg=BG_PANEL, fg=ACCENT, anchor="w")
        self.preview_label.pack(side="left", fill="x", expand=True)

        self.show_manual_frame = tk.Frame(show_frame, bg=BG_PANEL)
        self._field_row(self.show_manual_frame, "Show Code", self.show_code, "e.g. V-LA35")
        self._field_row(self.show_manual_frame, "Show Acronym", self.show_acronym, "e.g. HBFSHR")
        self._field_row(self.show_manual_frame, "Export Date (YYMMDD)", self.export_date, "e.g. 260617")

        tc_row = tk.Frame(show_frame, bg=BG_PANEL)
        tc_row.pack(fill="x", pady=4)
        tk.Label(tc_row, text="Ref Video Start TC", font=FONT_LABEL, bg=BG_PANEL,
                 fg=TEXT_PRIMARY, width=22, anchor="w").pack(side="left")
        self.hour_display = tk.Label(tc_row, textvariable=self.start_timecode_display,
                                      font=FONT_SMALL, bg=BG_PANEL, fg=TEXT_MUTED)
        self.hour_display.pack(side="left")
        self.hour_wrap = tk.Frame(tc_row, bg=BG_INPUT,
                             highlightthickness=1,
                             highlightbackground=BORDER, highlightcolor=ACCENT)
        tk.Frame(self.hour_wrap, bg=BG_INPUT, width=2).pack(side="left")
        self.hour_entry = tk.Entry(self.hour_wrap, textvariable=self.start_timecode,
                                    font=FONT_SMALL, bg=BG_INPUT, fg=TEXT_PRIMARY,
                                    insertbackground=TEXT_PRIMARY, relief="flat", bd=0,
                                    highlightthickness=0, width=10)
        self.hour_entry.pack(side="left", ipady=7)
        # hour_wrap shown in manual mode only; hour_display shown in auto mode (default)
        self.hour_hint = tk.Label(tc_row, text="hh:mm:ss:ff",
                                   font=FONT_SMALL, bg=BG_PANEL, fg="#666666")

        # ── REFERENCE VIDEO ────────────────────────────────────────────────
        self._section_label(main, "REFERENCE VIDEO")
        ref_frame = self._panel(main)
        self._mode_row(ref_frame, self.ref_mode, self._on_ref_mode_change)
        ref_row = tk.Frame(ref_frame, bg=BG_PANEL)
        ref_row.pack(fill="x", pady=4)
        ref_wrap = tk.Frame(ref_row, bg=BG_INPUT,
                            highlightthickness=1,
                            highlightbackground=BORDER, highlightcolor=ACCENT)
        ref_wrap.pack(side="left", fill="x", expand=True)
        tk.Frame(ref_wrap, bg=BG_INPUT, width=2).pack(side="left")
        self.ref_entry = tk.Entry(ref_wrap, textvariable=self.reference_video_path,
                 font=FONT_SMALL, bg=BG_INPUT, fg=TEXT_PRIMARY,
                 insertbackground=TEXT_PRIMARY, relief="flat", bd=0, highlightthickness=0)
        self.ref_entry.pack(side="left", fill="x", expand=True, ipady=7)
        self.ref_browse_btn = self._rounded_btn(ref_row, "Browse...", self._browse_reference, small=True, match_height=True)
        self.ref_browse_btn.pack(side="left", padx=(8, 0), fill="y")
        self.ref_hint = tk.Label(ref_frame, text="Auto-detected on connect",
                 font=FONT_SMALL, bg=BG_PANEL, fg=TEXT_MUTED)
        self.ref_hint.pack(anchor="w", pady=(4, 0))

        # ── OUTPUT FOLDER ──────────────────────────────────────────────────
        self._section_label(main, "OUTPUT FOLDER")
        out_frame = self._panel(main)
        self._mode_row(out_frame, self.out_mode, self._on_out_mode_change)
        out_row = tk.Frame(out_frame, bg=BG_PANEL)
        out_row.pack(fill="x", pady=4)
        out_wrap = tk.Frame(out_row, bg=BG_INPUT,
                            highlightthickness=1,
                            highlightbackground=BORDER, highlightcolor=ACCENT)
        out_wrap.pack(side="left", fill="x", expand=True)
        tk.Frame(out_wrap, bg=BG_INPUT, width=2).pack(side="left")
        self.out_entry = tk.Entry(out_wrap, textvariable=self.output_dir,
                 font=FONT_SMALL, bg=BG_INPUT, fg=TEXT_PRIMARY,
                 insertbackground=TEXT_PRIMARY, relief="flat", bd=0, highlightthickness=0)
        self.out_entry.pack(side="left", fill="x", expand=True, ipady=7)
        self.out_browse_btn = self._rounded_btn(out_row, "Browse...", self._browse_output, small=True, match_height=True)
        self.out_browse_btn.pack(side="left", padx=(8, 0), fill="y")
        self.out_hint = tk.Label(out_frame, text="Auto-detected on connect",
                 font=FONT_SMALL, bg=BG_PANEL, fg=TEXT_MUTED)
        self.out_hint.pack(anchor="w", pady=(4, 0))

        # Initialize widget states based on current mode (both default to auto = disabled)
        # These are called at end of _build_vfx_tab after all widgets exist

        # ── EXPORT BUTTONS (3 steps only) ──────────────────────────────────
        self._section_label(main, "EXPORT")
        btn_frame = tk.Frame(main, bg=BG_DARK)
        btn_frame.pack(fill="x", pady=(0, 12), padx=(4, 0))
        self.step_canvases = []

        # Only step 1 starts reachable — 2 and 3 start grey (_step_circle's
        # enabled=False look) and switch to the normal accent-outline
        # "ready" look via _set_step_ready once their prerequisite step
        # actually completes (see _do_connect/_do_scan), matching the
        # Episode Export tab's PHASE 1/2 circles instead of showing every
        # step as already reachable before Connect has even been clicked.
        self.step_canvases.append(self._step_circle(btn_frame, "1"))
        self.btn_connect = self._rounded_btn(btn_frame, "Connect to Resolve", self._do_connect)
        self.btn_connect.pack(side="left", padx=(0, 14))
        self.step_canvases.append(self._step_circle(btn_frame, "2", enabled=False))
        self.btn_scan = self._rounded_btn(btn_frame, "Scan Episodes", self._do_scan, enabled=False)
        self.btn_scan.pack(side="left", padx=(0, 14))
        self.step_canvases.append(self._step_circle(btn_frame, "3", enabled=False))
        self.btn_export = self._rounded_btn(btn_frame, "Export", self._do_export,
                                              enabled=False, accent=True,
                                              reserve_text="Continue")
        self.btn_export.pack(side="left", padx=(0, 8))
        # Reset Export sits right next to Export button, always visible but starts disabled
        self.btn_reset_export = self._rounded_btn(btn_frame, "Reset Export",
                                                   self._do_reset_export, enabled=False)
        self.btn_reset_export.pack(side="left", padx=(8, 0))
        # Starts disabled — enabled via _set_btn_state when export stops or completes

        # Create CSV checkbox — right of Reset Export, disabled until connected
        self._create_xlsx = tk.BooleanVar(value=True)
        self._xlsx_check = self._canvas_checkbox(btn_frame, self._create_xlsx, "Create .xlsx")
        self._xlsx_check.pack(side="left", padx=(16, 0))
        self._xlsx_check.set_enabled(False)

        # ── PROGRESS ───────────────────────────────────────────────────────
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(main, variable=self.progress_var,
                                             maximum=100, mode="determinate")
        prog_style = ttk.Style()
        prog_style.theme_use("clam")
        prog_style.configure("TProgressbar", troughcolor=BG_INPUT,
                        background=ACCENT, borderwidth=0, thickness=10)
        self.progress_bar = ttk.Progressbar(main, variable=self.progress_var,
                                             maximum=100, mode="determinate")
        self.progress_bar.pack(fill="x", pady=(16, 4))
        self.progress_status = tk.Label(main, text="", font=FONT_SMALL,
                                         bg=BG_DARK, fg=TEXT_SUCCESS)
        self.progress_status.pack(anchor="w", pady=(0, 4))

        # (action buttons are in btn_frame, hidden until needed)

        # ── DETECTED EPISODES (tag style) ──────────────────────────────────
        ep_header = tk.Frame(main, bg=BG_DARK)
        # pady=(12, 2) lands the label-to-box gap at 4px, matching
        # _section_label's gap (e.g. NUMBER OF PLATES DETECTED) — this row's
        # own height (taller than the label alone, since DISABLE ALL sits
        # beside it) already contributes 2px of that before any pady is added.
        ep_header.pack(fill="x", pady=(12, 2))
        tk.Label(ep_header, text="DETECTED EPISODES",
                 font=("SF Pro Display", 10, "bold"), bg=BG_DARK, fg=TEXT_MUTED).pack(side="left")
        btn_bg  = BG_INPUT
        btn_hov = "#3a3a3a"
        btn_prs = "#1a1a1a"
        self.btn_toggle_all = tk.Label(ep_header, text="DISABLE ALL",
                                        font=("SF Pro Display", 10, "bold"),
                                        bg="#2a2a2a", fg="#555555",
                                        padx=10, pady=3)
        self.btn_toggle_all.pack(side="right")
        self.btn_toggle_all.bind("<Enter>",           lambda e: self.btn_toggle_all.config(bg=btn_hov))
        self.btn_toggle_all.bind("<Leave>",           lambda e: self.btn_toggle_all.config(bg=btn_bg))
        self.btn_toggle_all.bind("<ButtonPress-1>",   lambda e: self.btn_toggle_all.config(bg=btn_prs))
        self.btn_toggle_all.bind("<ButtonRelease-1>", lambda e: (self.btn_toggle_all.config(bg=btn_hov), self._toggle_all_episodes()))
        self._all_disabled = False
        self.ep_outer = tk.Frame(main, bg="#252525", padx=12, pady=0)
        self.ep_outer.pack(fill="x", pady=(0, 8))
        self.ep_tags_frame = tk.Frame(self.ep_outer, bg="#252525")
        self.ep_tags_frame.pack(fill="x", pady=12)
        # Zero-sized permanent placeholder — forces ep_tags_frame to shrink to nothing
        # when all episode tags are removed (tkinter bug: frame stays at last child's size)
        tk.Frame(self.ep_tags_frame, bg="#252525", width=0, height=0).pack()
        self._ep_tag_widgets = []

        # Initialize all mode states properly
        self._on_show_mode_change()
        self._on_ref_mode_change()
        self._on_out_mode_change()

        # ── NUMBER OF PLATES ───────────────────────────────────────────────
        self._section_label(main, "NUMBER OF PLATES DETECTED")
        plates_frame = tk.Frame(main, bg="#252525", padx=16, pady=12)
        plates_frame.pack(fill="x", pady=(0, 8))
        self.plates_count_label = tk.Label(plates_frame, text="—",
                 font=("SF Pro Display", 14, "bold"), bg="#252525", fg=ACCENT,
                 anchor="w")
        self.plates_count_label.pack(anchor="w")

        # ── LOG (hidden by default) ─────────────────────────────────────────
        log_header = tk.Frame(main, bg=BG_DARK)
        log_header.pack(fill="x", pady=(28, 0))

        btn_bg   = BG_INPUT
        btn_hov  = "#3a3a3a"
        btn_prs  = "#1a1a1a"
        self.btn_log_toggle = tk.Label(log_header, text="SHOW LOG",
                                        font=("SF Pro Display", 10, "bold"),
                                        bg=btn_bg, fg=TEXT_PRIMARY,
                                        padx=10, pady=4)
        self.btn_log_toggle.pack(side="left")
        self.btn_log_toggle.bind("<Enter>",           lambda e: self.btn_log_toggle.config(bg=btn_hov))
        self.btn_log_toggle.bind("<Leave>",           lambda e: self.btn_log_toggle.config(bg=btn_bg))
        self.btn_log_toggle.bind("<ButtonPress-1>",   lambda e: self.btn_log_toggle.config(bg=btn_prs))
        self.btn_log_toggle.bind("<ButtonRelease-1>", lambda e: (self.btn_log_toggle.config(bg=btn_hov), self._toggle_log()))

        self.log_win = None
        self.log_box = None
        self._log_visible = False
        self._log_buffer = []  # (message, tag) pairs — persists across log window close/reopen, and across never having opened it at all

    def _do_continue_export(self):
        output = self.output_dir.get().strip()
        if not output:
            return
        # Use _last_done from engine - exact index of last successfully exported clip
        last = getattr(self.engine, '_last_done', 0)
        start_index = last  # _last_done = number of clips successfully completed
        if start_index >= len(self.export_list):
            self._log("All clips already exported.", "success")
            return
        self._log(f"Resuming from clip {start_index + 1}/{len(self.export_list)}.", "muted")
        self._set_btn_state(self.btn_reset_export, False)  # Disable Reset Export while running
        self.btn_export._text = "Export"
        self._set_btn_state(self.btn_export, False)
        self._set_stop_button()  # Ensure STOP shows immediately before thread starts
        self._run_export_task(output, start_index=start_index)

    def _enable_reset_export_btn(self):
        """Enable Reset Export button with green styling."""
        self.btn_reset_export._bg = "#2a6e2a"
        self.btn_reset_export._state["bg"] = "#2a6e2a"
        self.btn_reset_export._state["fg"] = "#FFFFFF"
        self.btn_reset_export._draw("#2a6e2a", "#FFFFFF")
        self.btn_reset_export.unbind("<Enter>")
        self.btn_reset_export.unbind("<Leave>")
        self.btn_reset_export.unbind("<ButtonPress-1>")
        self.btn_reset_export.unbind("<ButtonRelease-1>")
        self.btn_reset_export.bind("<Enter>", lambda e: self.btn_reset_export._draw("#3a8e3a", "#FFFFFF"))
        self.btn_reset_export.bind("<Leave>", lambda e: self.btn_reset_export._draw("#2a6e2a", "#FFFFFF"))
        self.btn_reset_export.bind("<ButtonPress-1>", lambda e: self.btn_reset_export._draw("#1a5e1a", "#FFFFFF"))
        self.btn_reset_export.bind("<ButtonRelease-1>", lambda e: (self._do_reset_export(), self.btn_reset_export._draw("#2a6e2a", "#FFFFFF")))

    def _do_reset_export(self):
        """Reset export progress but keep episodes. Restore full plate list."""
        self._set_btn_state(self.btn_reset_export, False)
        self._export_started = False
        self._vfx_run_complete = False
        self._all_disabled = False
        self.btn_toggle_all.config(text="DISABLE ALL", bg=BG_INPUT, fg=TEXT_PRIMARY)
        self._disabled_episodes.clear()
        self._xlsx_check.set_enabled(True)

        # Restore full export list from engine
        try:
            self._suppress_progress = True
            full_list = self.engine.prepare_export(
                self.show_code.get().strip() or "SHOW",
                self.show_acronym.get().strip() or "ACRN",
                self.export_date.get().strip() or datetime.now().strftime("%y%m%d")
            )
            self._suppress_progress = False
            self.export_list = full_list
            self.engine.export_list = full_list
        except Exception:
            self._suppress_progress = False

        self._update_episode_list()
        self._update_plate_count()
        self.btn_export._text = "Export"
        self._set_progress_normal()
        self.progress_var.set(0)
        self.progress_status.config(text="Click Export to Continue.")
        self._set_btn_state(self.btn_export, True)
        self._log("Export reset. Ready to export from the beginning.", "muted")
        if hasattr(self, 'step_canvases') and len(self.step_canvases) > 2:
            c = self.step_canvases[2]
            size = 24
            c.delete("all")
            c.create_oval(1, 1, size-1, size-1, outline=ACCENT, width=2, fill="")
            c.create_text(size//2, size//2, text="3",
                         font=("SF Pro Display", 11, "bold"), fill=ACCENT)

    def _show_continue_buttons(self, stopped_at):
        """Change Export button to Continue, enable Reset Export."""
        def _do():
            self._enable_reset_export_btn()
            self.btn_export._text = "Continue"
            self.btn_export._bg = ACCENT
            self.btn_export._fg = "#000000"
            self.btn_export._draw(ACCENT, "#000000")
            self.btn_export.unbind("<ButtonPress-1>")
            self.btn_export.unbind("<ButtonRelease-1>")
            self.btn_export.unbind("<Enter>")
            self.btn_export.unbind("<Leave>")
            def _on_press(e):
                self.btn_export._draw("#c07010", "#000000")
            def _on_release(e):
                self.btn_export._draw(ACCENT, "#000000")
                self._set_btn_state(self.btn_export, False)
                self._do_continue_export()
            self.btn_export.bind("<ButtonPress-1>", _on_press)
            self.btn_export.bind("<ButtonRelease-1>", _on_release)
            self.btn_export.bind("<Enter>", lambda e: self.btn_export._draw(ACCENT_HOVER, "#000000"))
            self.btn_export.bind("<Leave>", lambda e: self.btn_export._draw(ACCENT, "#000000"))
            # Show Reset Export - already always visible
        self.after(0, _do)

    def _set_stop_button(self):
        """Set RESET ALL button to STOP state for export."""
        self.btn_reset._text = "STOP"
        self.btn_reset._bg = "#8e2a2a"
        self.btn_reset._draw("#8e2a2a")
        # Clear all existing bindings first
        self.btn_reset.unbind("<Enter>")
        self.btn_reset.unbind("<Leave>")
        self.btn_reset.unbind("<ButtonPress-1>")
        self.btn_reset.unbind("<ButtonRelease-1>")
        self.btn_reset.bind("<Enter>", lambda e: self.btn_reset._draw("#ae3a3a"))
        self.btn_reset.bind("<Leave>", lambda e: self.btn_reset._draw(self.btn_reset._bg))
        self.btn_reset.bind("<ButtonPress-1>", lambda e: self.btn_reset._draw("#6e1a1a"))
        def _on_stop_release(e):
            # Disable immediately — re-enable when export actually stops
            self.btn_reset.unbind("<Enter>")
            self.btn_reset.unbind("<Leave>")
            self.btn_reset.unbind("<ButtonPress-1>")
            self.btn_reset.unbind("<ButtonRelease-1>")
            self.btn_reset._draw("#6e1a1a")
            self.progress_status.config(text="Stopping export...")
            self._stop_export_now()
        self.btn_reset.bind("<ButtonRelease-1>", _on_stop_release)

    def _run_export_task(self, output, start_index=0):
        """Run export in a background thread."""
        self._stop_export = False
        self._set_progress_normal()
        if start_index == 0:
            self.progress_var.set(0)
            self.progress_status.config(text="Preparing export...")
        else:
            self.progress_status.config(text="Resuming export...")
        self._set_btn_state(self.btn_export, False)
        # Disable STOP until export actually starts
        self._disable_reset_btn()
        self.btn_reset._text = "STOP"
        self.btn_reset._bg = "#8e2a2a"
        self.btn_reset._draw("#6e1a1a")
        self.btn_reset.unbind("<Enter>")
        self.btn_reset.unbind("<Leave>")
        self.btn_reset.unbind("<ButtonPress-1>")
        self.btn_reset.unbind("<ButtonRelease-1>")
        self._start_thinking(manage_reset_btn=False)
        self._export_started = True
        self.after(0, self._update_episode_list)
        self.after(0, lambda: self.btn_toggle_all.config(bg="#2a2a2a", fg="#555555"))
        self.after(0, lambda: self._xlsx_check.set_enabled(False))

        def task():
            try:
                # Always sync filtered list to engine before running
                self.engine.export_list = self.export_list

                # Now exporting for real — enable STOP button
                def _enable_stop_export():
                    self.progress_status.config(text="Starting render...")
                    self._set_stop_button()
                self.after(0, _enable_stop_export)

                # Setup per-clip screenshots if checkbox enabled
                _do_screenshots = self._create_xlsx.get()
                _shot_map = {}
                _list_folder = os.path.join(output, "LIST TO POST")
                _shots_folder = os.path.join(_list_folder, "SCREENSHOTS")
                _grab_screenshot = None

                # Pre-populate shot_map with any screenshots already saved to disk
                # (from a previous session before stop/continue)
                if os.path.exists(_shots_folder):
                    for fname in os.listdir(_shots_folder):
                        if fname.lower().endswith((".jpg", ".jpeg", ".png")):
                            clip_name = os.path.splitext(fname)[0]
                            # Match against export list filenames (without extension)
                            for item in self.export_list:
                                if os.path.splitext(item["filename"])[0] == clip_name:
                                    img_path = os.path.join(_shots_folder, fname)
                                    _shot_map[item["filename"]] = (img_path, 720, 1280)
                                    break

                if _do_screenshots:
                    import cv2 as _cv2
                    from PIL import Image as _PILImage
                    _ref = self.reference_video_path.get().strip() or None
                    _cap = _cv2.VideoCapture(_ref) if _ref and os.path.exists(str(_ref)) else None
                    if _cap and not _cap.isOpened():
                        _cap = None
                        self._log("  ⚠ Could not open ref video for screenshots.", "warn")
                    else:
                        os.makedirs(_shots_folder, exist_ok=True)
                        _start_tc = self.start_timecode.get().strip() or "03:59:50:00"
                        _fps = float(self.engine.timeline.GetSetting("timelineFrameRate") or 24)
                        _video_start_frames = timecode_str_to_frames(_start_tc, _fps)
                        # ref video opened silently — will log per clip during export

                        def _grab_screenshot(item):
                            try:
                                file_frame = int(item["start_frame"] - _video_start_frames)
                                if file_frame < 0:
                                    return
                                _cap.set(_cv2.CAP_PROP_POS_FRAMES, file_frame)
                                ret, frame = _cap.read()
                                if not ret or frame is None:
                                    self._log(f"  ⚠ Screenshot: could not read frame {file_frame}", "warn")
                                    return
                                rgb = _cv2.cvtColor(frame, _cv2.COLOR_BGR2RGB)
                                pil = _PILImage.fromarray(rgb)
                                # Save at 720x1280 (9:16 vertical 720p)
                                pil = pil.resize((720, 1280), _PILImage.LANCZOS)
                                shot_path = os.path.join(_shots_folder, item["filename"] + ".jpg")
                                pil.save(shot_path, "JPEG", quality=95)
                                _shot_map[item["filename"]] = (shot_path, 720, 1280)
                                self._log(f"  📷 Screenshot saved: {item['filename']} (frame {file_frame})", "muted")
                            except Exception as e:
                                self._log(f"  ⚠ Screenshot error: {e}", "warn")

                success = self.engine.run_export(output,
                    stop_flag=lambda: self._stop_export,
                    start_index=start_index,
                    screenshot_callback=_grab_screenshot)

                if _do_screenshots and _cap:
                    _cap.release()

                self.after(0, self._stop_thinking)
                def _to_reset():
                    self.btn_reset._text = "RESET ALL"
                    self.btn_reset._bg = "#2a6e2a"
                    self.btn_reset._draw("#2a6e2a")
                    self._enable_reset_btn()
                self.after(0, _to_reset)

                if self._stop_export:
                    done = self.engine._last_done if hasattr(self.engine, '_last_done') else start_index
                    total_plates = len(self.export_list)
                    last_item = self.export_list[done - 1] if done > 0 else {}
                    last_ep = last_item.get("episode_code", "?")
                    last_track = last_item.get("track_index", "?")
                    self._log(f"⚠ Stopped at plate {done}/{total_plates} · {last_ep} · Track {last_track}", "warn")
                    self.after(0, lambda d=done, t=total_plates, ep=last_ep, tr=last_track: self.progress_status.config(
                        text=f"Stopped at plate {d}/{t} · {ep} · Track {tr}"))
                    self.after(0, lambda: self._enable_reset_export_btn())
                    self._show_continue_buttons(done)
                elif success:
                    self._log("✓ Export complete.", "success")
                    self._log(f"  Output: {output}", "muted")
                    self._update_progress(100, None)
                    self._set_progress_green()
                    self._set_step_done(2)
                    self.after(0, lambda: self.progress_status.config(
                        text="All VFX plates have been exported."))
                    self.after(0, self._enable_reset_export_btn)
                    # Fully done (not stopped) — DISABLE ALL/ENABLE ALL now
                    # requires a fresh Scan Episodes before it unlocks again.
                    self._vfx_run_complete = True
                    self.after(0, self._refresh_toggle_all_btn)
                    if self._create_xlsx.get():
                        self.after(0, lambda: self._log("Building plate list xlsx...", "muted"))
                        self.after(0, lambda: self.progress_status.config(text="Building List to Post..."))
                        _exp_list = list(self.export_list)
                        _show = self.show_code.get().strip() or "SHOW"
                        _acr = self.show_acronym.get().strip() or "ACRN"
                        _date = self.export_date.get().strip() or datetime.now().strftime("%y%m%d")
                        _tl = self.engine.timeline
                        _lf = _list_folder
                        _sm = dict(_shot_map)
                        def _safe_log(msg, tag="muted"):
                            self.after(0, lambda m=msg, t=tag: self._log(m, t))
                        def _gen_xlsx():
                            try:
                                result = generate_plate_list_xlsx(
                                    export_list=_exp_list,
                                    output_dir=output,
                                    show_code=_show,
                                    acronym=_acr,
                                    date_str=_date,
                                    shot_map=_sm,
                                    list_folder=_lf,
                                    timeline=_tl,
                                    log_callback=_safe_log
                                )
                                _safe_log(f"✓ Plate list saved: {result}", "success")
                                self.after(0, lambda: self.progress_status.config(
                                    text="All VFX plates have been exported."))
                            except Exception as xe:
                                import traceback
                                _safe_log(f"✗ Plate list error: {xe}", "error")
                                _safe_log(traceback.format_exc(), "error")
                        threading.Thread(target=_gen_xlsx, daemon=True).start()
                else:
                    self._log("✗ Export failed — check log for details.", "error")
                    self.after(0, lambda: self.progress_status.config(
                        text="Export failed — check log for details."))
                    def _show_reset_only():
                        self._set_btn_state(self.btn_export, False)
                        self._enable_reset_export_btn()
                    self.after(0, _show_reset_only)
            except Exception as e:
                print(f"TASK EXCEPTION: {e}", flush=True)
                import traceback; traceback.print_exc()
                self.after(0, lambda err=str(e): self._log(f"✗ {err}", "error"))
                self.after(0, self._stop_thinking)
                self.after(0, lambda: self._set_btn_state(self.btn_export, True))
        threading.Thread(target=task, daemon=True).start()

    def _switch_tab(self, tab_id):
        self._active_tab = tab_id
        # RESET ALL's enabled look is per-tab (see self._reset_armed) — make
        # sure the shared button reflects the tab we just switched TO, not
        # whatever tab last called _enable_reset_btn/_disable_reset_btn.
        # Skipped entirely while a background operation (either tab —
        # e.g. Episode Export's reel prescan) currently owns the button as
        # STOP (_thinking_owns_reset_btn) — switching tabs mid-scan used
        # to silently flip it back to a plain enabled/disabled RESET ALL,
        # even though that operation was still genuinely running in the
        # background; _stop_thinking() is what's supposed to hand it back,
        # once the operation that's actually using it finishes.
        if not getattr(self, "_thinking_owns_reset_btn", False):
            if self._reset_armed.get(tab_id):
                self._enable_reset_btn()
            else:
                self._disable_reset_btn()
        # Same per-tab treatment for the SHOWCODE pill.
        if hasattr(self, '_show_pill'):
            self._render_show_pill(self._show_pill_text.get(tab_id, ""))
        if hasattr(self, '_draw_tab_fn'):
            self._draw_tab_fn()
        if tab_id == "vfx":
            self._test_content.pack_forget()
            self._vfx_content.pack(fill="both", expand=True)
        elif tab_id == "test":
            self._vfx_content.pack_forget()
            self._test_content.pack(fill="both", expand=True)
        self.update_idletasks()
        # Window is locked resizable(False, False) — geometry() silently
        # fails to grow it on macOS unless briefly unlocked first (every
        # other resize call in the app already does this; this one didn't).
        self.resizable(False, True)
        target_height = self.winfo_reqheight()
        self.geometry(f"{APP_WIDTH}x{target_height}")
        self.resizable(False, False)
        self.update_idletasks()
        # Same macOS quirk _pp_resize_window() already works around: a
        # single resizable(False,True) -> geometry() -> resizable(False,False)
        # pass doesn't reliably apply a LARGE jump in height, and VFX vs.
        # Episode Export is one of the biggest jumps in the app —
        # winfo_height() can end up well short of target_height right
        # after this call. An immediate nested retry doesn't help
        # (confirmed elsewhere); only a genuinely separate, later call
        # does, once Tk's event queue has drained.
        if (abs(self.winfo_height() - target_height) > 4
                and not getattr(self, "_switch_tab_resize_retry_pending", False)):
            self._switch_tab_resize_retry_pending = True

            def _switch_tab_resize_retry():
                self._switch_tab_resize_retry_pending = False
                self.update_idletasks()
                self.resizable(False, True)
                self.geometry(f"{APP_WIDTH}x{self.winfo_reqheight()}")
                self.resizable(False, False)
            self.after(50, _switch_tab_resize_retry)

    def _build_log_window(self, title):
        """Floating log window, shared by both tabs — keeps log height from
        competing with the growing episode chip grid for the main window's
        height (that competition was squishing the log shorter and shorter
        as more chips populated)."""
        win = tk.Toplevel(self)
        win.title(title)
        win.configure(bg=BG_DARK)
        win.geometry("560x360")
        body = tk.Frame(win, bg=BG_DARK, padx=8, pady=8)
        body.pack(fill="both", expand=True)
        log_p = self._panel(body)
        log_scroll = tk.Scrollbar(log_p, bg=BG_PANEL)
        log_scroll.pack(side="right", fill="y")
        log_box = tk.Text(log_p, font=FONT_MONO, bg=BG_INPUT, fg=TEXT_PRIMARY,
                           relief="flat", bd=0, width=1, highlightthickness=0,
                           yscrollcommand=log_scroll.set, state="disabled", wrap="word")
        log_box.pack(fill="both", expand=True, padx=8, pady=8)
        log_scroll.config(command=log_box.yview)
        log_box.tag_configure("success", foreground=TEXT_SUCCESS)
        log_box.tag_configure("error", foreground=TEXT_ERROR)
        log_box.tag_configure("warn", foreground=TEXT_WARN)
        log_box.tag_configure("muted", foreground=TEXT_MUTED)
        return win, log_box

    def _toggle_log(self):
        if self._log_visible and self.log_win is not None and self.log_win.winfo_exists():
            self.log_win.destroy()
            self.log_win = None
            self.log_box = None
            self.btn_log_toggle.config(text="SHOW LOG")
            self._log_visible = False
        else:
            self.log_win, self.log_box = self._build_log_window("Log — VFX Export")
            self.log_win.protocol("WM_DELETE_WINDOW", self._toggle_log)
            self.btn_log_toggle.config(text="HIDE LOG")
            self._log_visible = True
            if self._log_buffer:
                self.log_box.config(state="normal")
                for msg, tag in self._log_buffer:
                    self.log_box.insert("end", msg + "\n", tag)
                self.log_box.see("end")
                self.log_box.config(state="disabled")
            self.log_win.lift()

    def _set_step_done(self, step_index):
        def _do():
            if step_index < len(self.step_canvases):
                c = self.step_canvases[step_index]
                size = 24
                c.delete("all")
                c.create_oval(1, 1, size-1, size-1, outline=TEXT_SUCCESS, width=2, fill="")
                c.create_text(size//2, size//2, text="✓",
                             font=("SF Pro Display", 13, "bold"), fill=TEXT_SUCCESS)
        self.after(0, _do)

    def _set_step_ready(self, step_index):
        """Switches a step circle from its initial grey/pending look
        (_step_circle's enabled=False) to the normal accent-outline "ready
        to click" look, once the prior step actually completes — called
        from _do_connect (unlocks step 2) and _do_scan (unlocks step 3).
        Distinct from _set_step_active's filled-accent look, which means
        "this step's own action is currently running", not merely reachable."""
        def _do():
            if step_index < len(self.step_canvases):
                c = self.step_canvases[step_index]
                size = 24
                c.delete("all")
                c.create_oval(1, 1, size-1, size-1, outline=ACCENT, width=2, fill="")
                c.create_text(size//2, size//2, text=str(step_index + 1),
                             font=("SF Pro Display", 11, "bold"), fill=ACCENT)
        self.after(0, _do)

    def _set_step_active(self, step_index):
        def _do():
            if step_index < len(self.step_canvases):
                c = self.step_canvases[step_index]
                size = 24
                c.delete("all")
                c.create_oval(1, 1, size-1, size-1, outline=ACCENT, width=2, fill=ACCENT)
                c.create_text(size//2, size//2, text=str(step_index + 1),
                             font=("SF Pro Display", 12, "bold"), fill=BG_DARK)
        self.after(0, _do)

    def _set_progress_green(self):
        def _do():
            s = ttk.Style()
            s.configure("TProgressbar", background=TEXT_SUCCESS)
        self.after(0, _do)

    def _set_progress_normal(self):
        def _do():
            s = ttk.Style()
            s.configure("TProgressbar", background=ACCENT)
        self.after(0, _do)

    def _mode_row(self, parent, var, command):
        """Auto/Manual toggle, canvas-drawn rather than tk.Radiobutton — native
        Radiobuttons have a longstanding macOS/Tk rendering bug where fg color
        doesn't reliably paint until a focus event (CPython bug tracker issues
        #42541, #44243), and no amount of forced refresh timing fixed it
        reliably. Drawing it ourselves sidesteps the native widget entirely,
        same approach already used for the dropdown.

        Returns the list of dot canvases (unchanged, existing callers rely
        on this), each with a .set_locked(bool) method attached — sharing
        one locked flag for the whole row — for sections that need to be
        fully disabled (e.g. Title Card Track under Skip Nest), not just
        left always-interactive."""
        row = tk.Frame(parent, bg=BG_PANEL)
        row.pack(fill="x", pady=(0, 6))
        tk.Label(row, text="Input Mode", font=FONT_LABEL, bg=BG_PANEL,
                 fg=TEXT_PRIMARY, width=22, anchor="w").pack(side="left")
        size = 14
        dots = []
        labels = []
        state = {"locked": False}
        LOCK_COL = "#555555"

        def _redraw():
            val = var.get()
            for mode, dot in dots:
                dot.delete("all")
                selected = (val == mode)
                if state["locked"]:
                    col = LOCK_COL
                else:
                    col = ACCENT if selected else TEXT_MUTED
                dot.create_oval(2, 2, size - 2, size - 2, outline=col, width=2)
                if selected:
                    dot.create_oval(5, 5, size - 5, size - 5,
                                     fill=(LOCK_COL if state["locked"] else ACCENT), outline="")

        for mode, label in [("auto", "Auto"), ("manual", "Manual")]:
            item = tk.Frame(row, bg=BG_PANEL, cursor="pointinghand")
            item.pack(side="left", padx=(0, 20))
            dot = tk.Canvas(item, width=size, height=size, bg=BG_PANEL, highlightthickness=0)
            dot.pack(side="left", padx=(0, 6))
            lbl = tk.Label(item, text=label, font=FONT_SMALL, bg=BG_PANEL, fg=TEXT_PRIMARY)
            lbl.pack(side="left")
            dots.append((mode, dot))
            labels.append(lbl)

            def _on_click(e, mode=mode):
                if state["locked"]:
                    return
                var.set(mode)
                command()
            for w in (item, dot, lbl):
                w.bind("<ButtonRelease-1>", _on_click)

        def _set_locked(locked):
            state["locked"] = locked
            for lbl in labels:
                lbl.config(fg=LOCK_COL if locked else TEXT_PRIMARY)
            _redraw()

        _redraw()
        var.trace_add("write", lambda *_: _redraw())
        result = [d for _, d in dots]
        for dot in result:
            dot.set_locked = _set_locked
        return result

    def _set_widgets_enabled(self, widgets, enabled):
        for w in widgets:
            if isinstance(w, tk.Entry):
                w.config(state="normal" if enabled else "disabled",
                         fg=TEXT_PRIMARY if enabled else TEXT_MUTED,
                         bg=BG_INPUT if enabled else "#2a2a2a")
            elif hasattr(w, "_enabled"):
                self._set_btn_state(w, enabled)

    def _on_show_mode_change(self):
        if hasattr(self, "_enable_reset_btn"): self._enable_reset_btn()
        is_auto = self.show_mode.get() == "auto"
        if is_auto:
            self.show_manual_frame.pack_forget()
            self.hour_wrap.pack_forget()
            self.hour_hint.pack_forget()
            self.hour_display.pack(side="left")
        else:
            self.show_manual_frame.pack(fill="x")
            self.hour_display.pack_forget()
            self.hour_wrap.pack(side="left")
            self.hour_hint.pack(side="left", padx=(8, 0))
        self._update_filename_preview()
        # Window is locked resizable(False, False) — geometry() silently
        # fails to grow/shrink it on macOS unless briefly unlocked first.
        self.update_idletasks()
        self.resizable(False, True)
        self.geometry(f"{APP_WIDTH}x{self.winfo_reqheight()}")
        self.resizable(False, False)

    def _on_ref_video_change(self):
        """Enable/disable Scan Episodes based on whether ref video is set."""
        if not hasattr(self, 'btn_scan'):
            return
        # Connected = engine has a timeline
        connected = (hasattr(self, 'engine') and self.engine is not None
                     and getattr(self.engine, 'timeline', None) is not None)
        has_ref = bool(self.reference_video_path.get().strip())
        self._set_btn_state(self.btn_scan, connected and has_ref)

    def _on_output_dir_change(self):
        """Enable/disable Export based on whether output folder is set."""
        if not hasattr(self, 'btn_export'):
            return
        has_output = bool(self.output_dir.get().strip())
        # Only enable export if scan is done (export_list exists)
        has_scan = bool(getattr(self, 'export_list', None))
        if has_output and has_scan:
            self._set_btn_state(self.btn_export, True)
        elif not has_output and has_scan:
            self._set_btn_state(self.btn_export, False)

    def _on_ref_mode_change(self):
        if hasattr(self, "_enable_reset_btn"): self._enable_reset_btn()
        is_auto = self.ref_mode.get() == "auto"
        self._set_widgets_enabled([self.ref_entry, self.ref_browse_btn], not is_auto)
        self.ref_hint.config(text="Auto-detected on connect" if is_auto else "Browse to select your reference video")

    def _on_out_mode_change(self):
        if hasattr(self, "_enable_reset_btn"): self._enable_reset_btn()
        is_auto = self.out_mode.get() == "auto"
        self._set_widgets_enabled([self.out_entry, self.out_browse_btn], not is_auto)
        self.out_hint.config(text="Auto-detected on connect" if is_auto else "Browse to select your output folder")

    def _rounded_btn(self, parent, text, command, enabled=True, accent=False, small=False,
                      reserve_text=None, match_height=False, danger=False, min_width=None):
        """Button drawn on Canvas with smooth rounded corners. danger=True
        gives it the same red used for STOP, for destructive actions."""
        if accent and not enabled:
            bg, fg = "#5a4a1a", "#888888"
        elif accent:
            bg, fg = ACCENT, "#000000"
        elif danger and not enabled:
            bg, fg = "#333333", "#555555"
        elif danger:
            bg, fg = "#8e2a2a", "#FFFFFF"
        else:
            bg, fg = ("#444444" if enabled else "#333333"), ("#FFFFFF" if enabled else "#555555")

        font = ("SF Pro Display", 11) if small else ("SF Pro Display", 13)
        pad_x, pad_y = (12, 4) if small else (18, 7)
        r = 8
        if match_height:
            # Will be resized after packing via place or configure
            pad_y = 7  # match entry ipady=7

        # Size canvas using font metrics - no temporary widget needed
        size_text = reserve_text if reserve_text else text
        import tkinter.font as tkfont
        f = tkfont.Font(family="SF Pro Display", size=11 if small else 13)
        tw = f.measure(size_text)
        th = f.metrics("linespace")
        w = max(tw + pad_x * 2, min_width) if min_width else tw + pad_x * 2
        h = th + pad_y * 2

        c = tk.Canvas(parent, width=w, height=h, bg=parent.cget("bg"),
                      highlightthickness=0, cursor="" if enabled else "arrow")

        # Initialize _text before first draw
        c._text = text

        # Draw once with tags for fast itemconfig updates
        x1, y1, x2, y2 = 0, 0, w, h
        points = [
            x1+r, y1, x2-r, y1, x2, y1, x2, y1+r,
            x2, y2-r, x2, y2, x2-r, y2, x1+r, y2,
            x1, y2, x1, y2-r, x1, y1+r, x1, y1,
        ]
        _poly_id = c.create_polygon(points, fill=bg, outline="", smooth=True)
        _text_id = c.create_text(w//2, h//2, text=c._text, font=font, fill=fg)

        def _draw(fill, text_color):
            c.itemconfig(_poly_id, fill=fill)
            c.itemconfig(_text_id, fill=text_color, text=c._text)

        # Store mutable state
        state = {"bg": bg, "fg": fg}

        if enabled:
            hover = ACCENT_HOVER if accent else ("#ae3a3a" if danger else "#555555")
            press = "#c07010" if accent else ("#6e1a1a" if danger else "#666666")
            c.bind("<Enter>", lambda e: _draw(hover, state["fg"]))
            c.bind("<Leave>", lambda e: _draw(state["bg"], state["fg"]))
            c.bind("<ButtonPress-1>", lambda e: _draw(press, state["fg"]))
            def _on_btn_release(e, cmd=command, s=state):
                _draw(s["bg"], s["fg"])
                cmd()
            c.bind("<ButtonRelease-1>", _on_btn_release)

        c._bg = bg
        c._fg = fg
        c._accent = accent
        c._danger = danger
        c._enabled = enabled
        c._command = command
        c._state = state

        def _redraw(fill_color, text_color):
            state["bg"] = fill_color
            state["fg"] = text_color
            _draw(fill_color, text_color)

        c._draw = _redraw
        c._text = text
        c._font = font
        return c

    def _section_label(self, parent, text):
        tk.Label(parent, text=text, font=("SF Pro Display", 10, "bold"),
                 bg=BG_DARK, fg=TEXT_MUTED).pack(anchor="w", pady=(12, 4))

    def _panel(self, parent):
        f = tk.Frame(parent, bg=BG_PANEL, bd=0, highlightthickness=0)
        f.pack(fill="x", pady=(0, 4))
        inner = tk.Frame(f, bg=BG_PANEL, padx=12, pady=10)
        inner.pack(fill="both", expand=True)
        return inner

    def _field_row(self, parent, label, var, placeholder=""):
        row = tk.Frame(parent, bg=BG_PANEL)
        row.pack(fill="x", pady=4)
        tk.Label(row, text=label, font=FONT_LABEL, bg=BG_PANEL,
                 fg=TEXT_PRIMARY, width=22, anchor="w").pack(side="left")
        # Entry wrapper with left padding via bg-matched frame
        entry_wrap = tk.Frame(row, bg=BG_INPUT,
                              highlightthickness=1,
                              highlightbackground=BORDER,
                              highlightcolor=ACCENT)
        entry_wrap.pack(side="left", fill="x", expand=True)
        tk.Frame(entry_wrap, bg=BG_INPUT, width=2).pack(side="left")
        entry = tk.Entry(entry_wrap, font=FONT_SMALL,
                         bg=BG_INPUT, fg=TEXT_PRIMARY, insertbackground=TEXT_PRIMARY,
                         relief="flat", bd=0, highlightthickness=0)
        entry.pack(side="left", fill="x", expand=True, ipady=7)

        def sync_var_to_entry(*args):
            current = entry.get()
            val = var.get()
            if current != val:
                entry.delete(0, "end")
                if val:
                    entry.insert(0, val)
                    entry.config(fg=TEXT_PRIMARY)
                elif placeholder:
                    entry.insert(0, placeholder)
                    entry.config(fg=TEXT_MUTED)
        var.trace_add("write", sync_var_to_entry)

        if var.get():
            entry.insert(0, var.get())
        elif placeholder:
            entry.insert(0, placeholder)
            entry.config(fg=TEXT_MUTED)

        def on_in(e, ent=entry, ph=placeholder):
            if ent.get() == ph and ent.cget("fg") == TEXT_MUTED:
                ent.delete(0, "end")
                ent.config(fg=TEXT_PRIMARY)

        def on_out(e, ent=entry, ph=placeholder, v=var):
            val = ent.get().strip()
            if not val:
                if v.get():
                    ent.insert(0, v.get())
                    ent.config(fg=TEXT_PRIMARY)
                elif ph:
                    ent.insert(0, ph)
                    ent.config(fg=TEXT_MUTED)
            else:
                v.set(val)
                ent.config(fg=TEXT_PRIMARY)

        def on_key(e, ent=entry, v=var):
            v.set(ent.get())

        entry.bind("<FocusIn>", on_in)
        entry.bind("<FocusOut>", on_out)
        entry.bind("<KeyRelease>", on_key)

    def _update_filename_preview(self):
        code = self.show_code.get().strip()
        acronym = self.show_acronym.get().strip()
        date = self.export_date.get().strip()
        if code and acronym and date:
            self.filename_preview.set(f"{code}_{acronym}_VFX_EP##_{date}_##")
        else:
            self.filename_preview.set("Connect to DaVinci to auto-fill...")

    def _check_deps_on_start(self):
        missing = check_dependencies()
        if missing:
            self._log("Missing dependencies:", "warn")
            for d in missing:
                self._log(f"  • {d}", "warn")
        else:
            self._log("All dependencies OK. Ready to connect.", "success")

    def _stop_scan_now(self):
        self._stop_scan = True
        self._log("⚠ Stopping scan after current frame...", "warn")

    def _rebind_stop_handler(self, on_stop):
        """(Re)binds the shared RESET ALL/STOP button's release handler to
        the given on_stop — factored out of _start_thinking() so a
        chained/nested _start_thinking() call (thinking already active)
        can move the live handler onto ITS OWN on_stop instead of leaving
        it pinned to whichever task happened to start first. Only rebinds
        <ButtonRelease-1> — the hover/press color bindings are generic
        and already correct once the button is in its STOP look."""
        def _on_generic_stop(e, _cb=on_stop):
            self.btn_reset.unbind("<Enter>")
            self.btn_reset.unbind("<Leave>")
            self.btn_reset.unbind("<ButtonPress-1>")
            self.btn_reset.unbind("<ButtonRelease-1>")
            self.btn_reset._draw("#6e1a1a")
            (_cb or self._do_reset)()
        self.btn_reset.bind("<ButtonRelease-1>", _on_generic_stop)

    def _start_thinking(self, manage_reset_btn=True, on_stop=None):
        """Start GIF animation. Also takes RESET ALL over into a STOP
        button for the duration, for any task that doesn't already manage
        that transition itself — nest/export/VFX-scan wire their own
        graceful-stop-with-resume version (a dedicated stop flag checked
        mid-loop) right after their own _start_thinking() call, passing
        manage_reset_btn=False so this doesn't stomp on that.

        Every other "thinking" task here (Connect to Premiere's scan,
        Connect to DaVinci, Skip Nest's project scan/Connect to AME,
        manual track re-pick, arming a timeline, the reel prescan) passes
        its own on_stop callback instead — a lightweight, task-specific
        cancel-and-revert (set a cancel flag the background thread checks
        before applying its eventual result, and immediately restore
        whatever UI that task had already speculatively changed, e.g. the
        dropdown's "SCANNING" placeholder) instead of the old behavior of
        just calling _do_reset() and wiping the whole tab. Falls back to
        _do_reset() if a caller doesn't pass one — kept as the safe
        default rather than a silent no-op, but every call site in this
        file now passes its own.

        _thinking_active/_thinking_owns_reset_btn are shared across BOTH
        tabs (one physical spinner, one physical RESET ALL button) — a
        plain reentrancy guard used to mean a second, genuinely unrelated
        operation starting while the first is still in flight (e.g.
        clicking Connect to DaVinci on the VFX tab while the Episode
        Export tab's reel prescan is still running in the background) was
        a total no-op, and that second operation's own _stop_thinking()
        would then tear down the FIRST operation's spinner/STOP button
        the moment IT finished — even though the first was still working.
        _thinking_depth turns this into a simple reentrant counter instead:
        every _start_thinking() call counts, and the shared UI only
        actually resets once every matching _stop_thinking() has landed.
        RESET ALL/Reset Nest force this back to 0 as a safety valve, in
        case some call path somewhere doesn't perfectly pair up."""
        self._thinking_depth = getattr(self, "_thinking_depth", 0) + 1
        if self._thinking_active:
            # A nested/chained task started while another is still in
            # flight — e.g. Connect to Premiere's own scan handing off
            # straight to the eager reel prescan right after, on the
            # same tab. The spinner/STOP visuals are already up, so no
            # redraw needed, but the STOP button's bound handler still
            # needs to move onto THIS call's on_stop: whatever most
            # recently started is what's actually running right now,
            # and is what STOP should interrupt. Previously this just
            # returned, leaving STOP bound to the FIRST caller's
            # on_stop for the whole rest of the chain — clicking STOP
            # during the reel prescan (after Connect to Premiere's own
            # scan had already returned) silently did nothing useful,
            # since the bound handler was still Connect's own on_stop,
            # which has no idea how to abort a prescan.
            if self._thinking_owns_reset_btn and on_stop is not None:
                self._rebind_stop_handler(on_stop)
            return
        self._thinking_active = True
        self._thinking_frame_idx = 0
        self._thinking_owns_reset_btn = manage_reset_btn and hasattr(self, "btn_reset")
        if self._thinking_owns_reset_btn:
            self._disable_reset_btn()
            self.btn_reset._text = "STOP"
            self.btn_reset._bg = "#8e2a2a"
            self.btn_reset._draw("#8e2a2a")
            self.btn_reset.unbind("<Enter>")
            self.btn_reset.unbind("<Leave>")
            self.btn_reset.unbind("<ButtonPress-1>")
            self.btn_reset.unbind("<ButtonRelease-1>")
            self.btn_reset.bind("<Enter>", lambda e: self.btn_reset._draw("#ae3a3a"))
            self.btn_reset.bind("<Leave>", lambda e: self.btn_reset._draw("#8e2a2a"))
            self.btn_reset.bind("<ButtonPress-1>", lambda e: self.btn_reset._draw("#6e1a1a"))
            self._rebind_stop_handler(on_stop)
        if self._thinking_frames:
            self._animate_gif()
        else:
            # Fallback: simple text dots animation
            self._animate_text_dots(0)

    def _stop_thinking(self):
        """Stop GIF animation and hide. Restores RESET ALL if
        _start_thinking's generic path was the one that took it over (see
        there) — leaves it alone otherwise, since nest/export/VFX-scan
        restore it themselves as part of their own completion handling.

        Paired with _start_thinking's _thinking_depth counter — if some
        OTHER operation's _start_thinking() is still outstanding (e.g. the
        VFX tab's Connect finished while the Episode Export tab's reel
        prescan is still running), this decrements the shared counter but
        leaves the actual spinner/STOP button alone rather than tearing it
        down out from under that still-running operation."""
        self._thinking_depth = max(0, getattr(self, "_thinking_depth", 1) - 1)
        if self._thinking_depth > 0:
            return
        self._thinking_active = False
        if hasattr(self, '_thinking_label'):
            self.after(0, lambda: self._thinking_label.config(image="", text=""))
        if getattr(self, "_thinking_owns_reset_btn", False):
            self._thinking_owns_reset_btn = False
            self._restore_reset_btn()

    def _animate_gif(self):
        if not self._thinking_active or not self._thinking_frames:
            return
        frame = self._thinking_frames[self._thinking_frame_idx]
        self._thinking_label.config(image=frame)
        self._thinking_frame_idx = (self._thinking_frame_idx + 1) % len(self._thinking_frames)
        self.after(80, self._animate_gif)

    def _animate_text_dots(self, frame):
        if not self._thinking_active:
            self._thinking_label.config(text="", image="")
            return
        dots = ["●  ○  ○", "○  ●  ○", "○  ○  ●"]
        self._thinking_label.config(
            image="",
            text=dots[frame % 3],
            font=("SF Pro Display", 9),
            fg="#FFFFFF",
            bg=BG_OUTER
        )
        self.after(250, lambda f=frame: self._animate_text_dots(f + 1))

    def _check_for_updates(self):
        """Silently check GitHub for a newer version."""
        def task():
            try:
                import urllib.request, json, ssl
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                req = urllib.request.urlopen(VERSION_URL, context=ctx, timeout=10)
                data = json.loads(req.read().decode())
                remote = data.get("version", "0")
                notes  = data.get("release_notes", "")
                def _parse(v):
                    try: return tuple(int(x) for x in v.split("."))
                    except: return (0,)
                if _parse(remote) > _parse(APP_VERSION):
                    self.after(0, lambda: self._show_update_banner(remote, notes, data.get("download_url", DOWNLOAD_URL)))
            except Exception:
                pass
        threading.Thread(target=task, daemon=True).start()

    def _show_update_banner(self, remote_version, notes, download_url):
        """Show a dismissable update banner below the title."""
        BNR_BG  = "#1a3a1a"
        BNR_FG  = "#6fcf6f"
        BTN_BG  = "#2a6e2a"
        BTN_HOV = "#3a8e3a"
        BTN_PRS = "#1a5e1a"

        banner = tk.Frame(self._main_frame, bg=BNR_BG)
        banner.pack(fill="x", pady=(0, 8))

        def _make_banner_btn(parent, text, cmd):
            b = tk.Label(parent, text=text, font=("SF Pro Display", 11),
                         bg=BTN_BG, fg="#ffffff", padx=10, pady=4, cursor="")
            b.bind("<Enter>",           lambda e: b.config(bg=BTN_HOV))
            b.bind("<Leave>",           lambda e: b.config(bg=BTN_BG))
            b.bind("<ButtonPress-1>",   lambda e: b.config(bg=BTN_PRS))
            b.bind("<ButtonRelease-1>", lambda e: (b.config(bg=BTN_HOV), cmd()))
            return b

        # ✕ on the left
        _make_banner_btn(banner, "✕", banner.destroy).pack(side="left", padx=(8, 4), pady=4)

        # Message text
        tk.Label(banner, text=f"✦ Version {remote_version} available — {notes}",
                 font=("SF Pro Display", 11), bg=BNR_BG, fg=BNR_FG, padx=8, pady=6
                 ).pack(side="left")

        # UPDATE NOW on the right
        _make_banner_btn(banner, "UPDATE NOW", lambda: self._do_update(remote_version, download_url)
                         ).pack(side="right", padx=(4, 8), pady=4)

    def _do_update(self, remote_version, download_url):
        import urllib.request, ssl
        self._log(f"Downloading v{remote_version}...", "muted")
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            script_path = os.path.abspath(__file__)
            resources_dir = os.path.dirname(script_path)
            vpath = os.path.join(resources_dir, "version.json")

            # Download version.json
            vreq = urllib.request.urlopen(VERSION_URL, context=ctx, timeout=30)
            with open(vpath, "wb") as f:
                f.write(vreq.read())

            # Download main.py
            req = urllib.request.urlopen(download_url, context=ctx, timeout=30)
            with open(script_path, "wb") as f:
                f.write(req.read())
            self._log(f"✓ Updated to v{remote_version}. Restarting...", "success")
            # Find the .app bundle by walking up from script_path
            path = os.path.abspath(script_path)
            app_bundle = None
            for _ in range(6):
                path = os.path.dirname(path)
                if path.endswith(".app"):
                    app_bundle = path
                    break
            def _restart():
                if app_bundle and os.path.exists(app_bundle):
                    # Use the app's own launcher executable directly
                    # -n forces a new instance even if already running
                    import subprocess
                    app_name = os.path.splitext(os.path.basename(app_bundle))[0]
                    launcher = os.path.join(app_bundle, "Contents", "MacOS", app_name)
                    if os.path.exists(launcher):
                        subprocess.Popen([launcher])
                    else:
                        os.system(f'open -n "{app_bundle}"')
                else:
                    os.execv(sys.executable, [sys.executable, script_path])
                self.after(500, self.destroy)
            self.after(1500, _restart)
        except Exception as e:
            self._log(f"✗ Update failed: {e}", "error")

    def _update_show_pill(self, text="", tab=None):
        """Store the pill text for `tab` (default: the active tab) and only
        redraw the shared pill widget if that tab is currently active — VFX
        and Episode Export each own their own pill text instead of one
        connecting and overwriting whatever the other tab was showing."""
        tab = tab or self._active_tab
        self._show_pill_text[tab] = text
        if tab == self._active_tab:
            self._render_show_pill(text)

    def _render_show_pill(self, text):
        """Show or hide the project name pill in the title row."""
        # Remove old pill if exists
        if hasattr(self, '_show_pill_widget') and self._show_pill_widget:
            self._show_pill_widget.destroy()
            self._show_pill_widget = None
        if not text:
            return
        import tkinter.font as tkfont
        f = tkfont.Font(family="SF Pro Display", size=13, weight="normal")
        tw = f.measure(text)
        w = tw + 48
        h = 32
        r = 8
        c = tk.Canvas(self._show_pill, width=w, height=h,
                      bg=BG_OUTER, highlightthickness=0)
        pts = [r,0, w-r,0, w,0, w,r, w,h-r, w,h, w-r,h, r,h, 0,h, 0,h-r, 0,r, 0,0]
        c.create_polygon(pts, fill="#0e0e0e", outline="", smooth=True)
        c.create_text(w//2, h//2, text=text, font=("SF Pro Display", 13),
                      fill="#FFFFFF", anchor="center")
        c.pack()
        self._show_pill_widget = c

    def _setup_menu(self):
        """Add Help menu to Mac menu bar — persists regardless of which window is focused."""
        menubar = tk.Menu(self)
        # Apple menu required for macOS menu bar to show other items
        apple_menu = tk.Menu(menubar, name="apple", tearoff=0)
        menubar.add_cascade(menu=apple_menu)
        # Window menu — standard macOS convention
        window_menu = tk.Menu(menubar, name="window", tearoff=0)
        menubar.add_cascade(label="Window", menu=window_menu)
        # Help menu
        help_menu = tk.Menu(menubar, name="help", tearoff=0)
        menubar.add_cascade(label="Help", menu=help_menu)
        help_menu.add_command(label="Setup Guide", command=self._open_guide)
        self.config(menu=menubar)
        self.update_idletasks()

    def _pp_build_track_illustration(self, parent):
        """Small looping animation reinforcing the "select all tracks,
        including captions" reminder above it — a cursor clicks V3 then C1
        in sequence, each turning blue (targeted) to match the others."""
        BLUE, GREY, GREY_TXT, BLUE_TXT = "#2f7de1", "#3a3a3a", "#999999", "#eaf2ff"
        rows = [("C1", True), None, ("V4", True), ("V3", False), ("V2", True),
                ("V1", True), None, ("A1", True), ("A2", True)]
        badge_w, badge_h, row_h, line_w = 36, 19, 24, 100

        row_tops = {}
        y = 8
        for item in rows:
            if item is None:
                y += 8
                continue
            row_tops[item[0]] = y
            y += row_h
        canvas_w = 10 + badge_w + 10 + line_w + 14
        canvas_h = y + 6

        c = tk.Canvas(parent, width=canvas_w, height=canvas_h, bg="#111111", highlightthickness=0)
        c.pack(anchor="w", pady=(4, 12))

        clip_offsets = [4, 20, 0, 30, 10, 24, 0, 14]
        badges = {}
        oi = 0
        for item in rows:
            if item is None:
                continue
            label, targeted = item
            x0, y0 = 10, row_tops[label]
            x1, y1 = x0 + badge_w, y0 + badge_h
            fill = BLUE if targeted else GREY
            txt_fill = BLUE_TXT if targeted else GREY_TXT
            rect = c.create_rectangle(x0, y0, x1, y1, fill=fill, outline="")
            txt = c.create_text((x0 + x1) // 2, (y0 + y1) // 2, text=label,
                                 font=("SF Pro Display", 9, "bold"), fill=txt_fill)
            lx0, ly0 = x1 + 10, y0 + (badge_h - 9) // 2
            lx1, ly1 = lx0 + line_w, ly0 + 9
            c.create_rectangle(lx0, ly0, lx1, ly1, fill="#222222", outline="")
            cx = lx0 + clip_offsets[oi % len(clip_offsets)]
            oi += 1
            c.create_rectangle(cx, ly0, min(cx + 34, lx1), ly1, fill="#333333", outline="")
            badges[label] = (rect, txt, x1, y1)

        div_y1 = row_tops["V4"] - 5
        div_y2 = row_tops["A1"] - 5
        c.create_line(6, div_y1, canvas_w - 6, div_y1, fill="#262626")
        c.create_line(6, div_y2, canvas_w - 6, div_y2, fill="#262626")

        cursor_pts = [(1.6, 0.8), (1.6, 15.2), (5.0, 12.2), (7.2, 17.2), (9.6, 16.2), (7.4, 11.2), (12, 11.2)]
        cursor_poly = c.create_polygon([0, 0] * len(cursor_pts), fill="#ffffff",
                                        outline="#3a3a3a", width=1.4, state="hidden")

        def _set_cursor(x, y, visible):
            if not c.winfo_exists():
                return
            pts = []
            for lx, ly in cursor_pts:
                pts.extend([x + lx, y + ly])
            c.coords(cursor_poly, *pts)
            c.itemconfig(cursor_poly, state="normal" if visible else "hidden")

        def _set_badge(label, targeted):
            rect, txt, _, _ = badges[label]
            c.itemconfig(rect, fill=BLUE if targeted else GREY)
            c.itemconfig(txt, fill=BLUE_TXT if targeted else GREY_TXT)

        v3_x1, v3_y1 = badges["V3"][2], badges["V3"][3]
        c1_x1, c1_y1 = badges["C1"][2], badges["C1"][3]
        v3_target = (v3_x1 - 14, v3_y1 - 16)
        c1_target = (c1_x1 - 14, c1_y1 - 16)
        off_target = (canvas_w - 16, canvas_h - 6)

        def _lerp(a, b, t):
            return a + (b - a) * t

        def _frame(t):
            if t < 0.08:
                _set_cursor(*off_target, False)
                v3_on = c1_on = False
            elif t < 0.28:
                local = (t - 0.08) / 0.20
                x = _lerp(off_target[0], v3_target[0], local)
                y = _lerp(off_target[1], v3_target[1], local)
                _set_cursor(x, y, True)
                v3_on = c1_on = False
            elif t < 0.38:
                local = (t - 0.28) / 0.10
                bounce = 1 - abs(local - 0.5) * 2
                _set_cursor(v3_target[0] - bounce * 2, v3_target[1] + bounce * 2, True)
                v3_on, c1_on = local > 0.35, False
            elif t < 0.53:
                local = (t - 0.38) / 0.15
                x = _lerp(v3_target[0], c1_target[0], local)
                y = _lerp(v3_target[1], c1_target[1], local)
                _set_cursor(x, y, True)
                v3_on, c1_on = True, False
            elif t < 0.63:
                local = (t - 0.53) / 0.10
                bounce = 1 - abs(local - 0.5) * 2
                _set_cursor(c1_target[0] - bounce * 2, c1_target[1] + bounce * 2, True)
                v3_on, c1_on = True, local > 0.35
            elif t < 0.9:
                _set_cursor(c1_target[0], c1_target[1], True)
                v3_on = c1_on = True
            else:
                local = (t - 0.9) / 0.10
                _set_cursor(c1_target[0], c1_target[1], local < 0.5)
                v3_on = c1_on = True
            _set_badge("V3", v3_on)
            _set_badge("C1", c1_on)

        state = {"start": None}
        DURATION_MS = 6000

        def _tick():
            if not c.winfo_exists():
                return
            now = time.monotonic() * 1000
            if state["start"] is None:
                state["start"] = now
            elapsed = (now - state["start"]) % DURATION_MS
            _frame(elapsed / DURATION_MS)
            c.after(60, _tick)
        _tick()

    def _open_guide(self):
        """Open the Setup Guide floating window."""
        if hasattr(self, '_guide_win') and self._guide_win and self._guide_win.winfo_exists():
            self._guide_win.lift()
            self._guide_win.focus_force()
            return

        win = tk.Toplevel(self)
        win.title("Setup Guide")
        win.configure(bg="#161616")
        win.resizable(False, False)
        # Width = padx(32) + 4 tabs(130px) + 3 gaps(2px) + padx(32) + extra(16) = 606px
        WIN_W = 592
        self._guide_win = win

        BG_WIN    = "#161616"
        BG_PANEL  = "#1E1E1E"
        TAB_INACT = "#252525"
        TAB_BRDR  = "#3a3a3a"
        CODE_BG   = "#111111"

        # Header
        tk.Label(win, text="Setup Guide", font=("SF Pro Display", 18, "bold"),
                 bg=BG_WIN, fg=ACCENT).pack(pady=(24, 2))
        tk.Label(win, text="Finishing Tool", font=("SF Pro Display", 12),
                 bg=BG_WIN, fg=TEXT_MUTED).pack(pady=(0, 16))

        # Mode toggle — VFX Export / Episode Export each have their own tab
        # set; a 5th+ tab in one row would overflow WIN_W, so the tab row is
        # rebuilt per mode instead of growing wider.
        MODES = {
            "vfx":     {"label": "VFX EXPORT",
                        "tab_names": ["PREPARING CLIPS", "TIMELINE", "REF VIDEO", "OUTPUT FOLDER"],
                        "tab_ids":   ["clips", "timeline", "refvideo", "output"]},
            "episode": {"label": "EPISODE EXPORT",
                        "tab_names": ["BEFORE YOU START", "NESTING", "OUTPUT FOLDER", "EXPORTING"],
                        "tab_ids":   ["ep_before", "ep_nesting", "ep_output", "ep_ame"]},
        }
        self._guide_mode = tk.StringVar(value="vfx")

        mode_row = tk.Frame(win, bg=BG_WIN)
        mode_row.pack(fill="x", padx=32, pady=(0, 12))
        SEG_W = (WIN_W - 64 - 4) // 2

        def _draw_mode_seg(canvas, label, active):
            w, h, r = SEG_W, 30, 15
            col = ACCENT if active else "#888888"
            bg  = BG_DARK if active else "#252525"
            lw  = 1.5 if active else 1
            canvas.delete("all")
            canvas.config(bg=BG_WIN)
            canvas.create_rectangle(r, 0, w-r, h, fill=bg, outline="")
            canvas.create_arc(0, 0, r*2, h, start=90, extent=180, fill=bg, outline="")
            canvas.create_arc(w-r*2, 0, w, h, start=270, extent=180, fill=bg, outline="")
            if active:
                canvas.create_arc(1, 1, r*2-1, h-1, start=90, extent=180, style="arc", outline=col, width=lw)
                canvas.create_arc(w-r*2+1, 1, w-1, h-1, start=270, extent=180, style="arc", outline=col, width=lw)
                canvas.create_line(r, 1, w-r, 1, fill=col, width=lw)
                canvas.create_line(r, h-1, w-r, h-1, fill=col, width=lw)
            fw = "bold" if active else "normal"
            canvas.create_text(w//2, h//2, text=label,
                               font=("SF Pro Display", 11, fw), fill=col)

        mode_canvases = {}
        for i, mid in enumerate(MODES):
            c = tk.Canvas(mode_row, width=SEG_W, height=30, bg=BG_WIN, highlightthickness=0)
            c.pack(side="left", padx=(0, 4) if i == 0 else (0, 0))
            c.bind("<Button-1>", lambda e, m=mid: _set_mode(m))
            mode_canvases[mid] = c

        # Tab bar
        tab_row = tk.Frame(win, bg=BG_WIN)
        tab_row.pack(fill="x", padx=32)

        self._guide_tabs = {}
        self._guide_active_tab = tk.StringVar(value="clips")

        # Content area (built before tabs so tabs can reference it)
        content_frame = tk.Frame(win, bg=BG_DARK, bd=0, highlightthickness=1,
                                  highlightbackground="#5a5a5a", highlightcolor="#5a5a5a")
        content_frame.pack(fill="x", padx=(32, 34), pady=(0, 24))

        # Section frames directly in content_frame, one per tab across BOTH
        # modes (ids are unique) — same pattern as main app tabs
        sections = {}
        for mdef in MODES.values():
            for tid in mdef["tab_ids"]:
                f = tk.Frame(content_frame, bg=BG_DARK, padx=20, pady=20)
                sections[tid] = f

        win.protocol("WM_DELETE_WINDOW", win.destroy)

        guide_tab_canvases = {}

        def _draw_guide_tab(canvas, label, active):
            tw, th, r = 130, 36, 10
            col = ACCENT if active else "#888888"
            bg  = BG_DARK if active else "#252525"
            lw  = 1.5 if active else 1
            canvas.delete("all")
            canvas.config(bg=BG_WIN)
            canvas.create_rectangle(0, r, tw, th+1, fill=bg, outline="")
            canvas.create_rectangle(r, 0, tw-r, r, fill=bg, outline="")
            canvas.create_arc(0, 0, r*2, r*2, start=90, extent=90, fill=bg, outline="")
            canvas.create_arc(tw-r*2, 0, tw, r*2, start=0, extent=90, fill=bg, outline="")
            if active:
                canvas.create_line(r, 1, tw-r, 1, fill=col, width=lw)
                canvas.create_arc(1, 1, r*2+1, r*2+1, start=90, extent=90,
                                  outline=col, style="arc", width=lw)
                canvas.create_arc(tw-r*2-1, 1, tw-1, r*2+1, start=0, extent=90,
                                  outline=col, style="arc", width=lw)
                canvas.create_line(1, r, 1, th+1, fill=col, width=lw)
                canvas.create_line(tw-1, r, tw-1, th+1, fill=col, width=lw)
            fw = "bold" if active else "normal"
            canvas.create_text(tw//2, th//2, text=label,
                               font=("SF Pro Display", 11, fw), fill=col)

        def _show_tab(tid):
            mdef = MODES[self._guide_mode.get()]
            for t, f in sections.items():
                f.pack_forget()
            sections[tid].pack(fill="both", expand=True, padx=20, pady=20)
            for t, c in guide_tab_canvases.items():
                lbl = mdef["tab_names"][mdef["tab_ids"].index(t)]
                _draw_guide_tab(c, lbl, t == tid)
            self._guide_active_tab.set(tid)
            # Update height only, keep width fixed
            win.update_idletasks()
            h = win.winfo_reqheight()
            win.geometry(f"{WIN_W}x{h}")

        def _build_tab_row(mode):
            mdef = MODES[mode]
            for c in guide_tab_canvases.values():
                c.destroy()
            guide_tab_canvases.clear()
            self._guide_tabs.clear()
            for i, (tid, tname) in enumerate(zip(mdef["tab_ids"], mdef["tab_names"])):
                tw, th = 130, 36
                c = tk.Canvas(tab_row, width=tw, height=th+1,
                              bg=BG_WIN, highlightthickness=0)
                c.pack(side="left", padx=(0, 2))
                _draw_guide_tab(c, tname, i == 0)
                c.bind("<Button-1>", lambda e, t=tid: _show_tab(t))
                guide_tab_canvases[tid] = c
                self._guide_tabs[tid] = c

        def _set_mode(mode):
            self._guide_mode.set(mode)
            for mid, c in mode_canvases.items():
                _draw_mode_seg(c, MODES[mid]["label"], mid == mode)
            _build_tab_row(mode)
            _show_tab(MODES[mode]["tab_ids"][0])

        for mid, c in mode_canvases.items():
            _draw_mode_seg(c, MODES[mid]["label"], mid == "vfx")
        _build_tab_row("vfx")
        self._guide_set_mode = _set_mode  # exposed for testing / external use

        def _section_hdr(parent, text):
            tk.Label(parent, text=text, font=("SF Pro Display", 9, "bold"),
                     bg=BG_DARK, fg="#888888").pack(anchor="w", pady=(16, 4))
            tk.Frame(parent, bg="#2a2a2a", height=1).pack(fill="x", pady=(0, 12))

        def _step(parent, num, title, body, wrap=420):
            row = tk.Frame(parent, bg=BG_DARK)
            row.pack(fill="x", pady=(0, 12))
            c = tk.Canvas(row, width=24, height=24, bg=BG_DARK, highlightthickness=0)
            c.create_oval(1, 1, 23, 23, outline=ACCENT, width=2, fill="")
            c.create_text(12, 12, text=str(num), font=("SF Pro Display", 10, "bold"), fill=ACCENT)
            c.pack(side="left", anchor="n", pady=2)
            txt = tk.Frame(row, bg=BG_DARK)
            txt.pack(side="left", padx=(10, 0), fill="x", expand=True)
            tk.Label(txt, text=title, font=("SF Pro Display", 12, "bold"),
                     bg=BG_DARK, fg="#ffffff", anchor="w").pack(anchor="w")
            tk.Label(txt, text=body, font=("SF Pro Display", 11),
                     bg=BG_DARK, fg="#888888", anchor="w", wraplength=wrap, justify="left"
                     ).pack(anchor="w", pady=(2, 0))

        def _tip(parent, text):
            f = tk.Frame(parent, bg=BG_DARK)
            f.pack(fill="x", pady=(4, 12))
            tk.Frame(f, bg=ACCENT, width=3).pack(side="left", fill="y")
            tk.Label(f, text=text, font=("SF Pro Display", 10), bg=BG_DARK,
                     fg="#777777", wraplength=420, justify="left",
                     padx=10, pady=8).pack(side="left", anchor="w")

        def _code(parent, text, color=None):
            tk.Label(parent, text=text, font=("SF Mono", 10),
                     bg=CODE_BG, fg=color or ACCENT,
                     anchor="w", padx=10, pady=6, wraplength=440, justify="left"
                     ).pack(fill="x", pady=(2, 6))

        # ── PREPARING CLIPS ──────────────────────────────────────────────────
        sec = sections["clips"]
        _section_hdr(sec, "CLIP COLORS")
        _step(sec, 1, "Orange → VFX plates",
              "Any clip with the Orange clip color will be exported as a VFX plate. "
              "If clips are stacked on higher tracks and also orange, each layer gets a suffix: _V1, _V2, _V3, and so on.")
        _step(sec, 2, "Apricot → Clean plates",
              "Clips with the Apricot clip color are exported with a _CLEAN suffix — "
              "use these for shots that need a clean version without VFX elements.")
        _step(sec, 3, "Chocolate → Reused plates",
              "Clips with the Chocolate clip color are reused plates and are ignored "
              "during export — they won't be included in the turnover.")
        _tip(sec, "Tip: Right-click any clip → Clip Color → choose the color. "
                  "You can select multiple clips and apply a clip color to all of them at once.")
        _section_hdr(sec, "CLIP EFFECTS")
        _step(sec, 1, "Speed effects & compound clips",
              "The export preset automatically removes speed effects during export. "
              "To preserve a speed effect, wrap the clip in a compound clip first — "
              "this bakes it in so it exports correctly. Other effects like keyframes "
              "are retained unless removed manually.")
        _step(sec, 2, "Transitions & handles",
              "The export preset automatically includes handles for transitions — "
              "as long as the transition effect is applied to the clip. No extra steps needed.")
        _step(sec, 3, "Color correction",
              "Make sure your color grade is applied and finalized before exporting. "
              "Whatever grade is on the clip is what VFX will receive.")
        _tip(sec, "Tip: After scanning, you can toggle individual episodes on or off "
                  "before exporting — so you don't have to export everything at once.")

        # ── TIMELINE ─────────────────────────────────────────────────────────
        sec = sections["timeline"]
        _section_hdr(sec, "NAMING CONVENTION")
        _step(sec, 1, "Recommended timeline format",
              "Naming your DaVinci timeline using this format helps the app auto-detect "
              "your show info — but you can always switch to Manual mode if needed:")
        _code(sec, "SHOWCODE_ACRONYM_STRINGOUT_VFX_EP##-##_YYMMDD_COLOR")
        tk.Label(sec, text="Example timeline name:", font=("SF Pro Display", 10),
                 bg=BG_DARK, fg="#555").pack(anchor="w", pady=(4, 0))
        _code(sec, "V-LA30_TKWNM_STRINGOUT_VFX_EP01-57_260619_COLOR")
        tk.Label(sec, text="Resulting VFX plate filename:", font=("SF Pro Display", 10),
                 bg=BG_DARK, fg="#555").pack(anchor="w", pady=(4, 0))
        _code(sec, "V-LA30_TKWNM_VFX_EP01_260619_01", color="#6fcf6f")
        _step(sec, 2, "Manual mode",
              "If your timeline doesn't follow this format, switch to Manual mode "
              "in the Show Info section and enter your show code, acronym, and date directly.")
        _tip(sec, "Tip: The timeline name is only used for auto-detection. Exported filenames "
                  "are always generated from the show code and acronym.")

        # ── REF VIDEO ────────────────────────────────────────────────────────
        sec = sections["refvideo"]
        _section_hdr(sec, "SETUP")
        _step(sec, 1, "Import into DaVinci's Media Pool",
              "The reference video must be imported into DaVinci Resolve's Media Pool "
              "for the app to detect it. Without it in the Media Pool, auto-detection will not work.")
        _step(sec, 2, "Name your reference video",
              "Following the same naming convention helps the app find it automatically — "
              "but you can always browse to it manually:")
        _code(sec, "SHOWCODE_ACRONYM_VFX_EP##_YYMMDD_##.mov")
        _step(sec, 3, "Place it in the right folder",
              "Put the reference video inside the TO VFX folder. "
              "The app will scan for it automatically on connect.")
        _step(sec, 4, "Start timecode",
              "The app reads the start timecode automatically. "
              "If it can't detect it, enter it manually in the Show Info section.")
        _tip(sec, "Tip: Screenshots for the List to Post are taken from this reference video "
                  "— make sure it matches the timeline frame-for-frame.")

        # ── OUTPUT FOLDER ─────────────────────────────────────────────────────
        sec = sections["output"]
        _section_hdr(sec, "FOLDER STRUCTURE")
        _step(sec, 1, "Volume & folder match",
              "The app scans any mounted volume whose name contains your "
              "show code, then looks inside it (up to 6 folders deep) for "
              "the first folder with 'TO VFX' in its name:")
        _code(sec, "/Volumes/*SHOWCODE*/\n\u2192 ... (any depth, up to 6)\n\u2192 folder with 'TO VFX' in name")
        _step(sec, 2, "What gets created",
              "Inside TO VFX, the app creates one subfolder per episode plus a "
              "LIST TO POST folder containing the .xlsx document and screenshots.")
        _tip(sec, "Tip: If the drive isn't auto-detected, switch to Manual mode "
                  "and browse to your TO VFX folder directly.")

        # ── EPISODE EXPORT: BEFORE YOU START ────────────────────────────────
        sec = sections["ep_before"]
        _section_hdr(sec, "NAMING CONVENTIONS")
        _step(sec, 1, "STRINGOUT → Reel",
              "A timeline is picked up as a reel if its name contains "
              "STRINGOUT. Naming it in this format also lets the app "
              "auto-detect the show code, acronym, and date in Show Info:")
        _code(sec, "SHOWCODE_ACRONYM_STRINGOUT_EP##-##_DATE")
        _tip(sec, "The app also skips names containing AAF, REF, VFX, SUB, "
                  "NESTED, or TRAILER, so old exports and backup copies are "
                  "not picked up by mistake.")
        _step(sec, 2, "EPISODE / Graphic → Title card track",
              "The title card track is identified by clip names. The app "
              "looks for clips whose names start with EPISODE, or for "
              "Motion Graphics Template (MOGRT) clips, which Premiere "
              "reports simply as Graphic. Keep these clips on one dedicated "
              "track, V8 or higher:")
        _code(sec, "EPISODE ##")
        _step(sec, 3, "Reference track",
              "The app scans for the reference track by looking for a clip "
              "whose name contains your show code and either REF or PIC LOCK:")
        _code(sec, "SHOWCODE_ACRONYM_PIC LOCK_EP##-##_DATE_REF.mov")
        _tip(sec, "If \"Mute Master Clips\" is turned on, nesting mutes this "
                  "track and everything below it.")

        _section_hdr(sec, "BEFORE NESTING")
        cols = tk.Frame(sec, bg=BG_DARK)
        cols.pack(fill="x")
        left = tk.Frame(cols, bg=BG_DARK)
        left.pack(side="left", anchor="n", padx=(0, 16))
        self._pp_build_track_illustration(left)
        right = tk.Frame(cols, bg=BG_DARK)
        right.pack(side="left", anchor="n", fill="x", expand=True)
        _step(right, 4, "Select every track, including captions",
              "Before clicking \"Nest Episodes\", click each track header in "
              "Premiere's timeline panel so every track — including caption "
              "tracks — is highlighted blue. The app only includes "
              "highlighted tracks when building each nested episode; muting "
              "or hiding a track does not exclude it, only leaving its "
              "header unhighlighted does.", wrap=200)
        _tip(sec, "If you plan to export the \"MARKETING\" or \"SOCIAL MEDIA\" "
                  "deliverables, set up a lower-third track for the "
                  "on-screen name cards (these are Motion Graphics Template "
                  "clips, which Premiere names Graphic automatically) and a "
                  "Music Stem clip with mx somewhere in its filename, both "
                  "on the reel timeline before nesting. For a Social Media "
                  "export of the trailer, remember to add the Watermark.")

        # ── EPISODE EXPORT: NESTING ─────────────────────────────────────────
        sec = sections["ep_nesting"]
        _section_hdr(sec, "HOW NESTING WORKS")
        _step(sec, 1, "Reel dropdown",
              "The dropdown lists every STRINGOUT timeline found on "
              "connect, in story order. Select one reel to nest, or check "
              "\"Nest All\" to process every STRINGOUT timeline back-to-back.")
        _step(sec, 2, "Nest All → Nest Remaining",
              "If some reels have already been nested, the \"Nest All\" "
              "button relabels itself to \"Nest Remaining\" and continues "
              "from where you left off instead of starting over.")
        _step(sec, 3, "One title card → One episode",
              "Each title card clip becomes its own subsequence, trimmed "
              "from card to card (or to the reel's tails at the first and "
              "last episode), and moved into the project's DELIVERY > "
              "FINAL bin.")
        _step(sec, 4, "Mute Master Clips",
              "This checkbox sits next to \"Nest All\" and is off by "
              "default. When turned on, the app mutes the reference track "
              "and every video track below it, once per reel — since "
              "nesting creates each episode as a subsequence of that "
              "timeline, the mute carries into every episode automatically. "
              "Tracks above the title card track are never affected.")
        _step(sec, 5, "Re-nesting an existing episode",
              "If the episode number you're about to nest already exists "
              "in the project's DELIVERY > FINAL bin, the app asks whether "
              "to overwrite it. \"Yes\" replaces it with the newly nested "
              "one; \"No\" leaves it untouched and marks its tag done "
              "instead. \"Cancel\" stops the whole nest run, not just this "
              "episode. Under \"Nest All\", check \"Apply to all reels\" "
              "before answering to reuse that choice for every later "
              "collision in this run instead of being asked each time.")
        _tip(sec, "Use \"Reset Nest\" to start the current reel over from "
                  "scratch. If you already nested outside of this session, "
                  "use \"Skip Nest\" instead — \"Connect to AME\" will scan "
                  "the project directly and pick up every already-nested "
                  "FINAL timeline, including the trailer.")

        # ── EPISODE EXPORT: OUTPUT FOLDER ───────────────────────────────────
        sec = sections["ep_output"]
        _section_hdr(sec, "FOLDER STRUCTURE")
        _step(sec, 1, "Auto-detected path",
              "After nesting, the app scans mounted volumes for this structure:")
        _code(sec, "/Volumes/SHOWCODE_*/SHOWCODE_*_EDIT/\n→ folder with 'DELIVERY' in name\n→ folder with 'FINAL' in name\n→ folder with 'LIVE' in name")
        _step(sec, 2, "Manual mode",
              "If it isn't found automatically, switch to Manual and browse "
              "to the folder where your queued .mp4 exports should land.")
        _step(sec, 3, "MARKETING / TRAILER / SRT folders",
              "Checking MARKETING or SOCIAL MEDIA, or the SRT checkbox, needs "
              "its own destination too — auto-detected as a sibling of the "
              "LIVE folder under DELIVERY (a folder with 'MARKETING' or "
              "'TRAILER' in the name), or browsed to manually. The trailer's "
              "SRT rides along in the TRAILER folder, not the general SRT one.")
        _tip(sec, "Tip: This is also where Queue Episodes sends its AME output "
                  "files — one .mp4 per nested episode, named to match.")

        # ── EPISODE EXPORT: AME EXPORT ───────────────────────────────────────
        sec = sections["ep_ame"]
        _section_hdr(sec, "EXPORTING TO ADOBE MEDIA ENCODER")
        _tip(sec, "Tip: Already nested last session? Click Skip Nest on Phase 1 "
                  "instead of nesting again — Connect to AME will scan the "
                  "project directly and pick up every already-nested episode "
                  "(and the trailer, if one is found).")
        _step(sec, 1, "Connect to AME",
              "Launches (or foregrounds) Adobe Media Encoder so it's ready to "
              "receive queued episodes.")
        _step(sec, 2, "Preset auto-discovery",
              "The app looks for your installed LIVE.epr preset under "
              "~/Documents/Adobe/Adobe Media Encoder/*/Presets/ and uses it "
              "automatically. If it can't be found, you'll be asked to browse "
              "to it once — that path is remembered for the rest of the session.")
        _step(sec, 3, "Export styles",
              "LIVE is checked by default. MARKETING adds an _M-suffixed cut "
              "trimmed to the poster clip. SOCIAL MEDIA only applies to the "
              "trailer (if one was found) and queues two Watermark variants. "
              "Check any combination — each one queues as its own pass.")
        _step(sec, 4, "SRT",
              "Only does something alongside a checked LIVE pass — swaps in "
              "the \"LIVE WITH SRTs\" preset so AME also renders an .srt "
              "sidecar, which the app moves into your output folder on its "
              "own once AME finishes rendering it.")
        _step(sec, 5, "Update Date",
              "Renames each queued sequence's trailing _YYMMDD to today's "
              "date right before it's queued — useful for a same-day "
              "re-queue where the output filenames should reflect today, "
              "not whenever the episode was originally nested.")
        _step(sec, 6, "Queue Episodes",
              "Sends every checked, unqueued item to AME's render queue, "
              "named to match, in your Output Folder(s). This does not "
              "start rendering — hit render in Media Encoder yourself when "
              "you're ready.")
        _tip(sec, "Tip: hitting STOP mid-queue pauses it — the button becomes "
                  "\"Continue Queueing\" and picks up where it left off.")
        _tip(sec, "Tip: the progress bar changes color to match whichever "
                  "style is currently queueing — gold for LIVE, blue for "
                  "MARKETING, purple for SOCIAL MEDIA.")

        # Show first tab
        _show_tab("clips")

        # No separate menu on guide window — main app menu bar handles Help

        # Set fixed width, natural height, center on screen
        win.update_idletasks()
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        h = win.winfo_reqheight() or 500
        win.geometry(f"{WIN_W}x{h}+{(sw-WIN_W)//2}+{(sh-h)//2}")
        win.lift()
        win.focus_force()

    def _enable_reset_btn(self):
        """Enable the RESET ALL button."""
        if not hasattr(self, 'btn_reset'):
            return
        self._reset_armed[self._active_tab] = True
        if getattr(self, "_thinking_owns_reset_btn", False):
            # A running task currently owns this button as STOP (see
            # _start_thinking) — every caller of _enable_reset_btn()
            # ("something changed, arm the reset button") used to
            # unconditionally overwrite the button's text/click-binding
            # back to RESET ALL regardless of that, silently killing the
            # STOP takeover out from under a still-running task (seen
            # live: switching Title Card Track mode mid-scan flipped
            # STOP back to RESET ALL even though the scan kept running).
            # _reset_armed above still gets set so the correct RESET ALL
            # state is there once _stop_thinking()/_restore_reset_btn
            # actually hands the button back.
            return
        self.btn_reset._text = "RESET ALL"
        self.btn_reset._bg = "#2a6e2a"
        self.btn_reset._draw("#2a6e2a")
        self.btn_reset._action = self._do_reset
        self.btn_reset.bind("<Enter>", lambda e: self.btn_reset._draw("#3a8e3a"))
        self.btn_reset.bind("<Leave>", lambda e: self.btn_reset._draw(self.btn_reset._bg))
        self.btn_reset.bind("<ButtonPress-1>", lambda e: self.btn_reset._draw("#1a5e1a"))
        def _on_release(e):
            if self.btn_reset._text == "STOP":
                self.btn_reset._text = "RESET ALL"
                self.btn_reset._bg = "#2a6e2a"
                self.btn_reset._draw("#2a6e2a")
                self.btn_reset.after(10, self.btn_reset._action)
            else:
                self._disable_reset_btn()
                self.btn_reset.after(10, self.btn_reset._action)
        self.btn_reset.bind("<ButtonRelease-1>", _on_release)

    def _disable_reset_btn(self):
        """Disable the RESET ALL button - greyed out."""
        if not hasattr(self, 'btn_reset'):
            return
        self._reset_armed[self._active_tab] = False
        self.btn_reset._draw("#2a2a2a")
        self.btn_reset.unbind("<Enter>")
        self.btn_reset.unbind("<Leave>")
        self.btn_reset.unbind("<ButtonPress-1>")
        self.btn_reset.unbind("<ButtonRelease-1>")

    def _restore_reset_btn(self):
        def _do():
            self.btn_reset._text = "RESET ALL"
            self.btn_reset._bg = "#2a6e2a"
            self.btn_reset._draw("#2a6e2a")
            self._enable_reset_btn()
        self.after(0, _do)

    def _stop_export_now(self):
        self._stop_export = True
        self._log("⚠ Stop requested — will stop after current clip.", "warn")
        self.after(0, lambda: self.progress_status.config(
            text="Stop requested — finishing current clip..."))

    def _do_reset(self):
        """Reset the currently active tab back to its initial state. RESET
        ALL is scoped to whichever tab you're on — resetting the Episode
        Export tab doesn't touch VFX progress and vice versa."""
        # Safety valve for _thinking_depth (see _start_thinking/
        # _stop_thinking) — a genuine "give up and start over" click
        # should always be able to clear the shared spinner/STOP state
        # outright, even if some call path somewhere left the counter
        # imperfectly paired.
        self._thinking_depth = 0
        self._thinking_active = False
        self.after(0, self._disable_reset_btn)
        if hasattr(self, '_show_pill'):
            self._update_show_pill("")

        if self._active_tab == "vfx":
            self._stop_export = True
            self._stop_scan = True
            self._export_started = False
            self._vfx_run_complete = False
            self._all_disabled = False
            self._disabled_episodes.clear()
            self._create_xlsx.set(True)
            self._xlsx_check.set_enabled(False)
            self.engine = None
            self.episode_markers = []
            self.export_list = []
            self._refresh_toggle_all_btn()

            # Reset step circles — step 1 goes back to ready (reachable
            # immediately), steps 2/3 go back to grey/pending, matching
            # their construction-time defaults (see step_canvases' build).
            for i, c in enumerate(self.step_canvases):
                size = 24
                col = ACCENT if i == 0 else "#555555"
                c.delete("all")
                c.create_oval(1, 1, size-1, size-1, outline=col, width=2, fill="")
                c.create_text(size//2, size//2, text=str(i+1),
                             font=("SF Pro Display", 11, "bold"), fill=col)

            # Reset progress bar and status
            self._set_progress_normal()
            self.progress_var.set(0)
            self.progress_status.config(text="")

            # Reset text vars
            self.filename_preview.set("Connect to DaVinci to auto-fill...")
            self.start_timecode_display.set("Auto-detected on connect")
            self.hour_display.config(fg=TEXT_MUTED)

            # Reset Reference Video + Output Folder — both stay empty until
            # DaVinci reconnects, same as they are on first launch.
            self.reference_video_path.set("")
            self.output_dir.set("")
            self.ref_mode.set("auto")
            self.out_mode.set("auto")
            self._on_ref_mode_change()
            self._on_out_mode_change()

            # Clear log
            if self.log_box is not None:
                self.log_box.config(state="normal")
                self.log_box.delete("1.0", "end")
                self.log_box.config(state="disabled")

            # Clear episode tags — repack ep_tags_frame so it collapses to nothing
            for w in self._ep_tag_widgets:
                try: w.destroy()
                except: pass
            self._ep_tag_widgets.clear()
            self.ep_tags_frame.pack_forget()
            self.ep_tags_frame.pack(fill="x", pady=12)

            # Reset plates count
            self.plates_count_label.config(text="—")

            # Disable Reset Export button
            self._set_btn_state(self.btn_reset_export, False)

            # Restore export button text
            self.btn_export._text = "Export"

            # Reset all step buttons
            self._set_btn_state(self.btn_connect, True)
            self._set_btn_state(self.btn_scan, False)
            self._set_btn_state(self.btn_export, False)

            self._log("Reset. Ready to connect.", "success")

        elif self._active_tab == "test" and hasattr(self, '_pp_nest_status'):
            self._pp_full_reset()

        # RESET ALL itself is one button shared across both tabs — always
        # restore its default look regardless of which tab triggered this.
        self.btn_reset._text = "RESET ALL"
        self.btn_reset._bg = "#2a6e2a"
        self.btn_reset._draw("#2a2a2a")  # show greyed out

        # Resize window after all widgets are cleared
        self.update_idletasks()
        self.resizable(False, True)
        self.geometry(f"{APP_WIDTH}x{self.winfo_reqheight()}")
        self.resizable(False, False)

    def _do_connect(self):
        self._enable_reset_btn()
        self._set_btn_state(self.btn_connect, False)
        self.after(0, lambda: self._connect_status.config(text="Connecting..."))

        def _on_stop():
            # connect() below is a single blocking call with no mid-flight
            # checkpoint to interrupt, so this can't truly abort it — but
            # it reuses the identity check the task already does once
            # connect() eventually returns (self.engine is my_engine?):
            # setting self.engine to None here makes that check fail the
            # same way it already does after a RESET ALL landed mid-connect,
            # so the stale result gets discarded there instead of applied,
            # calling _stop_thinking() itself once that happens — this
            # just handles the immediate, visible part (button/status)
            # right away instead of waiting for the connect call to return.
            self.engine = None
            self.after(0, lambda: self._connect_status.config(text=""))
            self._set_btn_state(self.btn_connect, True)

        self._start_thinking(on_stop=_on_stop)
        def task():
            try:
                self._set_step_active(0)
                self.engine = ExportEngine(log_callback=self._log,
                                           progress_callback=self._update_progress)
                my_engine = self.engine
                info = self.engine.connect()
                if self.engine is not my_engine:
                    # RESET ALL (STOP) ran while connect() was in flight —
                    # self.engine is no longer this task's own instance
                    # (either None, from the reset, or a newer connect
                    # attempt's engine). Back off entirely rather than
                    # autofilling fields or re-enabling buttons for a
                    # connection the user already asked to abandon.
                    self.after(0, self._stop_thinking)
                    return
                self._set_btn_state(self.btn_connect, False)
                self._log(f"✓ Connected: {info['name']}.", "success")
                _sc = info.get("show_code", "").strip()
                _ac = info.get("show_acronym", "").strip()
                if _sc and _ac:
                    self.after(0, lambda s=f"{_sc}_{_ac}": self._update_show_pill(s, tab="vfx"))
                elif _sc:
                    self.after(0, lambda s=_sc: self._update_show_pill(s, tab="vfx"))
                self._set_step_done(0)
                self._set_step_ready(1)
                self.after(0, lambda: self._connect_status.config(text=""))

                def autofill():
                    # SHOW INFO section
                    if self.show_mode.get() == "auto":
                        self.show_code.set(info.get("show_code", ""))
                        self.show_acronym.set(info.get("show_acronym", ""))
                        self.export_date.set(datetime.now().strftime("%y%m%d"))
                        full_tc = info.get("start_timecode", "03:59:50:00")
                        self.start_timecode.set(full_tc)
                        self.start_timecode_display.set(full_tc)
                        self.hour_display.config(fg=ACCENT)
                        self._update_filename_preview()

                    # REFERENCE VIDEO section
                    if self.ref_mode.get() == "auto":
                        ref_path = get_offline_reference_path(
                            self.engine.project,
                            show_code=self.show_code.get().strip()
                        )
                        if ref_path:
                            self.reference_video_path.set(ref_path)
                            self._log(f"Ref video: {os.path.basename(ref_path)}.", "success")
                        else:
                            self._log("Reference video not found — switched to Manual. Please browse to your ref video.", "warn")
                            self.after(0, lambda: self.ref_mode.set("manual"))
                            self.after(0, self._on_ref_mode_change)
                            self.after(0, lambda: self.reference_video_path.set(""))

                    # Enable scan only if ref video is available
                    def _enable_scan_if_ref():
                        has_ref = bool(self.reference_video_path.get().strip())
                        self._set_btn_state(self.btn_scan, has_ref)
                        if not has_ref:
                            self._log("⚠ Scan Episodes disabled — no reference video selected.", "warn")
                    self.after(0, _enable_scan_if_ref)
                    self.after(0, self._stop_thinking)
                    self.after(0, lambda: self._xlsx_check.set_enabled(True))

                    # OUTPUT FOLDER section
                    if self.out_mode.get() == "auto":
                        found = find_output_folder(
                            info.get("show_code", ""),
                            info.get("show_acronym", ""),
                            log_callback=lambda m: self._log(m, "muted")
                        )
                        if found:
                            self.output_dir.set(found)
                            self._log(f"Output folder: {found}", "success")
                        else:
                            self._log("Output folder not found — switched to Manual. Please browse to your TO VFX folder. "
                                       "See the lines above for exactly which volumes/folders were checked.", "warn")
                            self.after(0, lambda: self.out_mode.set("manual"))
                            self.after(0, self._on_out_mode_change)
                            self.after(0, lambda: self.output_dir.set(""))
                self.after(0, autofill)

            except Exception as e:
                self._log(f"✗ {e}", "error")
                self.after(0, lambda: self._connect_status.config(text=""))
                self.after(0, lambda: self._set_btn_state(self.btn_connect, True))
                self.after(0, self._stop_thinking)
        threading.Thread(target=task, daemon=True).start()

    def _do_scan(self):
        # Immediate UI feedback
        self._xlsx_check.set_enabled(False)
        self._set_btn_state(self.btn_scan, False)
        self.progress_status.config(text="Preparing scan...")
        self.progress_var.set(0)
        self._set_progress_normal()
        self._set_step_active(1)
        self._start_thinking(manage_reset_btn=False)
        # Disable STOP until scan actually starts
        self._disable_reset_btn()
        self.btn_reset._text = "STOP"
        self.btn_reset._bg = "#8e2a2a"
        self.btn_reset._draw("#6e1a1a")
        self.btn_reset.unbind("<Enter>")
        self.btn_reset.unbind("<Leave>")
        self.btn_reset.unbind("<ButtonPress-1>")
        self.btn_reset.unbind("<ButtonRelease-1>")
        def task():
            try:
                self._scanning = True
                # Now scanning for real — enable STOP button
                def _enable_stop_scan():
                    self.btn_reset._text = "STOP"
                    self.btn_reset._bg = "#8e2a2a"
                    self.btn_reset._draw("#8e2a2a")
                    self.btn_reset.bind("<Enter>", lambda e: self.btn_reset._draw("#ae3a3a"))
                    self.btn_reset.bind("<Leave>", lambda e: self.btn_reset._draw(self.btn_reset._bg))
                    self.btn_reset.bind("<ButtonPress-1>", lambda e: self.btn_reset._draw("#6e1a1a"))
                    def _stop_scan_release(e):
                        # Disable immediately on click
                        self.btn_reset.unbind("<Enter>")
                        self.btn_reset.unbind("<Leave>")
                        self.btn_reset.unbind("<ButtonPress-1>")
                        self.btn_reset.unbind("<ButtonRelease-1>")
                        self.btn_reset._draw("#6e1a1a")
                        self.progress_status.config(text="Stopping scan...")
                        self._stop_scan_now()
                    self.btn_reset.bind("<ButtonRelease-1>", _stop_scan_release)
                self.after(0, _enable_stop_scan)
                self.after(0, lambda: self.progress_status.config(text="Scanning episodes..."))

                # Pass ref video path regardless of mode - already detected during connect
                ref = self.reference_video_path.get().strip() or None
                tc = self.start_timecode.get().strip() or "03:59:50:00"
                self._stop_scan = False
                self.episode_markers = self.engine.scan_episodes(
                    ref, start_timecode=tc, stop_flag=lambda: self._stop_scan)
                self._scanning = False
                self.after(0, self._stop_thinking)

                if self._stop_scan:
                    self._scanning = False
                    self._set_progress_normal()
                    self.after(0, lambda: self.progress_var.set(0))
                    self._log(f"⚠ Scan stopped. Found {len(self.episode_markers)} episodes so far.", "warn")
                    self.after(0, lambda: self.progress_status.config(
                        text="Scan stopped. Click Scan Episodes to retry."))
                    self.after(0, lambda: self._xlsx_check.set_enabled(True))
                    self.after(0, self._restore_reset_btn)
                    self._set_btn_state(self.btn_scan, True)
                    return

                # Scan completed successfully
                self._log(f"✓ Detected {len(self.episode_markers)} episodes", "success")

                # Silently count plates (suppress progress to avoid bar dipping)
                try:
                    self._suppress_progress = True
                    code = self.show_code.get().strip()
                    acronym = self.show_acronym.get().strip()
                    date = self.export_date.get().strip()
                    if not code: code = "SHOW"
                    if not acronym: acronym = "ACRN"
                    if not date: date = datetime.now().strftime("%y%m%d")
                    preview = self.engine.prepare_export(code, acronym, date)
                    self._suppress_progress = False
                    self.export_list = preview
                    self.after(0, self._update_plate_count)
                    self._log(f"  {len(preview)} plates ready to export.", "muted")
                except Exception as e:
                    self._suppress_progress = False
                    self._log(f"  Could not count plates: {e}", "muted")

                # Hit 100% first, then show episodes
                self._update_progress(100, None)
                self._set_progress_green()
                self._set_step_done(1)
                self._set_step_ready(2)
                # Fresh scan — the "run complete, needs a rescan" lock on
                # DISABLE ALL/ENABLE ALL no longer applies.
                self._vfx_run_complete = False
                self._update_episode_list()
                self.after(0, lambda: self.progress_status.config(text="Click Export to Continue."))
                self.after(0, self._restore_reset_btn)
                self.after(0, lambda: self._set_btn_state(self.btn_scan, False))
                # Only enable export if output folder is set
                has_output = bool(self.output_dir.get().strip())
                self._set_btn_state(self.btn_export, has_output)
                if not has_output:
                    self._log("⚠ Export disabled — please select an output folder.", "warn")
                self.after(0, lambda: self._xlsx_check.set_enabled(True))
            except FileNotFoundError:
                self._scanning = False
                self.after(0, self._stop_thinking)
                self._log("✗ Reference video not found. Please browse to it in the Reference Video section.", "error")
                self._restore_reset_btn()
                self._set_btn_state(self.btn_scan, True)
            except Exception as e:
                self._scanning = False
                self.after(0, self._stop_thinking)
                self._log(f"✗ {e}", "error")
                self._restore_reset_btn()
                self._set_btn_state(self.btn_scan, True)
        threading.Thread(target=task, daemon=True).start()


    def _do_export(self):
        output = self.output_dir.get().strip()
        if not output:
            messagebox.showerror("Missing Info", "Please set an output folder.")
            return

        # Prepare list synchronously on main thread (fast, no DaVinci API calls)
        try:
            if not self.export_list:
                self._suppress_progress = True
                self.export_list = self.engine.prepare_export(
                    self.show_code.get().strip(),
                    self.show_acronym.get().strip(),
                    self.export_date.get().strip()
                )
                self._suppress_progress = False

            # Apply disabled episode filter
            disabled = self._disabled_episodes.copy()
            if disabled:
                self.export_list = [
                    item for item in self.export_list
                    if item.get("episode_code") not in disabled
                ]
                self._log(f"Skipping episodes: {', '.join(sorted(disabled))}.", "muted")
                self._log(f"  Update: {len(self.export_list)} plates to export.", "muted")

            # Sync to engine
            self.engine.export_list = self.export_list
            self._update_plate_count()

        except Exception as e:
            self._suppress_progress = False
            self._log(f"✗ Failed to prepare clips: {e}", "error")
            return

        # Now start the actual export
        self._run_export_task(output, start_index=0)

    def _browse_reference(self):
        path = filedialog.askopenfilename(
            title="Select Reference Video",
            filetypes=[("Video files", "*.mov *.mp4 *.mxf *.avi"), ("All files", "*.*")]
        )
        if path:
            self.reference_video_path.set(path)
            self._log(f"Ref video set to: {os.path.basename(path)}.", "success")

    def _browse_output(self):
        path = filedialog.askdirectory(title="Select Output Folder")
        if path:
            self.output_dir.set(path)

    def _log(self, message, tag=None):
        self._log_buffer.append((message, tag or ""))
        def _do():
            if self.log_box is None:
                return
            self.log_box.config(state="normal")
            self.log_box.insert("end", message + "\n", tag or "")
            self.log_box.see("end")
            self.log_box.config(state="disabled")
        self.after(0, _do)

    def _update_progress(self, pct, msg=None):
        if getattr(self, '_suppress_progress', False):
            return
        import time
        now = time.time()
        # Throttle UI updates to max once per 150ms to keep main thread free
        last = getattr(self, '_last_progress_update', 0)
        if now - last < 0.15 and msg and msg.startswith("Exporting Plate:"):
            return  # Skip this update, UI already recent enough
        self._last_progress_update = now

        def _do():
            self.progress_var.set(pct)
            if msg and msg.startswith("Exporting Plate:"):
                if hasattr(self, 'progress_status'):
                    self.progress_status.config(text=msg)
            elif msg and not getattr(self, '_scanning', False):
                self._log(msg, "muted")
        self.after(0, _do)

    def _update_episode_list(self):
        def _do():
            for w in self._ep_tag_widgets:
                try: w.destroy()
                except: pass
            self._ep_tag_widgets.clear()

            # Build tags in wrapping rows
            tw, th, r, gap = 72, 24, 6, 5
            # Get available width - default to 600 if not yet rendered
            avail = max(self.ep_tags_frame.winfo_width() - 16, 400)
            cols = max(1, avail // (tw + gap))

            row_frame = None
            for idx, m in enumerate(self.episode_markers):
                if idx % cols == 0:
                    row_frame = tk.Frame(self.ep_tags_frame, bg="#252525")
                    row_frame.pack(fill="x", pady=(0, 2))
                    self._ep_tag_widgets.append(row_frame)

                ep = m.episode_code
                disabled = ep in self._disabled_episodes
                locked = getattr(self, '_export_started', False)
                if locked:
                    tag_fill = "#3d3d3d" if disabled else "#5a4a1a"
                    text_fill = "#888888"
                    toggle_char = "⊘"
                    text_fill = "#888888" if disabled else "#666666"
                else:
                    tag_fill = "#3d3d3d" if disabled else ACCENT
                    text_fill = "#888888" if disabled else "#000000"
                    toggle_char = "+" if disabled else "×"

                tag = tk.Canvas(row_frame, bg="#252525", highlightthickness=0,
                               width=tw, height=th)
                tag.pack(side="left", padx=(0, gap), pady=2)

                x1, y1, x2, y2 = 0, 0, tw, th
                pts = [x1+r,y1, x2-r,y1, x2,y1, x2,y1+r,
                       x2,y2-r, x2,y2, x2-r,y2, x1+r,y2,
                       x1,y2, x1,y2-r, x1,y1+r, x1,y1]
                tag.create_polygon(pts, fill=tag_fill, outline="", smooth=True)
                tag.create_text(12, th//2, text=toggle_char,
                               font=("SF Pro Display", 12, "bold"), fill=text_fill)
                tag.create_line(22, 4, 22, th-4, fill=text_fill)
                tag.create_text(22 + (tw-22)//2, th//2, text=ep,
                               font=("SF Pro Display", 10, "bold"), fill=text_fill)

                def _on_click(e, episode=ep):
                    if getattr(self, '_export_started', False):
                        return  # Locked during export
                    if episode in self._disabled_episodes:
                        self._disabled_episodes.discard(episode)
                    else:
                        self._disabled_episodes.add(episode)
                    self._update_episode_list()
                    self._update_plate_count()

                tag.bind("<ButtonRelease-1>", _on_click)
                self._ep_tag_widgets.append(tag)
            self._refresh_toggle_all_btn()

        def _do_and_resize():
            _do()
            self.update_idletasks()
            h = self.winfo_reqheight()
            self.geometry(f"{APP_WIDTH}x{h}")
        self.after(0, _do_and_resize)

    def _vfx_toggle_all_locked(self):
        """Shared guard for VFX's DISABLE ALL/ENABLE ALL — nothing to
        safely act on (no episodes scanned yet), an export actively in
        flight, or one that just finished completely (deliberate —
        requires a fresh Scan Episodes to unlock again, not just Reset
        Export/Reset All). Mirrors the Episode Export tab's
        _pp_exp_util_locked."""
        return (getattr(self, '_export_started', False)
                or getattr(self, '_vfx_run_complete', False)
                or not self.episode_markers)

    def _toggle_all_episodes(self):
        """Toggle all episodes on or off."""
        if self._vfx_toggle_all_locked():
            return
        if self._all_disabled:
            self._disabled_episodes.clear()
        else:
            self._disabled_episodes = set(m.episode_code for m in self.episode_markers)
        self._update_episode_list()
        self._update_plate_count()

    def _refresh_toggle_all_btn(self):
        """Keeps DISABLE ALL/ENABLE ALL's label truthful to what's actually
        on screen — derived from the episode markers every rebuild instead
        of only tracked through the bulk-toggle button — and greys it out
        whenever _vfx_toggle_all_locked() says there's nothing safe to act
        on right now. Mirrors the Episode Export tab's
        _pp_refresh_exp_util_buttons."""
        toggleable = [m.episode_code for m in self.episode_markers]
        self._all_disabled = bool(toggleable) and all(ep in self._disabled_episodes for ep in toggleable)
        locked = self._vfx_toggle_all_locked()
        bg, fg = ("#2a2a2a", "#555555") if locked else (BG_INPUT, TEXT_PRIMARY)
        self.btn_toggle_all.config(
            text="ENABLE ALL" if self._all_disabled else "DISABLE ALL", bg=bg, fg=fg)

    def _update_plate_count(self):
        if not self.export_list:
            return
        disabled = getattr(self, '_disabled_episodes', set())
        count = sum(1 for item in self.export_list
                   if item.get("episode_code", "") not in disabled)
        self.plates_count_label.config(text=str(count))

    def _set_btn_state(self, btn, enabled):
        """Every button in this app is a _rounded_btn-drawn tk.Canvas —
        the plain tk.Label button constructor (_btn) this used to also
        support was removed as dead code, so this only ever needs the
        canvas-button path now."""
        def _do():
            btn._enabled = enabled
            danger = getattr(btn, "_danger", False)
            if enabled:
                bg = ACCENT if btn._accent else ("#8e2a2a" if danger else "#444444")
                fg = "#000000" if btn._accent else "#FFFFFF"
            else:
                bg = "#5a4a1a" if btn._accent else "#333333"
                fg = "#888888" if btn._accent else "#555555"
            btn._bg = bg
            btn._fg = fg
            btn._draw(bg, fg)
            btn.config(cursor="" if enabled else "arrow")
            btn.unbind("<Enter>")
            btn.unbind("<Leave>")
            # These canvas buttons are bound via <ButtonPress-1>/
            # <ButtonRelease-1> (see _rounded_btn), never the generic
            # <Button-1> — unbinding that was a no-op, leaving a
            # previously-enabled button's old <ButtonRelease-1> (which
            # calls _command()) live underneath the greyed-out visual.
            btn.unbind("<ButtonPress-1>")
            btn.unbind("<ButtonRelease-1>")
            if enabled:
                hover = ACCENT_HOVER if btn._accent else ("#ae3a3a" if danger else "#555555")
                press = "#c07010" if btn._accent else ("#6e1a1a" if danger else "#666666")
                btn.bind("<Enter>", lambda e, b=btn, h=hover, f=fg: b._draw(h, f))
                btn.bind("<Leave>", lambda e, b=btn, f=fg: b._draw(b._bg, f))
                btn.bind("<ButtonPress-1>", lambda e, b=btn, p=press, f=fg: b._draw(p, f))
                def _on_release(e, b=btn, f=fg):
                    b._draw(b._bg, f)
                    b._command()
                btn.bind("<ButtonRelease-1>", _on_release)
        self.after(0, _do)


if __name__ == "__main__":
    import traceback
    try:
        app = VFXExporterApp()
        app.mainloop()
    except Exception as e:
        try:
            import tkinter.messagebox as mb
            mb.showerror("Startup Error", traceback.format_exc())
        except Exception:
            traceback.print_exc()
