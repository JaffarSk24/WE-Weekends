"""Fails if any tracked text file contains invisible or formatting characters
(zero-width marks, BOM, no-break spaces, variation selectors, tag characters,
bidi controls). Run from the repository root."""
import subprocess
import sys

FORBIDDEN = set(range(0x200B, 0x2010)) | set(range(0x202A, 0x202F)) | {0x202F, 0x2028, 0x2029, 0x2060, 0x2061, 0x2062, 0x2063, 0x2064, 0xFEFF, 0x00A0}
FORBIDDEN |= set(range(0xFE00, 0xFE10)) | set(range(0xE0000, 0xE0080))

files = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout.splitlines()
bad = 0
for name in files:
    try:
        text = open(name, encoding="utf-8").read()
    except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
        continue
    for number, line in enumerate(text.splitlines(), 1):
        for ch in line:
            if ord(ch) in FORBIDDEN:
                print(f"{name}:{number}: U+{ord(ch):04X}")
                bad += 1
sys.exit(1 if bad else 0)
