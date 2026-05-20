"""Phase 0 raw screencap header probe.

Captures one raw screencap via `adb exec-out screencap`, dumps the first
16 bytes in hex, and decodes them under the documented Android 9+ layout:

    uint32 width
    uint32 height
    uint32 pixel_format
    uint32 colorspace

Writes the report to bench/artifacts/raw_header.txt and also prints to
stdout. The full raw buffer is written to bench/artifacts/raw_capture.bin
so the bytes can be re-inspected without the device.

Exits non-zero if the header does not round-trip into a sensible
(W*H*4 + header_size) length, and ESCALATEs the finding to stderr.
"""
from __future__ import annotations

import struct
import subprocess
import sys
from pathlib import Path

from bench._common import ARTIFACTS_DIR, ensure_dirs, verify_device_or_die

PIXEL_FORMATS = {
    1: "PIXEL_FORMAT_RGBA_8888",
    2: "PIXEL_FORMAT_RGBX_8888",
    3: "PIXEL_FORMAT_RGB_888",
    4: "PIXEL_FORMAT_RGB_565",
    5: "PIXEL_FORMAT_BGRA_8888",  # deprecated on modern Android
}

DATASPACES = {
    0: "DATASPACE_UNKNOWN",
    142671872: "DATASPACE_V0_SRGB",
    143130624: "DATASPACE_V0_BT709",
    143261696: "DATASPACE_V0_SRGB_LINEAR",
    410868736: "DATASPACE_DISPLAY_P3",
    143982592: "DATASPACE_V0_BT601_625",
    151715840: "DATASPACE_V0_JFIF",
}


def main() -> int:
    ensure_dirs()
    verify_device_or_die()
    proc = subprocess.run(
        ["adb", "exec-out", "screencap"],
        check=True, capture_output=True, timeout=30,
    )
    buf = proc.stdout
    if len(buf) < 16:
        print(f"ERROR: raw screencap returned only {len(buf)} bytes", file=sys.stderr)
        return 2

    bin_path = ARTIFACTS_DIR / "raw_capture.bin"
    bin_path.write_bytes(buf)

    header16 = buf[:16]
    hex16 = " ".join(f"{b:02x}" for b in header16)
    w, h, fmt, cs = struct.unpack_from("<IIII", buf, 0)
    expected_16 = 16 + w * h * 4
    expected_12 = 12 + w * h * 4

    lines = []
    lines.append("Phase 0 raw screencap header probe")
    lines.append("==================================")
    lines.append("")
    lines.append(f"buffer_size_bytes : {len(buf)}")
    lines.append(f"first_16_bytes_hex: {hex16}")
    lines.append("")
    lines.append("Decoded under documented Android 9+ layout (LE uint32):")
    lines.append(f"  [0..3]   width        = {w}")
    lines.append(f"  [4..7]   height       = {h}")
    lines.append(f"  [8..11]  pixel_format = {fmt}  ({PIXEL_FORMATS.get(fmt, 'UNKNOWN')})")
    lines.append(f"  [12..15] colorspace   = {cs}  ({DATASPACES.get(cs, 'UNKNOWN')})")
    lines.append("")
    lines.append(f"expected len with 16-byte header (RGBA_8888): {expected_16}")
    lines.append(f"expected len with 12-byte header (pre-A9):    {expected_12}")
    if len(buf) == expected_16 and fmt == 1:
        lines.append("CONCLUSION: 16-byte header (Android 9+), RGBA_8888 — matches documented layout.")
        verdict = 0
    elif len(buf) == expected_12 and fmt == 1:
        # Re-decode under 12-byte layout to confirm
        w12, h12, fmt12 = struct.unpack_from("<III", buf, 0)
        lines.append(f"  reinterpret 12-byte header: w={w12} h={h12} fmt={fmt12}")
        lines.append("CONCLUSION: 12-byte header, RGBA_8888 — pre-Android-9 layout.")
        verdict = 0
    else:
        lines.append("ESCALATE: header does not round-trip under either documented layout.")
        lines.append("          do NOT proceed to Phase 1 without revising the parser.")
        verdict = 5

    report_path = ARTIFACTS_DIR / "raw_header.txt"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print()
    print(f"wrote {report_path}")
    print(f"wrote {bin_path} ({len(buf)} bytes)")
    return verdict


if __name__ == "__main__":
    raise SystemExit(main())
