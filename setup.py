from setuptools import setup

APP = ['main.py']
DATA_FILES = [
    ('', ['thinking.gif', 'icon.png', 'version.json',
          'LIVE.epr', 'MARKETING.epr', 'SOCIAL MEDIA.epr', 'LIVE WITH SRTs.epr']),
]
OPTIONS = {
    'argv_emulation': False,
    'iconfile': 'icon.icns',
    'frameworks': [
        '/Library/Frameworks/Python.framework/Versions/3.13/Frameworks/Tcl.framework',
        '/Library/Frameworks/Python.framework/Versions/3.13/Frameworks/Tk.framework',
    ],
    'plist': {
        'CFBundleName': 'Finishing Tool',
        'CFBundleDisplayName': 'Finishing Tool',
        'CFBundleIdentifier': 'com.finishingtool.app',
        'CFBundleVersion': '1.0',
        'CFBundleShortVersionString': '1.0',
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '12.0',
    },
    'packages': [
        'PIL', 'cv2', 'pytesseract', 'openpyxl', 'xlsxwriter', 'numpy',
        'tkinter', 'urllib', 'threading', 'json', 'pymiere', 'requests',
    ],
    'includes': [
        'PIL', 'cv2', 'pytesseract', 'openpyxl', 'xlsxwriter', 'numpy', 'tkinter',
        'pymiere', 'requests',
    ],
    'excludes': ['matplotlib', 'scipy', 'PyQt5', 'PyQt6'],
}

setup(
    app=APP,
    name='Finishing Tool',
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
