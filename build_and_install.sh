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
PACKAGES="opencv-python pytesseract pillow openpyxl xlsxwriter numpy py2app"

if [ ! -d "$VENV_DIR" ]; then
    echo "→ Creating virtual environment..."
    $PYTHON -m venv "$VENV_DIR"
    echo "→ Installing Python packages..."
    "$VENV_DIR/bin/python3" -m pip install --quiet --upgrade pip
    "$VENV_DIR/bin/python3" -m pip install --quiet $PACKAGES
    echo "✓ Python packages installed"
else
    echo "✓ Virtual environment (existing)"
    # Only install missing packages
    MISSING=$("$VENV_DIR/bin/python3" -c "
import importlib, sys
pkgs = {'opencv-python':'cv2','pytesseract':'pytesseract','pillow':'PIL',
        'openpyxl':'openpyxl','xlsxwriter':'xlsxwriter','numpy':'numpy','py2app':'py2app'}
missing = [k for k,v in pkgs.items() if importlib.util.find_spec(v) is None]
print(' '.join(missing))
" 2>/dev/null)
    if [ -n "$MISSING" ]; then
        echo "→ Installing missing packages: $MISSING"
        "$VENV_DIR/bin/python3" -m pip install --quiet $MISSING
        echo "✓ Packages installed"
    else
        echo "✓ All packages present"
    fi
fi

# ── DaVinci Resolve scripting check ──────────────────────────────────────────
RESOLVE_MODULES="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules"
if [ -d "$RESOLVE_MODULES" ]; then
    echo "✓ DaVinci Resolve Studio scripting API found"
else
    echo "⚠️  DaVinci Resolve scripting modules not found — make sure Resolve Studio is installed."
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

# ── Build .app with py2app (skip if main.py unchanged) ───────────────────────
cd "$SCRIPT_DIR"
HASH_FILE="$SCRIPT_DIR/.last_build_hash"
CURRENT_HASH=$(md5 -q main.py 2>/dev/null || md5sum main.py | awk '{print $1}')
LAST_HASH=$(cat "$HASH_FILE" 2>/dev/null || echo "")

if [ "$CURRENT_HASH" = "$LAST_HASH" ] && [ -d "dist/${APP_NAME}.app" ]; then
    echo "✓ main.py unchanged — skipping rebuild (using existing build)"
else
    echo "→ Building .app bundle..."
    rm -rf build dist
    "$VENV_DIR/bin/python3" setup.py py2app 2>&1
    if [ ! -d "dist/${APP_NAME}.app" ]; then
        echo ""
        echo "✗ Build failed — dist/${APP_NAME}.app not found."
        exit 1
    fi
    echo "$CURRENT_HASH" > "$HASH_FILE"
    echo "✓ App built"
fi

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
