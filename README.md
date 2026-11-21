# cdi_audio_extract.py

A Python script for extracting audio from raw Philips CD-i disc images (`.bin` files). It auto-detects the sector format, descrambles scrambled images, scans every sector's CD-ROM XA sub-header for audio, and decodes each audio channel to `.wav` with `ffmpeg`.

---

## Background

Philips CD-i games store audio as **MPEG program streams** (MP2 audio) interleaved within raw CD sectors. Every sector carries a sub-header identifying its type (audio, video, or data) and which logical channel it belongs to. Standard ISO tools (`isoinfo`, `hdiutil`, 7-Zip) cannot read CD-i images because CD-i uses the **Green Book filesystem**, not ISO 9660.

This script bypasses the filesystem entirely: it reads the raw sector stream, classifies sectors by sub-header, and reassembles each audio channel separately.

---

## Requirements

- Python 3.6+
- [`ffmpeg`](https://ffmpeg.org/) on your `PATH` (if missing, the raw streams are kept instead of decoded)
- No third-party Python packages

**macOS:** `brew install ffmpeg`  
**Linux (Debian/Ubuntu):** `sudo apt install ffmpeg`  
**Windows:** download from [ffmpeg.org](https://ffmpeg.org/download.html) and add its `bin/` folder to your `PATH`.

---

## Usage

```bash
python3 cdi_audio_extract.py "game.bin"
```

Output goes to a folder named after the input file:

```
game_audio/
  file1_ch0.wav
  file1_ch1.wav
```

### Options

| Option | Effect |
|--------|--------|
| `--descramble auto` | Detect scrambling from sector headers (default) |
| `--descramble on` | Always descramble |
| `--descramble off` | Never descramble |

Always run the script on the **original `.bin`**. Tools like `bchunk` strip the sector headers this script depends on.

---

## Supported Formats

| Stored sector size | Layout | Detected by | Scrambling |
|---|---|---|---|
| 2448 bytes | Raw 2352-byte sector + 96 bytes of subchannel | Sync pattern stride | Supported |
| 2352 bytes | Raw sector (most common) | Sync pattern stride | Supported |
| 2340 bytes | Raw sector without the 12-byte sync | Header validity at stride | Supported |
| 2336 bytes | Mode 2 sub-header + user data | Sub-header copy agreement | Not applicable |
| 2332 bytes | As 2336, without the trailing 4-byte EDC (inferred) | Sub-header copy agreement | Not applicable |

The script also handles:

- **Leading padding / write offsets:** it searches the first 8 MB for the first sync pattern that repeats at a valid stride, and starts reading there.
- **Two-second pregap:** reported from the first sector's absolute address (00:00:00 means the 150-sector pregap is included; 00:02:00 means the image starts at the program area). Every sector is scanned either way, so the pregap does not affect extraction.

> The 2332-byte layout is inferred from the byte arithmetic (2336 minus the Form 2 EDC) rather than from a published specification. If you have a real 2332-byte image that misbehaves, please open an issue.

---

## Scrambling

Raw CD data is scrambled on the disc per **ECMA-130 Annex B**: every byte after the 12-byte sync pattern is XORed with output from a 15-bit linear feedback shift register (polynomial x¹⁵ + x + 1, preset to 1). Drives normally descramble during reading, but some dumping tools save the scrambled stream as-is.

**Detection.** The sync pattern itself is never scrambled, so it cannot reveal scrambling. Instead, the script samples 200 sectors across the image and checks each 12-byte header (MSF address, mode byte, and duplicated sub-header) two ways: as stored, and after descrambling. Whichever interpretation produces valid headers wins. The verdict and both counts are printed.

**Descrambling.** The 2340-byte scramble table is generated once at startup. Each sector's header is descrambled to classify it; only audio sectors are fully descrambled, which keeps the scan fast (about 45 seconds for a full 570 MB scrambled image, mostly ffmpeg decoding).

---

## How It Works

1. **Format detection.** Find the sector size and the byte offset where sector data begins.
2. **Scramble detection.** Compare header validity as stored versus descrambled.
3. **Sector scan.** For each sector, read the sub-header (descrambling it if needed). A submode of `0x64` marks a real-time Form 2 audio sector; its 2324-byte user data is appended to that `(file number, channel)` stream on disk.
4. **Decoding.** Each stream is passed to `ffmpeg`, which demuxes the MPEG program stream and writes a `.wav`. The intermediate `.raw` is deleted on success; any ffmpeg warnings are counted and reported.

---

## Example

```bash
python3 cdi_audio_extract.py "Atlantis The Last Resort (CD-i).BIN"
```

```
Detecting sector format for Atlantis The Last Resort (CD-i).BIN...
  Sector size:      2352 bytes (raw sector)
  Data begins at:   byte 0
  Total sectors:    254476
  Pregap included:  no — image starts at 00:02:00, the start of the program area
  Scrambled:        no (200/200 sampled headers valid as stored, 0/200 valid after descrambling)

Scanning sectors...

  [████████████████████████████████████████] 100.0%  (254476/254476 sectors)

Found 2 audio channel(s):
  File 1, Channel 0 | coding=0x7f | 24860 sectors | 55.1 MB
  File 1, Channel 1 | coding=0x7f | 31541 sectors | 69.9 MB

Decoding with ffmpeg...
  ✓ file1_ch0.wav
  ✓ file1_ch1.wav
```

Multiple channels are normal on CD-i titles. They may hold different music cues, speech versus music, or alternate languages.

---

## Testing

The script was verified against synthetic CD-i images built from two known MP2 streams, in nine variants: 2352 plain, 2352 scrambled, 2352 scrambled with a 150-sector pregap and 1000 bytes of leading padding, 2448 plain and scrambled, 2340 plain and scrambled, 2336, and 2332. Every variant decoded **bit-identically** to the source streams with no ffmpeg warnings. The generated scramble table matches the reference table in [libburn's `ecma130ab.c`](https://dev.lovelyhq.com/libburnia/libburn/raw/1.0.2/libburn/ecma130ab.c).

Synthetic images can't capture every quirk of real discs, so reports from real scrambled dumps are welcome.

---

## Known Limitations

- **Pure XA ADPCM audio** (common on Photo CD and some non-game titles) is not decoded. The script detects and extracts those sectors but ffmpeg fails on them, so the `.raw` file is kept for inspection.
- **Sync-less formats (2336, 2332)** carry no header, so scrambling and pregap cannot be determined for them (and scrambled images in those formats are not known to exist).
- **Mixed images** where only part of the disc is scrambled are treated by majority vote. Use `--descramble on/off` to override.

---

## Technical Reference

### Raw Mode 2 Form 2 sector (2352 bytes)

| Bytes | Field | Notes |
|-------|-------|-------|
| 0–11 | Sync | `00 FF FF FF FF FF FF FF FF FF FF 00` (never scrambled) |
| 12–14 | Address | Absolute minute, second, frame in BCD |
| 15 | Mode | `02` for CD-i |
| 16 | File number | Logical file identifier |
| 17 | Channel number | Interleaved channel (0–31) |
| 18 | Submode | Sector type flags (see below) |
| 19 | Coding info | Audio/video coding parameters |
| 20–23 | Sub-header copy | Duplicate of bytes 16–19 |
| 24–2347 | User data | 2324 bytes |
| 2348–2351 | EDC | Error detection (may be zero in Form 2) |

### Submode `0x64`

`0x64` = `0110 0100`:

| Bit | Value | Meaning |
|-----|-------|---------|
| 6 | `0x40` | Real-time sector |
| 5 | `0x20` | Form 2 (2324-byte user data) |
| 2 | `0x04` | Audio |

---

## Acknowledgements

- [**ffmpeg**](https://ffmpeg.org/) performs the MPEG demuxing and MP2 decoding. It is invoked as an external program and is not bundled.
- The scrambler follows **ECMA-130**, Annex B; [libburn](https://dev.lovelyhq.com/libburnia/libburn/raw/1.0.2/libburn/ecma130ab.c) served as the reference implementation for verification.
- Sector layout follows the CD-ROM XA / **Green Book** sub-header format.

---

## License

MIT. This license covers the script only. CD-i games and the audio extracted from them remain the property of their copyright holders.
