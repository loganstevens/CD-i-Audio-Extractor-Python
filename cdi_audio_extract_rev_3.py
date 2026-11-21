#!/usr/bin/env python3
"""
cdi_audio_extract.py

Extract audio from raw Philips CD-i disc images (.bin) by reading the
CD-ROM XA sub-header of every sector directly, bypassing the Green Book
filesystem entirely.

Handles:
  * stored sector sizes of 2448, 2352, 2340, 2336 and 2332 bytes
  * data that does not begin at byte 0 (lead-in or write-offset padding)
  * images with or without the 2-second (150-sector) pregap
  * scrambled images (ECMA-130 Annex B), descrambled on the fly

Requires only the Python standard library, plus ffmpeg on PATH for decoding.
"""

import argparse
import os
import shutil
import subprocess
import sys
from collections import Counter

SYNC = b"\x00" + b"\xff" * 10 + b"\x00"
RAW_SECTOR = 2352
SCRAMBLED_LEN = RAW_SECTOR - len(SYNC)   # 2340: everything after the sync
PREGAP_SECTORS = 150                     # 2 seconds at 75 sectors/second
PAYLOAD_SIZE = 2324                      # full Mode 2 Form 2 user data area
AUDIO_SUBMODE = 0x64                     # real-time + Form 2 + audio
SAMPLE_COUNT = 200                       # sectors sampled during detection
SYNC_SEARCH_BYTES = 8 * 1024 * 1024      # how far to look for the first sync

# Layout of each stored sector size.
#   sync:    each sector begins with the 12-byte sync pattern
#   header:  offset of the 4-byte MSF + mode header (None if not stored).
#            This is also where the scrambled region begins.
#   sub:     offset of the 8-byte sub-header (4 bytes + duplicate copy)
#   payload: offset of the user data
FORMATS = {
    2448: {"sync": True,  "header": 12,   "sub": 16, "payload": 24,
           "desc": "raw 2352-byte sector + 96 bytes of subchannel"},
    2352: {"sync": True,  "header": 12,   "sub": 16, "payload": 24,
           "desc": "raw sector"},
    2340: {"sync": False, "header": 0,    "sub": 4,  "payload": 12,
           "desc": "raw sector without the 12-byte sync"},
    2336: {"sync": False, "header": None, "sub": 0,  "payload": 8,
           "desc": "Mode 2 sub-header + user data (no sync or header)"},
    2332: {"sync": False, "header": None, "sub": 0,  "payload": 8,
           "desc": "as 2336 without the trailing 4-byte EDC (inferred layout)"},
}


# --------------------------------------------------------------------------
# ECMA-130 Annex B scrambler
# --------------------------------------------------------------------------

def build_scramble_table(length=SCRAMBLED_LEN):
    """15-bit LFSR, polynomial x^15 + x + 1, preset to 1. The output bytes
    are XORed over every byte of a raw sector after the 12-byte sync."""
    table = bytearray(length)
    reg = 0x0001
    for i in range(length):
        value = 0
        for bit in range(8):
            value |= (reg & 1) << bit
            feedback = (reg ^ (reg >> 1)) & 1
            reg = (reg >> 1) | (feedback << 14)
        table[i] = value
    return bytes(table)


SCRAMBLE_TABLE = build_scramble_table()
_SCRAMBLE_INT = int.from_bytes(SCRAMBLE_TABLE, "big")
_HEADER_KEY = int.from_bytes(SCRAMBLE_TABLE[:12], "big")


def descramble(region):
    """Descramble the 2340 bytes that follow the sync (XOR is its own inverse)."""
    return (int.from_bytes(region, "big") ^ _SCRAMBLE_INT).to_bytes(SCRAMBLED_LEN, "big")


def descramble_header(h12):
    """Descramble only header + sub-header (12 bytes) — cheap enough to do
    on every sector just to classify it."""
    return (int.from_bytes(h12, "big") ^ _HEADER_KEY).to_bytes(12, "big")


# --------------------------------------------------------------------------
# Header helpers
# --------------------------------------------------------------------------

def _bcd_ok(value, limit):
    return (value >> 4) <= 9 and (value & 0x0F) <= 9 and value < limit


def _bcd(value):
    return (value >> 4) * 10 + (value & 0x0F)


def header_is_valid(h):
    """h = 12 bytes: minute, second, frame, mode, sub-header, sub-header copy."""
    m, s, f, mode = h[0], h[1], h[2], h[3]
    if mode not in (1, 2):
        return False
    if not (_bcd_ok(m, 0xA0) and _bcd_ok(s, 0x60) and _bcd_ok(f, 0x75)):
        return False
    if mode == 2 and h[4:8] != h[8:12]:
        return False
    return True


def sample_indices(total, count=SAMPLE_COUNT):
    if total <= count:
        return list(range(total))
    return sorted({i * total // count for i in range(count)})


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

def detect_layout(path, file_size):
    """Return (sector_size, start_offset) or (None, None).

    Formats with a sync pattern are found by locating the first sync and
    confirming it repeats at the sector stride, which also skips any leading
    padding. The sync is never scrambled, so this works on scrambled images.
    Sync-less formats are scored by how often the duplicated sub-header
    copies agree at that stride."""
    with open(path, "rb") as f:
        head = f.read(SYNC_SEARCH_BYTES)

    pos = head.find(SYNC)
    tries = 0
    while pos != -1 and tries < 10000:
        for size in (2352, 2448):
            if all(head[pos + size * k: pos + size * k + 12] == SYNC for k in range(1, 4)):
                return size, pos
        tries += 1
        pos = head.find(SYNC, pos + 1)

    best_size, best_score = None, 0.0
    with open(path, "rb") as f:
        for size in (2340, 2336, 2332):
            hdr = FORMATS[size]["header"]
            sub_off = FORMATS[size]["sub"]
            total = file_size // size
            hits = considered = 0
            for idx in sample_indices(total):
                f.seek(idx * size)
                sector = f.read(size)
                if hdr is not None:
                    # 2340 keeps the header, which may be scrambled: accept it
                    # if it is valid either as stored or after descrambling.
                    h = sector[hdr:hdr + 12]
                    if len(h) < 12:
                        continue
                    considered += 1
                    if header_is_valid(h) or header_is_valid(descramble_header(h)):
                        hits += 1
                    continue
                sub = sector[sub_off:sub_off + 8]
                if len(sub) < 8 or sub == bytes(8):
                    continue  # empty sectors match trivially; ignore them
                considered += 1
                if sub[:4] == sub[4:]:
                    hits += 1
            score = hits / considered if considered else 0.0
            if file_size % size == 0:
                score += 0.01  # tie-break toward an exact fit
            if score > best_score:
                best_size, best_score = size, score

    if best_score >= 0.6:
        return best_size, 0
    return None, None


def detect_scrambling(path, size, start, total):
    """Compare how many sampled headers look valid as stored versus after
    descrambling. Returns (is_scrambled, valid_as_stored, valid_descrambled)."""
    hdr = FORMATS[size]["header"]
    if hdr is None:
        return False, 0, 0
    as_stored = descrambled = 0
    with open(path, "rb") as f:
        for idx in sample_indices(total):
            f.seek(start + idx * size)
            h = f.read(size)[hdr:hdr + 12]
            if len(h) < 12:
                continue
            if header_is_valid(h):
                as_stored += 1
            if header_is_valid(descramble_header(h)):
                descrambled += 1
    return descrambled > as_stored, as_stored, descrambled


def describe_start(path, size, start, scrambled):
    """Use the first sector's absolute MSF address to tell whether the image
    includes the 2-second pregap (00:00:00) or begins at the program area
    (00:02:00). Purely informational: every sector is scanned regardless."""
    hdr = FORMATS[size]["header"]
    if hdr is None:
        return "unknown (this format stores no sector header)"
    with open(path, "rb") as f:
        f.seek(start)
        h = f.read(size)[hdr:hdr + 12]
    if scrambled:
        h = descramble_header(h)
    if len(h) < 12 or not header_is_valid(h):
        return "unknown (first sector header not readable)"
    frames = (_bcd(h[0]) * 60 + _bcd(h[1])) * 75 + _bcd(h[2])
    msf = f"{h[0]:02x}:{h[1]:02x}:{h[2]:02x}"
    if frames == 0:
        return f"yes — image starts at {msf}, so the {PREGAP_SECTORS}-sector pregap is included"
    if frames == PREGAP_SECTORS:
        return f"no — image starts at {msf}, the start of the program area"
    return f"image starts at {msf}"


# --------------------------------------------------------------------------
# Main extraction
# --------------------------------------------------------------------------

def print_progress(current, total, width=40):
    filled = int(width * current / total)
    bar = "█" * filled + "░" * (width - filled)
    print(f"\r  [{bar}] {current / total * 100:5.1f}%  ({current}/{total} sectors)",
          end="", flush=True)


def extract_cdi_audio(bin_path, descramble_mode="auto"):
    if not os.path.isfile(bin_path):
        print(f"ERROR: file not found: {bin_path}")
        sys.exit(1)

    file_size = os.path.getsize(bin_path)
    print(f"Detecting sector format for {bin_path}...")

    size, start = detect_layout(bin_path, file_size)
    if size is None:
        print("ERROR: Could not detect the sector format.")
        print("No sync pattern was found and no sync-less layout matched.")
        print("If you ran bchunk first, use the original .bin instead of the extracted .iso.")
        sys.exit(1)

    fmt = FORMATS[size]
    total = (file_size - start) // size
    hdr = fmt["header"]

    auto_scrambled, as_stored, descrambled_ok = detect_scrambling(bin_path, size, start, total)
    if hdr is None:
        scrambled = False
        scramble_note = "not applicable (no header stored in this format)"
    elif descramble_mode == "on":
        scrambled = True
        scramble_note = "forced on (--descramble on)"
    elif descramble_mode == "off":
        scrambled = False
        scramble_note = "forced off (--descramble off)"
    else:
        scrambled = auto_scrambled
        checked = len(sample_indices(total))
        scramble_note = (f"{'yes' if scrambled else 'no'} "
                         f"({as_stored}/{checked} sampled headers valid as stored, "
                         f"{descrambled_ok}/{checked} valid after descrambling)")

    print(f"  Sector size:      {size} bytes ({fmt['desc']})")
    print(f"  Data begins at:   byte {start}" + ("  (leading bytes skipped)" if start else ""))
    print(f"  Total sectors:    {total}")
    print(f"  Pregap included:  {describe_start(bin_path, size, start, scrambled)}")
    print(f"  Scrambled:        {scramble_note}")
    if scrambled:
        print("  Sectors will be descrambled on the fly (ECMA-130 Annex B).")

    base = os.path.splitext(bin_path)[0]
    output_dir = base + "_audio"
    os.makedirs(output_dir, exist_ok=True)

    submode_counts = Counter()
    channel_codings = {}
    channel_sectors = Counter()
    handles = {}
    bad_sync = 0
    payload_rel = fmt["payload"] - (hdr or 0)

    print("\nScanning sectors...\n")
    try:
        with open(bin_path, "rb") as f:
            f.seek(start)
            for n in range(1, total + 1):
                sector = f.read(size)
                if len(sector) < size:
                    break

                if fmt["sync"] and sector[:12] != SYNC:
                    bad_sync += 1
                else:
                    if hdr is not None:
                        h = sector[hdr:hdr + 12]
                        if scrambled:
                            h = descramble_header(h)
                        sub = h[4:8]
                    else:
                        sub = sector[fmt["sub"]:fmt["sub"] + 4]

                    submode = sub[2]
                    submode_counts[submode] += 1

                    if submode == AUDIO_SUBMODE:
                        if scrambled:
                            body = descramble(sector[hdr:hdr + SCRAMBLED_LEN])
                            payload = body[payload_rel:payload_rel + PAYLOAD_SIZE]
                        else:
                            payload = sector[fmt["payload"]:fmt["payload"] + PAYLOAD_SIZE]

                        key = (sub[0], sub[1])
                        if key not in handles:
                            raw_path = os.path.join(output_dir, f"file{key[0]}_ch{key[1]}.raw")
                            handles[key] = open(raw_path, "wb")
                        handles[key].write(payload)
                        channel_codings[key] = sub[3]
                        channel_sectors[key] += 1

                if n % 1000 == 0 or n == total:
                    print_progress(n, total)
    finally:
        for handle in handles.values():
            handle.close()

    print()
    print("\nSubmode distribution: " +
          ", ".join(f"0x{k:02x}: {v}" for k, v in sorted(submode_counts.items())))
    if bad_sync:
        print(f"Sectors skipped (sync pattern missing): {bad_sync}")

    print(f"\nFound {len(handles)} audio channel(s):")
    for key in sorted(handles):
        size_mb = channel_sectors[key] * PAYLOAD_SIZE / (1024 * 1024)
        print(f"  File {key[0]}, Channel {key[1]} | coding=0x{channel_codings[key]:02x} | "
              f"{channel_sectors[key]} sectors | {size_mb:.1f} MB")

    if not handles:
        print("No audio sectors found.")
        if scrambled is False and auto_scrambled is False and hdr is not None:
            print("If you believe the image is scrambled, try --descramble on.")
        return

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("\nffmpeg not found on PATH — raw streams kept in the output folder.")
        return

    print("\nDecoding with ffmpeg...")
    for key in sorted(handles):
        name = f"file{key[0]}_ch{key[1]}"
        raw_path = os.path.join(output_dir, name + ".raw")
        wav_path = os.path.join(output_dir, name + ".wav")

        result = subprocess.run(
            [ffmpeg, "-y", "-v", "warning", "-i", raw_path, "-map", "0:a", wav_path],
            capture_output=True, text=True
        )
        warnings = [line for line in result.stderr.splitlines() if line.strip()]

        if result.returncode == 0 and os.path.exists(wav_path) and os.path.getsize(wav_path) > 0:
            note = f"  (ffmpeg reported {len(warnings)} warning(s))" if warnings else ""
            print(f"  ✓ {name}.wav{note}")
            os.remove(raw_path)
        else:
            print(f"  ✗ {name} — ffmpeg failed, keeping .raw")
            print(f"    {warnings[-1] if warnings else 'unknown error'}")

    print(f"\nDone! Output in: {output_dir}/")


def main():
    parser = argparse.ArgumentParser(
        description="Extract audio channels from a raw Philips CD-i disc image.")
    parser.add_argument("image", help="path to the raw .bin disc image")
    parser.add_argument("--descramble", choices=["auto", "on", "off"], default="auto",
                        help="descramble sectors: auto-detect (default), force on, or force off")
    args = parser.parse_args()
    extract_cdi_audio(args.image, args.descramble)


if __name__ == "__main__":
    main()
