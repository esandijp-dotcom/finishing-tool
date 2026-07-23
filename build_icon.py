#!/usr/bin/env python3
"""
Generates icon.icns from icon.png for Finishing Tool.
"""
import os
import subprocess
import tempfile
import shutil

def build_icns():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    png_path   = os.path.join(script_dir, "icon.png")
    icns_path  = os.path.join(script_dir, "icon.icns")

    if not os.path.exists(png_path):
        print(f"✗ icon.png not found at {png_path}")
        return False

    tmpdir      = tempfile.mkdtemp()
    iconset_dir = os.path.join(tmpdir, "icon.iconset")
    os.makedirs(iconset_dir)

    sizes = [16, 32, 64, 128, 256, 512]
    for size in sizes:
        out = os.path.join(iconset_dir, f"icon_{size}x{size}.png")
        subprocess.run(["sips", "-z", str(size), str(size), png_path, "--out", out],
                       capture_output=True)
        out2x = os.path.join(iconset_dir, f"icon_{size}x{size}@2x.png")
        subprocess.run(["sips", "-z", str(size*2), str(size*2), png_path, "--out", out2x],
                       capture_output=True)

    result = subprocess.run(["iconutil", "-c", "icns", iconset_dir, "-o", icns_path],
                            capture_output=True)
    shutil.rmtree(tmpdir, ignore_errors=True)

    if result.returncode != 0:
        print(f"✗ iconutil failed: {result.stderr.decode()}")
        return False

    print(f"✓ Icon created: {icns_path}")
    return True

if __name__ == "__main__":
    build_icns()
