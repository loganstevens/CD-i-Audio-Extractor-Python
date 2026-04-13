"""
Author: Logan Stevens (https://loganstevens.github.io)
Created: 2026-04-13
Description: Python script for extracting audio from raw Philips CD-i disc images.
GitHub: https://github.com/loganstevens
Version: 2.0 — Added auto-detection of sector size, pregap, and scrambling. Improved progress reporting and error handling.
Copyright (c) 2026 Logan Stevens.
Licensed under the MIT License.
"""

import os
import sys
import subprocess
from collections import defaultdict

SYNC = bytes([0x00,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0x00])
CANDIDATE_SIZES = [2448, 2352, 2340, 2336, 2332]

# Byte offset of sync pattern within a sector for each size
# Most formats have sync at offset 0, but some have a 4-byte timestamp prepended
SYNC_OFFSETS = {
    2448: 0,
    2352: 0,
    2340: 0,
    2336: 4,
    2332: 4,
}

# Byte offset of sub-header (file, channel, submode, coding) within a sector
SUBHEADER_OFFSETS = {
    2448: 16,
    2352: 16,
    2340: 16,
    2336: 16,
    2332: 16,
}

# Byte offset of audio payload within a sector
PAYLOAD_OFFSETS = {
    2448: 24,
    2352: 24,
    2340: 24,
    2336: 16,  # no sync/header — sub-header is at start
    2332: 16,
}

PAYLOAD_SIZE = 2304  # XA audio payload is always 2304 bytes regardless of sector size

def detect_sector_size(bin_path):
    """Detect sector size by finding the sync pattern stride."""
    with open(bin_path, "rb") as f:
        header = f.read(max(CANDIDATE_SIZES) * 4)

    for size in CANDIDATE_SIZES:
        sync_offset = SYNC_OFFSETS[size]
        # Check sync at sector 0, 1, and 2 to be sure
        found = all(
            header[sync_offset + (size * i) : sync_offset + (size * i) + 12] == SYNC
            for i in range(3)
            if sync_offset + (size * i) + 12 <= len(header)
        )
        if found:
            return size

    return None

def detect_pregap(bin_path, sector_size):
    """
    Check if the disc has a two-second pregap (150 sectors of silence/zeros
    before the program area). Returns True if pregap detected.
    """
    sync_offset = SYNC_OFFSETS[sector_size]
    subheader_offset = SUBHEADER_OFFSETS[sector_size]
    with open(bin_path, "rb") as f:
        # Read first 150 sectors
        data = f.read(sector_size * 150)

    audio_in_first_150 = 0
    for i in range(150):
        sector = data[i*sector_size:(i+1)*sector_size]
        if len(sector) < sector_size:
            break
        if sector[subheader_offset + 2] == 0x64:
            audio_in_first_150 += 1

    # If no audio in first 150 sectors, likely a pregap
    return audio_in_first_150 == 0

def detect_scrambling(bin_path, sector_size):
    """
    Scrambled sectors XOR the sync pattern — if we find the sync pattern
    intact we're unscrambled. If the sync is missing but file parsed OK
    via stride detection, it may be scrambled.
    This is a best-effort check only.
    """
    sync_offset = SYNC_OFFSETS[sector_size]
    with open(bin_path, "rb") as f:
        first = f.read(sector_size)
    sync_found = first[sync_offset:sync_offset+12] == SYNC
    # If we detected the sector size successfully, sync is intact = not scrambled
    return not sync_found

def print_progress(current, total, width=40):
    filled = int(width * current / total)
    bar = "█" * filled + "░" * (width - filled)
    percent = current / total * 100
    print(f"\r  [{bar}] {percent:5.1f}%  ({current}/{total} sectors)", end="", flush=True)

def extract_cdi_audio(bin_path):
    base = os.path.splitext(bin_path)[0]
    output_dir = base + "_audio"
    os.makedirs(output_dir, exist_ok=True)

    # --- Auto-detect format ---
    print(f"Detecting sector format for {bin_path}...")

    sector_size = detect_sector_size(bin_path)
    if sector_size is None:
        print("ERROR: Could not detect sector size. Sync pattern not found at any known stride.")
        print("The file may be scrambled, corrupted, or not a raw CD-i disc image.")
        sys.exit(1)

    has_pregap   = detect_pregap(bin_path, sector_size)
    scrambled    = detect_scrambling(bin_path, sector_size)

    sync_off     = SYNC_OFFSETS[sector_size]
    sub_off      = SUBHEADER_OFFSETS[sector_size]
    payload_off  = PAYLOAD_OFFSETS[sector_size]

    file_size     = os.path.getsize(bin_path)
    total_sectors = file_size // sector_size

    print(f"  Sector size:  {sector_size} bytes")
    print(f"  Total sectors: {total_sectors}")
    print(f"  Pregap detected: {has_pregap}")
    print(f"  Scrambled: {scrambled}")
    if scrambled:
        print("  WARNING: Scrambled images are not currently supported. Output may be incorrect.")

    # --- Scan sectors ---
    channels       = defaultdict(bytes)
    submode_counts = defaultdict(int)
    channel_codings = {}

    print(f"\nScanning sectors...\n")

    with open(bin_path, "rb") as f:
        sector_num = 0
        while True:
            sector = f.read(sector_size)
            if len(sector) < sector_size:
                break

            submode = sector[sub_off + 2]
            submode_counts[hex(submode)] += 1

            if submode == 0x64:
                file_num = sector[sub_off]
                channel  = sector[sub_off + 1]
                coding   = sector[sub_off + 3]
                key      = (file_num, channel)
                channels[key] += sector[payload_off:payload_off + PAYLOAD_SIZE]
                channel_codings[key] = coding

            sector_num += 1
            if sector_num % 1000 == 0 or sector_num == total_sectors:
                print_progress(sector_num, total_sectors)

    print()  # newline after bar

    print(f"\nTotal sectors scanned: {sector_num}")  # Addition
    print(f"\nSubmode distribution: {dict(submode_counts)}")
    print(f"\nFound {len(channels)} audio channel(s):")
    for (fn, ch), coding in sorted(channel_codings.items()):
        size_mb = len(channels[(fn, ch)]) / (1024*1024)
        print(f"  File {fn}, Channel {ch} | coding=0x{coding:02x} | {size_mb:.1f} MB")

    if not channels:
        print("No audio sectors found.")
        return

    # --- Extract and decode ---
    print("\nExtracting and decoding...")
    for (fn, ch) in sorted(channels.keys()):
        raw_path = os.path.join(output_dir, f"file{fn}_ch{ch}.raw")
        wav_path = os.path.join(output_dir, f"file{fn}_ch{ch}.wav")

        with open(raw_path, "wb") as f:
            f.write(channels[(fn, ch)])

        result = subprocess.run(
            ["ffmpeg", "-y", "-i", raw_path, "-map", "0:a", wav_path],
            capture_output=True, text=True
        )

        if os.path.exists(wav_path) and os.path.getsize(wav_path) > 0:
            print(f"  SUCCESS: file{fn}_ch{ch}.wav")
            os.remove(raw_path)
        else:
            print(f"  FAILED: file{fn}_ch{ch} — ffmpeg failed, keeping .raw")
            print(f"    {result.stderr.splitlines()[-1] if result.stderr else 'unknown error'}")

    print(f"\nDone! Output in: {output_dir}/")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 cdi_audio_extract.py \"game.bin\"")
        sys.exit(1)
    extract_cdi_audio(sys.argv[1])