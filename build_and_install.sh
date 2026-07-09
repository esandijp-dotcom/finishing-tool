#!/bin/bash
# Finishing Tool - Build & Install

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="Finishing Tool"
APP_PATH="/Applications/${APP_NAME}.app"

# ── FORCE PYTHON 3.13 ─────────────────────────────────────────────────────────
PYTHON="/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"
if [ ! -f "$PYTHON" ]; then
    PYTHON=$(which python3)
fi

echo ""
echo "Using Python: $($PYTHON --version)"
echo ""

echo "╔══════════════════════════════════════════════╗"
echo "║        Finishing Tool — Build & Install      ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# ── Homebrew ──────────────────────────────────────────────────────────────────
if ! command -v brew &> /dev/null; then
    echo "→ Installing Homebrew..."
    NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    eval "$(/opt/homebrew/bin/brew shellenv)"
    echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
else
    echo "✓ Homebrew"
fi

export PATH="/opt/homebrew/bin:$PATH"

# ── Tesseract ─────────────────────────────────────────────────────────────────
if ! command -v tesseract &> /dev/null; then
    echo "→ Installing Tesseract OCR..."
    brew install tesseract
else
    echo "✓ Tesseract OCR"
fi

# ── Virtual environment ───────────────────────────────────────────────────────
VENV_DIR="$SCRIPT_DIR/.venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "→ Creating virtual environment..."
    $PYTHON -m venv "$VENV_DIR"
else
    echo "✓ Virtual environment (existing)"
fi

echo "→ Installing Python packages..."
"$VENV_DIR/bin/python3" -m pip install --quiet --upgrade pip
"$VENV_DIR/bin/python3" -m pip install --quiet \
    opencv-python \
    pytesseract \
    pillow \
    openpyxl \
    xlsxwriter \
    numpy \
    pymiere \
    requests \
    py2app
echo "✓ Python packages installed"

# ── DaVinci Resolve scripting check (VFX Export tab) ─────────────────────────
RESOLVE_MODULES="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules"
if [ -d "$RESOLVE_MODULES" ]; then
    echo "✓ DaVinci Resolve Studio scripting API found"
else
    echo "⚠️  DaVinci Resolve scripting modules not found — make sure Resolve Studio is installed."
fi

# ── Pymiere Link extension (Episode Export tab needs this to reach Premiere) ─
# pymiere (installed above) talks to Premiere Pro through a companion CEP
# panel called "Pymiere Link" — a separate .zxp extension, not a pip
# package, that has to be installed into Premiere itself. Installed here via
# Adobe's own ExManCmd tool, the same way pymiere's own installer does it.
# Non-fatal if Premiere isn't on this machine at all — the VFX/DaVinci half
# of the app still works fine without it.
echo "→ Checking for Adobe Premiere Pro..."
if system_profiler -detailLevel mini SPApplicationsDataType 2>/dev/null | grep -qi "Premiere Pro"; then
    echo "✓ Premiere Pro found — installing Pymiere Link extension..."
    ZXP_PATH="$SCRIPT_DIR/pymiere_link.zxp"
    if [ ! -f "$ZXP_PATH" ]; then
        echo "⚠️  pymiere_link.zxp not found next to this script — skipping "
        echo "    (should have been downloaded alongside the other source files)."
    else
        TMPDIR_CEP=$(mktemp -d)
        EXMAN_DMG="$TMPDIR_CEP/ExManCmd_mac.dmg"
        echo "  Downloading Adobe Extension Manager Command Line tool..."
        if curl -fsSL "https://download.macromedia.com/pub/extensionmanager/ExManCmd_mac.dmg" -o "$EXMAN_DMG"; then
            MOUNT_POINT="$TMPDIR_CEP/ExManCmdMount"
            if hdiutil attach "$EXMAN_DMG" -mountpoint "$MOUNT_POINT" -nobrowse -quiet; then
                EXMANCMD="$MOUNT_POINT/Contents/MacOS/ExManCmd"
                if [ -f "$EXMANCMD" ]; then
                    "$EXMANCMD" --install "$ZXP_PATH" && echo "✓ Pymiere Link installed" \
                        || echo "⚠️  Pymiere Link install reported an error (may already be installed)"
                else
                    echo "⚠️  ExManCmd binary not found in mounted DMG — skipping"
                fi
                hdiutil detach "$MOUNT_POINT" -quiet || true
            else
                echo "⚠️  Could not mount ExManCmd DMG — skipping Pymiere Link install"
            fi
        else
            echo "⚠️  Could not download ExManCmd — skipping Pymiere Link install"
        fi
        rm -rf "$TMPDIR_CEP"

        # Unsigned/third-party CEP extensions (like Pymiere Link) need debug
        # mode enabled per Premiere version to actually load — covers the
        # CSXS versions used by Premiere 2019 through the current release.
        for CSXS_VER in 6 7 8 9 10 11 12; do
            defaults write "com.adobe.CSXS.${CSXS_VER}" PlayerDebugMode 1 2>/dev/null || true
        done
        echo "✓ Enabled unsigned-extension debug mode for CEP"
    fi
else
    echo "  Premiere Pro not found on this machine — skipping Pymiere Link "
    echo "  (Episode Export tab needs this; VFX/DaVinci tab does not)."
fi

# ── Build app icon ────────────────────────────────────────────────────────────
echo "→ Building app icon..."
cd "$SCRIPT_DIR"
if [ -f "build_icon.py" ]; then
    "$VENV_DIR/bin/python3" build_icon.py && echo "✓ Icon built" || echo "⚠️  Icon skipped (non-fatal)"
elif [ -f "icon.png" ]; then
    ICONSET="$SCRIPT_DIR/icon.iconset"
    mkdir -p "$ICONSET"
    for SIZE in 16 32 64 128 256 512; do
        sips -z $SIZE $SIZE icon.png --out "$ICONSET/icon_${SIZE}x${SIZE}.png" &>/dev/null
    done
    iconutil -c icns "$ICONSET" -o "$SCRIPT_DIR/icon.icns" &>/dev/null
    rm -rf "$ICONSET"
    echo "✓ Icon built from icon.png"
else
    echo "⚠️  No icon found (non-fatal)"
fi

# ── Build .app with py2app ───────────────────────────────────────────────────
cd "$SCRIPT_DIR"
echo "→ Building .app bundle..."
rm -rf build dist
"$VENV_DIR/bin/python3" setup.py py2app 2>&1
if [ ! -d "dist/${APP_NAME}.app" ]; then
    echo ""
    echo "✗ Build failed — dist/${APP_NAME}.app not found."
    exit 1
fi
echo "✓ App built"

# ── Install to /Applications ──────────────────────────────────────────────────
echo "→ Installing to /Applications..."
rm -rf "$APP_PATH"
cp -R "dist/${APP_NAME}.app" "/Applications/"
echo "✓ Installed to /Applications/${APP_NAME}.app"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║              All done!                       ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "✓ Finishing Tool installed successfully."
