# Finishing Tool v1.0
Finishing pipeline automation for episodic post-production — by Juan Esandi

---

## What it does
Two independent workflows in one app:

**VFX Export** — connects to DaVinci Resolve Studio via its API and exports VFX plates for post-production turnovers. Automatically detects show info from the timeline name, scans episodes via OCR on the reference video, exports clips track by track, takes screenshots, and generates a .xlsx List to Post.

**Episode Export** — connects to Adobe Premiere Pro via `pymiere` to nest a STRINGOUT reel into individual episode subsequences (one per title card), then queues them to Adobe Media Encoder for LIVE/MARKETING/SOCIAL MEDIA delivery, with SRT sidecar support.

Either half works independently — a machine only needs DaVinci Resolve Studio installed for VFX Export, or only Premiere Pro for Episode Export.

---

## Files

### In this folder (local build):
`main.py`, `installer.py`, `installer.spec`, `setup.py`, `build_icon.py`, `icon.png`, `thinking.gif`, `version.json`, `pymiere_link.zxp`

### On GitHub (github.com/esandijp-dotcom/finishing-tool):
`main.py`, `installer.py`, `thinking.gif`, `icon.png`, `version.json`, `build_and_install.sh`, `setup.py`, `build_icon.py`, `pymiere_link.zxp`, `LIVE.epr`, `MARKETING.epr`, `SOCIAL MEDIA.epr`, `LIVE WITH SRTs.epr`, `01_STRINGOUT Render.xml`, `02_COLORED VFX 4444 XQ Render.xml`, `03_PREMIERE XML Render.xml`

---

## All commands
Run from: `/Volumes/JUAN_FINISHING SSD/Finishing Tool Builder`

```bash
# Run app without building
cd '/Volumes/JUAN_FINISHING SSD/Finishing Tool Builder' && .venv/bin/python3 main.py

# Run installer without building
cd '/Volumes/JUAN_FINISHING SSD/Finishing Tool Builder' && .venv/bin/python3 installer.py

# Build app (output to 'App Build' folder)
cd '/Volumes/JUAN_FINISHING SSD/Finishing Tool Builder' && .venv/bin/python3 setup.py py2app --dist-dir 'App Build'

# Build app and install directly to /Applications
cd '/Volumes/JUAN_FINISHING SSD/Finishing Tool Builder' && bash build_and_install.sh

# Build installer .app (uses installer.spec for version info)
cd '/Volumes/JUAN_FINISHING SSD/Finishing Tool Builder' && pyinstaller installer.spec
```

---

## Releasing minor updates (main.py only)
1. Edit `main.py`
2. Push to GitHub
3. Bump version in `version.json` (e.g. "1.0" -> "1.0.1")
4. Push `version.json` to GitHub
5. Users see green UPDATE NOW banner on next launch -> downloads new `main.py` + `version.json` -> restarts automatically

---

## Releasing major updates (full rebuild)
1. Edit `main.py`
2. Bump version in `version.json` and in `setup.py` (CFBundleVersion)
3. Run `build_and_install.sh` to rebuild the .app
4. Zip: `cd 'App Build' && zip -r "Finishing Tool.zip" "Finishing Tool.app"`
5. Upload `Finishing Tool.zip` to GitHub Releases (replace old zip)
6. Push updated files to GitHub repo
7. Bump installer version in `installer.spec` (both `version=` and `CFBundleShortVersionString`)
8. Rebuild installer: `pyinstaller installer.spec`
9. Share new `dist/Finishing Tool Installer.app`

---

## What a fresh install sets up

`build_and_install.sh` (and the standalone `installer.py`/`install.command` alternates) handle everything needed for both workflows to run on a new Mac:

- Homebrew + Tesseract OCR (VFX Export's episode-scanning)
- A Python virtual environment with `opencv-python`, `pytesseract`, `pillow`, `openpyxl`, `xlsxwriter`, `numpy`, `pymiere`, `requests`
- DaVinci Resolve render presets, copied into Resolve's own Presets/Render folder
- AME export presets (`LIVE.epr`, `MARKETING.epr`, `SOCIAL MEDIA.epr`, `LIVE WITH SRTs.epr`), both bundled into the built app and copied into AME's own Presets folder
- The **Pymiere Link** CEP extension, installed into Premiere Pro via Adobe's own `ExManCmd` tool — this is what lets `pymiere` (and therefore the Episode Export tab) actually talk to Premiere. Skipped automatically on a machine with no Premiere Pro installed.

### Premiere Pro / Episode Export requirements
Episode Export needs two things beyond the pip package:
1. **`pymiere_link.zxp`** installed into Premiere as a CEP extension — verify by opening Premiere and checking `Window > Extensions` for "Pymiere Link". Installed automatically by the build script when Premiere is detected; if it's ever missing, reinstall with:
   ```bash
   # from a folder containing pymiere_link.zxp
   curl -fsSL https://download.macromedia.com/pub/extensionmanager/ExManCmd_mac.dmg -o /tmp/ExManCmd.dmg
   hdiutil attach /tmp/ExManCmd.dmg -mountpoint /tmp/ExManCmdMount -nobrowse
   /tmp/ExManCmdMount/Contents/MacOS/ExManCmd --install pymiere_link.zxp
   hdiutil detach /tmp/ExManCmdMount
   ```
2. **Premiere Pro must be running** with a project open before clicking Connect to Premiere in the app — `pymiere` can't launch Premiere headlessly.

If Connect to Premiere fails with a Python import error, `pymiere` itself isn't installed — `pip install pymiere` (already handled by the build script, only relevant if running `main.py` directly outside a built app).

---

## Clip colors (DaVinci Resolve)
| Color | Behavior |
|-------|----------|
| Orange | Exported as VFX plate. Stacked orange clips get _V1, _V2, _V3 suffixes |
| Apricot | Exported with _CLEAN suffix |
| Chocolate | Reused plate — skipped during export |

---

## VFX Export — timeline naming convention
Recommended format for auto-detection:
```
SHOWCODE_ACRONYM_STRINGOUT_VFX_EP##-##_YYMMDD_COLOR
```
Example: `V-LA30_TKWNM_STRINGOUT_VFX_EP01-57_260619_COLOR`

Resulting plate filename: `V-LA30_TKWNM_VFX_EP01_260619_01`

If your timeline does not match, use Manual mode in the app.

### Output folder auto-detection
```
/Volumes/SHOWCODE_*/SHOWCODE_*_EDIT/
  -> folder with "TURNOVER" in name
    -> folder with "TO VFX" in name
```
If not detected, use Manual mode and browse to your TO VFX folder.

### Render preset
`02_COLORED VFX 4444 XQ`

---

## Episode Export — naming conventions
| Purpose | Format | Example |
|---------|--------|---------|
| Reel timeline (STRINGOUT) | `SHOWCODE_ACRONYM_STRINGOUT_EP##-##_DATE` | `V-LA30_TKWNM_STRINGOUT_EP01-15_260619` |
| Title card clip | `EPISODE ##` (or a Motion Graphics Template, which Premiere reports as `Graphic`) | `EPISODE 03` |
| Reference/pic-lock clip | `SHOWCODE_ACRONYM_PIC LOCK_EP##-##_DATE_REF.mov` | `V-LA30_TKWNM_PIC LOCK_EP01-15_260619_REF.mov` |

Full walkthrough (track selection, Mute Master Clips, overwrite handling, AME export styles) lives in the app's own Setup Guide — `Help > Setup Guide`, Episode Export tab.

### Output folder auto-detection
```
/Volumes/SHOWCODE_*/SHOWCODE_*_EDIT/
  -> folder with "DELIVERY" in name
    -> folder with "FINAL" in name
      -> folder with "LIVE" in name
```
MARKETING/TRAILER/SRT folders auto-detect as siblings of LIVE/FINAL. If not detected, use Manual mode per output type.

---

## Auto-update URLs
```
VERSION_URL  = https://raw.githubusercontent.com/esandijp-dotcom/finishing-tool/main/version.json
DOWNLOAD_URL = https://raw.githubusercontent.com/esandijp-dotcom/finishing-tool/main/main.py
```
