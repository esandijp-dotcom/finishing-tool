#!/bin/bash
set -e

# ── Finishing Tool Installer ─────────────────────────────────────────────────
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export TK_SILENCE_DEPRECATION=1

GITHUB="https://raw.githubusercontent.com/esandijp-dotcom/finishing-tool/main"
APP_DIR="$HOME/Applications/FinishingTool"
APP_BUNDLE="$HOME/Applications/Finishing Tool.app"

echo "================================================"
echo "  Finishing Tool Installer"
echo "================================================"
echo ""

# ── Python ───────────────────────────────────────────────────────────────────
echo "→ Checking Python..."
PYTHON=$(which python3)
PY_VER=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "  Python $PY_VER ✓"

# ── Homebrew ─────────────────────────────────────────────────────────────────
echo "→ Checking Homebrew..."
if ! command -v brew &>/dev/null; then
    echo "  Installing Homebrew (you may be asked for your password)..."
    NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    eval "$(/opt/homebrew/bin/brew shellenv)" 2>/dev/null || true
fi
echo "  Homebrew ✓"

# ── Tesseract ────────────────────────────────────────────────────────────────
echo "→ Checking Tesseract..."
if ! command -v tesseract &>/dev/null; then
    echo "  Installing Tesseract..."
    brew install tesseract
fi
echo "  Tesseract ✓"

# ── pip packages ─────────────────────────────────────────────────────────────
echo "→ Installing Python packages..."
$PYTHON -m pip install pillow opencv-python pytesseract openpyxl xlsxwriter numpy pymiere requests --break-system-packages -q
echo "  Packages ✓"

# ── Pymiere Link extension (Episode Export tab needs this to reach Premiere) ─
echo "→ Checking for Adobe Premiere Pro..."
if system_profiler -detailLevel mini SPApplicationsDataType 2>/dev/null | grep -qi "Premiere Pro"; then
    ZXP_PATH="$APP_DIR/pymiere_link.zxp"
    curl -fsSL --insecure "$GITHUB/pymiere_link.zxp" -o "$ZXP_PATH" 2>/dev/null
    if [ -f "$ZXP_PATH" ]; then
        TMPDIR_CEP=$(mktemp -d)
        EXMAN_DMG="$TMPDIR_CEP/ExManCmd_mac.dmg"
        if curl -fsSL "https://download.macromedia.com/pub/extensionmanager/ExManCmd_mac.dmg" -o "$EXMAN_DMG"; then
            MOUNT_POINT="$TMPDIR_CEP/ExManCmdMount"
            if hdiutil attach "$EXMAN_DMG" -mountpoint "$MOUNT_POINT" -nobrowse -quiet; then
                "$MOUNT_POINT/Contents/MacOS/ExManCmd" --install "$ZXP_PATH" \
                    && echo "  Pymiere Link ✓" || echo "  ⚠️  Pymiere Link install reported an error"
                hdiutil detach "$MOUNT_POINT" -quiet || true
            fi
        fi
        rm -rf "$TMPDIR_CEP"
        for CSXS_VER in 6 7 8 9 10 11 12; do
            defaults write "com.adobe.CSXS.${CSXS_VER}" PlayerDebugMode 1 2>/dev/null || true
        done
    else
        echo "  ⚠️  Could not download pymiere_link.zxp — skipping"
    fi
else
    echo "  Premiere Pro not found — skipping Pymiere Link"
fi

# ── Download app files ───────────────────────────────────────────────────────
echo "→ Downloading Finishing Tool..."
mkdir -p "$APP_DIR"
curl -fsSL --insecure "$GITHUB/main.py"      -o "$APP_DIR/main.py"
curl -fsSL --insecure "$GITHUB/thinking.gif" -o "$APP_DIR/thinking.gif"
curl -fsSL --insecure "$GITHUB/icon.png"     -o "$APP_DIR/icon.png"
echo "  Files downloaded ✓"

# ── Build .app bundle ────────────────────────────────────────────────────────
echo "→ Building app..."
CONTENTS="$APP_BUNDLE/Contents"
MACOS="$CONTENTS/MacOS"
RES="$CONTENTS/Resources"
mkdir -p "$MACOS" "$RES"

cat > "$MACOS/FinishingTool" << LAUNCHER
#!/bin/bash
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:\$PATH"
export TK_SILENCE_DEPRECATION=1
cd "$APP_DIR"
exec "$PYTHON" "$APP_DIR/main.py"
LAUNCHER
chmod +x "$MACOS/FinishingTool"

cat > "$CONTENTS/Info.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>Finishing Tool</string>
<key>CFBundleDisplayName</key><string>Finishing Tool</string>
<key>CFBundleIdentifier</key><string>com.finishingtool.app</string>
<key>CFBundleVersion</key><string>1.0</string>
<key>CFBundleExecutable</key><string>FinishingTool</string>
<key>CFBundleIconFile</key><string>icon</string>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST

# Convert icon
if command -v sips &>/dev/null && command -v iconutil &>/dev/null; then
    ICONSET="$RES/icon.iconset"
    mkdir -p "$ICONSET"
    for SIZE in 16 32 64 128 256 512; do
        sips -z $SIZE $SIZE "$APP_DIR/icon.png" --out "$ICONSET/icon_${SIZE}x${SIZE}.png" &>/dev/null
    done
    iconutil -c icns "$ICONSET" -o "$RES/icon.icns" &>/dev/null
    rm -rf "$ICONSET"
fi

echo "  App bundle created ✓"
echo ""
echo "================================================"
echo "  Installation complete!"
echo "  Finishing Tool is in ~/Applications"
echo "================================================"
echo ""

# Launch it
open "$APP_BUNDLE"
