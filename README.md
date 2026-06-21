# Finishing Tool v1.0
VFX Plate Exporter for DaVinci Resolve — by Juan Esandi

---

## What it does
Connects to DaVinci Resolve Studio via its API and exports VFX plates for post-production turnovers. Automatically detects show info from the timeline name, scans episodes via OCR on the reference video, exports clips track by track, takes screenshots, and generates a .xlsx List to Post.

---

## Files

### In this folder (local build):
`main.py`, `installer.py`, `installer.spec`, `setup.py`, `build_icon.py`, `icon.png`, `thinking.gif`, `version.json`

### On GitHub (github.com/esandijp-dotcom/finishing-tool):
`main.py`, `installer.py`, `thinking.gif`, `icon.png`, `version.json`, `build_and_install.sh`, `setup.py`, `build_icon.py`

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

## Clip colors (DaVinci Resolve)
| Color | Behavior |
|-------|----------|
| Orange | Exported as VFX plate. Stacked orange clips get _V1, _V2, _V3 suffixes |
| Apricot | Exported with _CLEAN suffix |
| Chocolate | Reused plate — skipped during export |

---

## Timeline naming convention
Recommended format for auto-detection:
```
SHOWCODE_ACRONYM_STRINGOUT_VFX_EP##-##_YYMMDD_COLOR
```
Example: `V-LA30_TKWNM_STRINGOUT_VFX_EP01-57_260619_COLOR`

Resulting plate filename: `V-LA30_TKWNM_VFX_EP01_260619_01`

If your timeline does not match, use Manual mode in the app.

---

## Output folder auto-detection
```
/Volumes/SHOWCODE_*/SHOWCODE_*_EDIT/
  -> folder with "TURNOVER" in name
    -> folder with "TO VFX" in name
```
If not detected, use Manual mode and browse to your TO VFX folder.

---

## Render preset
`02_COLORED VFX 4444 XQ`

---

## Auto-update URLs
```
VERSION_URL  = https://raw.githubusercontent.com/esandijp-dotcom/finishing-tool/main/version.json
DOWNLOAD_URL = https://raw.githubusercontent.com/esandijp-dotcom/finishing-tool/main/main.py
```
