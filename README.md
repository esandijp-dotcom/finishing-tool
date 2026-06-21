# Finishing Tool v1.0

## Build the installer .app (run once on your Mac)
  cd '/Users/juanesandi/Downloads/Finishing Tool v1.0 - GitHub Files'
  pyinstaller --onefile --windowed --name "Finishing Tool Installer" --icon icon.png --add-data "main.py:." --add-data "thinking.gif:." --add-data "icon.png:." installer.py

App will be at: dist/Finishing Tool Installer.app

## Upload to GitHub (github.com/esandijp-dotcom/finishing-tool)
main.py, installer.py, thinking.gif, icon.png, version.json

## Releasing updates
1. Push new main.py to GitHub
2. Bump version in version.json
3. Users see green update banner on next launch
