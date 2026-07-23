# Finishing Tool — Developer README

## What This Is

A macOS desktop app built for a Finishing Editor at a vertical drama production company. It automates two of the most repetitive and error-prone tasks in post-production finishing:

1. **VFX plate exporting** from DaVinci Resolve Studio
2. **Episode nesting and LIVE export** from Premiere Pro

The app is a single Python file (`main.py`, ~4200 lines) built with `tkinter`. It has a dark, custom-styled UI that looks nothing like standard tkinter — all buttons, tabs, progress bars, and interactive elements are custom canvas-drawn. It connects to DaVinci Resolve via its official Python scripting API, and to Premiere Pro via the **Pymiere** library (a Python wrapper around Premiere's ExtendScript API).

---

## The Production Context

The editor works on vertical drama (9:16 aspect ratio, phone-format content). Each "show" has:
- ~57–90 episodes split across 3–4 "reels" (~20 episodes each)
- A DaVinci Resolve timeline called a "stringout" with all episodes in sequence
- VFX plates that need to be extracted and sent to a VFX department
- A Premiere Pro timeline (also a stringout) where episodes are nested and exported for QC

### Naming Convention

Everything follows the pattern:
```
SHOWCODE_ACRONYM_STAGE_EP##_YYMMDD[_suffix]
```

Examples:
- `LA35_HGTBH_FINAL_EP01_260704`
- `LA35_HGTBH_VFX_EP01-20_260630_COLOR`
- `LA35_HGTBH_FINAL_EP01_260704_CLEAN`

**Show codes** look like `LA35`, `VC30`, `CA30`, `ZZ40` (2 letters + 2 numbers). Drives are often prefixed with `V-` or `I-` (e.g. `V-LA35`, `I-LA35`) — the app strips these prefixes when searching.

---

## App Architecture

### Files
| File | Purpose |
|------|---------|
| `main.py` | Entire app — ~4200 lines, single file |
| `version.json` | `{"version": "1.0.1"}` — checked on startup for auto-updates |
| `installer.py` | Standalone installer app (downloads + builds main.py) |
| `installer.spec` | PyInstaller spec for building the installer .app |
| `setup.py` | py2app config for building main.py into Finishing Tool.app |
| `build_and_install.sh` | Shell script: creates venv, pip installs, runs py2app, copies to /Applications |
| `build_icon.py` | Generates the app icon |

### Build Commands
```bash
# Run in development
cd '/Users/juanesandi/Downloads/finishing-tool-main'
/Library/Frameworks/Python.framework/Versions/3.13/bin/python3 main.py

# Build and install to /Applications
cd '/Volumes/JUAN_FINISHING SSD/Finishing Tool Builder'
bash build_and_install.sh

# Build installer .app
cd '/Volumes/JUAN_FINISHING SSD/Finishing Tool Builder'
pyinstaller installer.spec
```

### Python Version
Python 3.13 at `/Library/Frameworks/Python.framework/Versions/3.13/bin/python3`

### Dependencies
```
opencv-python   # cv2 — ref video frame capture for screenshots
pytesseract     # OCR for episode number detection from ref video
pillow          # PIL — image processing
openpyxl        # xlsx reading
xlsxwriter      # xlsx writing (plate list)
numpy           # array ops
py2app          # macOS app bundling
pymiere         # Premiere Pro Python bridge
```

---

## UI Design System

All colors are constants at the top of `main.py`:
```python
BG_OUTER  = "#161616"   # outermost window background
BG_DARK   = "#1E1E1E"   # tab content area background
BG_PANEL  = "#2A2A2A"   # section panel boxes (Show Info, etc.)
BG_INPUT  = "#333333"   # text entry fields
ACCENT    = "#E8A838"   # orange/gold — primary interactive color
TEXT_PRIMARY = "#FFFFFF"
TEXT_MUTED   = "#888888"
TEXT_SUCCESS = "#5DBD74"
TEXT_ERROR   = "#E05555"
TEXT_WARN    = "#E8A838"  # same as ACCENT
```

### Custom Widget Vocabulary

**Rounded Button (`_rounded_btn`)** — Canvas-drawn button. Has `._text`, `._bg`, `._fg`, `._draw(color)`, `._action` properties. Used for all major action buttons. `accent=True` makes it gold/orange, default is dark grey `#444`.

**Step Circle** — Canvas-drawn numbered circle (24×24px, orange outline). Used in both tabs to show workflow progress. States: numbered (orange outline), done (green outline + ✓ checkmark matching `TEXT_SUCCESS`).

**Episode Chips / Episode Tags** — Small canvas-drawn rounded-rectangle labels showing episode numbers (e.g. EP01, EP02). In VFX tab: `tw=72, th=24, r=6`. In Episode Export tab: `tw=54, th=24, r=6` with canvas `56×26` to avoid outline clipping.

Chip color states (Episode Export tab):
- Grey `#252525` fill, `#555555` text → not yet processed
- `#3a2a0a` fill, `ACCENT` text + `ACCENT` outline → currently active (nesting or exporting)
- `#1a3a1a` fill, `#5ae05a` text → nested/done
- `ACCENT` fill, `#000000` text → exported/done

Chip color states (VFX tab):
- `#252525` fill → not processed
- `ACCENT` fill, `#000000` text → active (being exported)
- Disabled/locked: `#5a4a1a` fill

**Show Pill** — Small label in the title bar showing `SHOWCODE_ACRONYM` after connecting (e.g. `LA35_HGTBH`). Background `#0e0e0e`.

**RESET ALL Button** — Canvas-drawn green button (`#2a6e2a`) in top-right corner. Becomes a red STOP button (`#8e2a2a`) when a task is running. Restores after task completes/stops via `_restore_reset_btn()`.

**Thinking Animation** — Bouncing dots GIF (`thinking.gif`) displayed in the tab bar to the right of the tabs during async operations. Started with `_start_thinking()`, stopped with `_stop_thinking()`. A text label (`self._connect_status`) sits to the left of it showing task status (e.g. "Connecting to Premiere...").

**Progress Bar** — `ttk.Progressbar` with `thickness=10`, `ACCENT` color. Lives below the export buttons in the VFX tab. Episode Export tab has two canvas-drawn progress bars (one per phase).

**Log Box** — `tk.Text` widget, `height=8`, `FONT_MONO`, `BG_INPUT` background. Hidden behind SHOW LOG / HIDE LOG toggle button. Tags: `success` (green), `error` (red), `warn` (orange), `muted` (grey).

**Section Label** — Uppercase grey small-caps label (`TEXT_MUTED`, `10px`, letter-spacing). Used to separate sections (e.g. "SHOW INFO", "DETECTED EPISODES").

**Panel Box** — `tk.Frame` with `bg=BG_PANEL` (`#2A2A2A`), used to group related inputs (Show Info, Reference Video, Output Folder sections).

**Dark Chip Box** — `tk.Frame` with `bg="#252525"`, used for episode chip areas and Reel Summary. Slightly darker than BG_PANEL.

---

## Tab 1: VFX EXPORT

### Purpose
Connects to DaVinci Resolve, scans a stringout timeline for VFX plates (identified by clip color), exports them to the TO VFX output folder, and generates an Excel "List to Post" with screenshots.

### Workflow
1. **Connect to Resolve** — Reads the active timeline name, auto-fills Show Info, auto-detects reference video and output folder
2. **Scan Episodes** — Uses OCR (pytesseract + OpenCV) to read the reference video frame-by-frame and detect episode number changes
3. **Export** — Renders plates via DaVinci Resolve's Deliver page using render preset `"02_COLORED VFX 4444 XQ"`, then generates xlsx

### Clip Color Logic
- **Orange** → VFX plate, exported normally
- **Apricot** → Clean plate, gets `_CLEAN` suffix
- **Chocolate** → Reused/skip, not exported
- **Stacked orange clips** → `_V1`, `_V2`, `_V3` suffixes for each layer

### Output Path
```
/Volumes/V-SHOWCODE_*/SHOWCODE_*_EDIT/05_TURNOVER/06_TO VFX/
```
Auto-detected by scanning `/Volumes/` for drives matching the show code. Handles `V-`, `I-` prefixes.

### List to Post (xlsx)
Generated after export at:
```
TO VFX/LIST TO POST/SHOWCODE_ACRONYM_VFX_LIST TO POST_YYMMDD.xlsx
```
Columns: FILE NAME / EPISODE / PLATES TO TURNOVER / VFX REF TC IN / VFX REF TC OUT / TURNOVER STATUS / TURNOVER NOTE / VFX / VFX NOTE / SCREENSHOT

Screenshots are embedded in the xlsx at full resolution (720×1280), scaled uniformly to fit cell height (`190/1280` scale factor), centered horizontally in the cell.

### Key Methods (VFX tab)
| Method | Purpose |
|--------|---------|
| `_do_connect()` | Connects to Resolve, fills Show Info |
| `_do_scan()` | OCR scan of ref video for episode markers |
| `_do_export()` | Main export thread launcher |
| `_run_export_task()` | Background export worker |
| `_stop_export_now()` | Sets `_stop_export = True` flag |
| `_do_reset()` | Full reset of VFX tab + Episode Export tab |
| `_update_episode_list()` | Rebuilds episode chip display |
| `find_output_folder()` | Auto-detects TO VFX folder |
| `get_offline_reference_path()` | Finds ref video in Media Pool |
| `generate_plate_list_xlsx()` | Builds the List to Post excel file |

---

## Tab 2: EPISODE EXPORT

### Purpose
Connects to Premiere Pro, auto-detects the episode title card track, creates subsequences (functionally identical to nests) for each episode, then queues them to Adobe Media Encoder for LIVE export.

### Key Concept: Subsequences ≠ True Nests
Premiere's scripting API doesn't support true nesting. Instead, `seq.createSubsequence(False)` creates a new sequence from the current in/out points. Each subsequence's timecode is reset to `00:00:00:00` with `sub.setZeroPoint("0")`. This is functionally identical to a nest for QC and export purposes.

### Phase 1 — Nest Episodes

**Step 1 — Connect to Premiere**
- Uses Pymiere library → HTTP connection to Pymiere Link CEP extension running inside Premiere
- Pymiere Link must be installed (the installer handles this automatically)
- `defaults write com.adobe.CSXS.11 PlayerDebugMode 1` must be run once in Terminal to allow unsigned extensions
- Active sequence in Premiere must be the stringout

**Step 2 — Title Card Track detection**
Auto-detection logic:
1. Scans tracks V8 and above only
2. Reads the first 3 clips of each track
3. Looks for clips whose names start with `"EPISODE"` (case-insensitive)
4. Falls back to clip count heuristic (15–30 clips) if no EPISODE-named clips found
5. Also scans last clip of every track above V4 for the Tails clip (`"Tail Leader"` — any name containing `"tail"`)

Manual override: Auto/Manual radio buttons. In Manual mode, dropdown shows only episode-candidate tracks.

**Step 3 — Starting Episode #**
User enters the first episode number for this reel (Reel 1 = 1, Reel 2 = 21, Reel 3 = 41). The Reel Summary shows `Next Start #` after each reel completes.

**Nesting pre-processing:**
Before nesting, the app mutes:
- All tracks **below** the title card track index
- Any track whose first 3 clips contain `"REF"` in the clip name (reference video tracks)

**Nesting loop:**
1. Sets `seq.setInPoint(ep_start)` / `seq.setOutPoint(ep_end)`
2. Calls `seq.createSubsequence(False)` — `False` = USE in/out points
3. Calls `sub.setZeroPoint("0")` — must be string, not int
4. Renames: `SHOWCODE_ACRONYM_FINAL_EP##_YYMMDD`
5. Restores original in/out after all episodes

**EPISODES NESTED chip box** — `#252525` background panel showing all episode chips that update live during nesting.

### Phase 2 — Export Live (In Progress)

**Step 1 — Output Folder**
Auto-detected path:
```
/Volumes/SHOWCODE_*/SHOWCODE_*_EDIT/##_DELIVERY/##_FINAL/##_LIVE/
```

**Step 2 — Queue Episodes**
Button sends all nested sequences to Adobe Media Encoder using the `"LIVE"` preset. AME exports as `.mp4`. User manually hits render in AME. **Not yet fully implemented.**

**EXPORT QUEUE chip box** — Same `#252525` panel, chips turn orange (`ACCENT` fill) when exported.

### Reel Summary Panel
Shows running totals across multiple reels in a session:
- **REELS NESTED** — how many reels have been fully nested
- **EPISODES** — total episode count this session
- **LAST NESTED** — last episode number (e.g. EP20)
- **EXPORTED** — how many exported to AME
- **NEXT START #** — what to enter as Starting Episode # for the next reel (green color)

### Key Methods (Episode Export tab)
| Method | Purpose |
|--------|---------|
| `_build_episode_export_tab()` | Builds entire Episode Export UI |
| `_pp_connect()` | Starts connect thread |
| `_pp_connect_task()` | Background: connects Pymiere, scans tracks, detects output |
| `_pp_scan_tracks()` | Scans Premiere tracks for title cards + tails |
| `_pp_run_autonest()` | Validates inputs, starts nest thread, turns RESET ALL to STOP |
| `_pp_autonest_task()` | Background: loops through title cards, creates subsequences |
| `_pp_on_nest_complete()` | Called when all episodes nested — unlocks Phase 2 |
| `_pp_build_nest_chips()` | Builds episode chip grid |
| `_pp_set_nest_chip_active()` | Colors chip yellow (currently nesting) |
| `_pp_set_nest_chip_done()` | Colors chip green (nested) |
| `_pp_full_reset()` | Resets entire Episode Export tab state |
| `_pp_run_export()` | Queue to AME (placeholder — not yet implemented) |
| `_pp_toggle_log()` | Show/hide log box |
| `_pp_log()` | Appends to episode export log |
| `_pp_detect_output()` | Auto-detects LIVE delivery folder |

---

## RESET ALL / STOP Button Logic

The green `RESET ALL` button in the top-right corner is the only global control. It works for both tabs:

- **Idle** → Green `#2a6e2a`, text "RESET ALL", calls `_do_reset()`
- **Task preparing** → Disabled (dark red, no interaction) + status shows "Preparing..."
- **Task running** → Red `#8e2a2a`, text "STOP", stops the current task
- **After stop** → Restored to green RESET ALL via `_restore_reset_btn()`

`_do_reset()` resets the VFX tab AND calls `_pp_full_reset()` for the Episode Export tab.

Note: `btn_reset._draw(color)` takes **1 argument** (just the fill color). This is different from `_rounded_btn` buttons which use `_draw(fill, text_color)` (2 arguments).

---

## Auto-Update System

On startup, the app checks `VERSION_URL` (GitHub raw) for a newer version. If found, a green banner appears below the title bar with "UPDATE NOW" and "LATER" buttons. Clicking "UPDATE NOW" downloads the new `main.py` to the same directory and restarts the app.

---

## Installer

The installer (`installer.py`) is built into a standalone macOS app with PyInstaller. It:
1. Downloads all required files from GitHub (main.py, thinking.gif, icon.png, version.json, build scripts, render presets, Pymiere Link extension)
2. Installs DaVinci render presets to `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Presets/Render/`
3. Installs Pymiere Link CEP extension to `~/Library/Application Support/Adobe/CEP/extensions/`
4. Runs `build_and_install.sh` to build the app with py2app and copy to `/Applications/`

Progress bar: Steps 1–3 = 10% / 10% / 15%, Step 4 (build) starts at 35% and adds 5% every 10 seconds, caps at 95% until done, then snaps to 100%.

---

## Known Issues & Current State (as of last session)

### VFX Export Tab
- ✅ Fully functional end-to-end
- ✅ xlsx with embedded screenshots working
- ✅ RESET ALL / STOP button working correctly
- ✅ Episode chips, log, progress bar all working

### Episode Export Tab
- ✅ Connect to Premiere working
- ✅ Track auto-detection working (EPISODE-named clips, V8+)
- ✅ Tails clip detection working (last clip of tracks V5+)
- ✅ Nesting working (`createSubsequence(False)`, `setZeroPoint("0")`)
- ✅ Episode chips build and animate during nesting
- ✅ REF track muting before nesting
- ⚠️ Log box SHOW/HIDE button has a bug — `_pp_toggle_log` references wrong frame name in some versions
- ⚠️ Radio buttons appear grey on macOS until window is refocused (macOS tkinter rendering bug)
- ⚠️ Bouncing dots sometimes don't stop after connecting
- ⚠️ Phase 1/2 panel boxes need to be removed (steps should render on BG_DARK, only chip boxes should have #252525 panels)
- ❌ Queue Episodes (AME export) not yet implemented

### Pending UI fixes (Episode Export tab)
1. Remove `BG_PANEL` wrapper from Phase 1 & 2 steps — use `BG_DARK` directly
2. Keep `#252525` panel only for EPISODES NESTED and EXPORT QUEUE chip boxes
3. REEL SUMMARY panel should use `#252525` not `BG_PANEL`
4. Chips should be `tw=54` (not 72), canvas `56×26` so outline isn't clipped
5. "Queue Episodes" button text (not "Export Live")
6. Radio button grey fix: force `update_idletasks()` + `rb.config(bg=BG_DARK)` on tab switch
7. Log toggle: `_pp_log_frame_outer` is correct name, `_pp_log_outer_frame` is wrong (old leftover)
8. "Connecting to Premiere..." should appear in `self._connect_status` label (tab bar) same as VFX tab

---

## Future Plans

### Setup Guide (Planned)
Two separate floating windows, same visual style:
- **VFX Export Guide** — tabs: PREPARING CLIPS / TIMELINE / REF VIDEO / OUTPUT FOLDER
- **Episode Export Guide** — tabs: PREMIERE SETUP / TIMELINE / NESTING / EXPORTING
- `?` button on each tab opens relevant guide

### AME Integration (In Progress)
- Auto-launch AME if not open: `subprocess.Popen(["open", "-a", "Adobe Media Encoder"])`
- Queue nested sequences via `pymiere.objects.app.encoder`
- Use existing `"LIVE"` preset in AME
- Output to `##_DELIVERY/##_FINAL/##_LIVE/` folder
- User manually hits render in AME (app does not auto-render)
- All episodes export as `.mp4` to same LIVE folder

### VERSIONING Tab (Planned — Tab 3)
After LIVE export is done, a third tab handles version exports:
- **MARKETING** — same as LIVE but music track muted
- **SOCIAL MEDIA** — with embedded subtitles
- **SOCIAL MEDIA + WATERMARK** — watermark variant
- Uses same AME preset system, separate presets for each version

### Additional Future Items
- SRT batch export
- Persist reel summary across sessions (currently resets on app restart)
- Setup guide integration with installer
- Support for multi-show workflows (show picker)

---

## GitHub

Repository: `github.com/esandijp-dotcom/finishing-tool`

Files that need to be on GitHub (downloaded by installer):
- `main.py`
- `version.json`
- `thinking.gif`
- `icon.png`
- `build_and_install.sh`
- `setup.py`
- `build_icon.py`
- `01_STRINGOUT Render.xml`
- `02_COLORED VFX 4444 XQ Render.xml`
- `03_PREMIERE XML Render.xml`
- `pymiere_link/` (CEP extension folder)

Release workflow:
1. Edit `main.py` → push to GitHub
2. Bump `version.json` → push to GitHub
3. Users see update banner on next app launch → auto-downloads + restarts
