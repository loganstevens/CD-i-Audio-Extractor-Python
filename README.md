# cdi_audio_extract.py

A Python script for extracting audio from raw Philips CD-i disc images (`.bin` files). Automatically detects sector format, scans for CD-XA audio sectors, and decodes them to `.wav` files using `ffmpeg`.

---

## Background

Philips CD-i games store audio as **MPEG program streams** interleaved within raw CD sectors. Each sector is 2352 bytes and contains a sub-header that identifies its type (audio, video, or data) and which logical channel it belongs to. Standard ISO tools cannot read CD-i disc images because they use the **Green Book filesystem**, not ISO 9660.

This script bypasses the filesystem entirely, reading the raw sector stream directly and extracting audio by sub-header classification.

---

## Requirements

- Python 3.6+
- [`ffmpeg`](https://ffmpeg.org/) (must be available on your `PATH`)
- No additional Python packages required

### Installing ffmpeg

**macOS:**
```bash
brew install ffmpeg
```

**Linux (Debian/Ubuntu):**
```bash
sudo apt install ffmpeg
```

**Windows:**
Download from [ffmpeg.org](https://ffmpeg.org/download.html) and add the `bin/` folder to your system `PATH`.

---

## Usage

```bash
python3 cdi_audio_extract.py "game.bin"
```

Output `.wav` files are saved to a folder named after the input file:

```
Atlantis The Last Resort (CD-i)_audio/
  file1_ch0.wav
  file1_ch1.wav
```

---

## Supported Formats

The script auto-detects the sector size by scanning for the CD sync pattern (`00 FF FF FF FF FF FF FF FF FF FF 00`) at each known stride:

| Sector Size | Description |
|-------------|-------------|
| 2448 bytes | Raw + 96-byte subchannel data |
| 2352 bytes | Standard raw sector (most common) |
| 2340 bytes | Raw without EDC/ECC |
| 2336 bytes | Mode 2, no sync header |
| 2332 bytes | Mode 2, no sync or EDC |

> **Note:** Scrambled disc images are detected and flagged but are not currently supported. The script will warn you and output may be incorrect.

---

## How It Works

### 1. Sector Format Detection
The script reads the first few sectors and tests each candidate sector size by checking whether the sync pattern repeats at the expected stride. The first matching size is used.

### 2. Pregap Detection
The first 150 sectors of a CD correspond to a two-second pregap. The script checks whether any audio sectors appear in this region to determine if a pregap is present (informational only).

### 3. Audio Sector Scanning
Every sector is read and its **submode byte** (byte 18 of the sector) is checked. A value of `0x64` identifies a real-time CD-XA audio sector. For each matching sector, the **file number** (byte 16) and **channel number** (byte 17) are recorded, and the 2304-byte payload is appended to that channel's buffer.

### 4. Decoding
Each channel's raw payload is written to a temporary `.raw` file and passed to `ffmpeg`, which decodes the MPEG program stream and outputs a `.wav` file. The temporary `.raw` file is deleted on success.

---

## Output

For each audio channel found, the script produces:

- `file{N}_ch{N}.wav` — decoded stereo audio at the stream's native sample rate (typically 44100 Hz)

Multiple channels are common on CD-i titles. They may represent:
- Different in-game music tracks or scenes
- Speech vs. background music
- Multiple language tracks

---

## Diagnostics

The script prints a summary before and after scanning:

```
Detecting sector format...
  Sector size:   2352 bytes
  Total sectors: 254476
  Pregap detected: False
  Scrambled: False

Scanning sectors...
  [████████████████████░░░░░░░░░░░░░░░░░░░░]  52.3%  (133012/254476 sectors)

Submode distribution: {'0x64': 56401, '0x48': 12003, '0x08': 186072}

Found 2 audio channel(s):
  File 1, Channel 0 | coding=0x7f | 61.2 MB
  File 1, Channel 1 | coding=0x7f | 77.8 MB
```

If `ffmpeg` fails to decode a channel, the `.raw` file is kept and the ffmpeg error is printed so you can investigate manually.

---

## Known Limitations

- **Scrambled images** are not supported. The script detects and warns but does not descramble.
- **Pure XA ADPCM audio** (rare in games, more common in Photo CD / Video CD titles) will fail the ffmpeg decode step. The `.raw` file will be retained for manual inspection.
- **Multi-file discs** (multiple file numbers in the sub-header) are handled correctly but are uncommon in CD-i games.
- The script must be run on the **original raw `.bin` file**. Do not use `bchunk`-extracted `.iso` files, as `bchunk` strips the sector headers that the script relies on.

---

## Example

```bash
python3 cdi_audio_extract.py "Atlantis The Last Resort (CD-i).BIN"
```

```
Detecting sector format for Atlantis The Last Resort (CD-i).BIN...
  Sector size:   2352 bytes
  Total sectors: 254476
  Pregap detected: False
  Scrambled: False

Scanning sectors...
  [████████████████████████████████████████] 100.0%  (254476/254476 sectors)

Submode distribution: {'0x64': 56401, '0x48': 0, '0x08': 198075}

Found 2 audio channel(s):
  File 1, Channel 0 | coding=0x7f | 61.2 MB
  File 1, Channel 1 | coding=0x7f | 77.8 MB

Extracting and decoding...
  ✓ file1_ch0.wav
  ✓ file1_ch1.wav

Done! Output in: Atlantis The Last Resort (CD-i)_audio/
```

---

## Technical Reference

### CD-i Sector Layout (2352-byte MODE2/Form2)

| Bytes | Field | Description |
|-------|-------|-------------|
| 0–11 | Sync | `00 FF FF FF FF FF FF FF FF FF FF 00` |
| 12–15 | Header | Minutes, seconds, frames, mode |
| 16 | File Number | Logical file identifier |
| 17 | Channel Number | Audio/video channel (0–31) |
| 18 | Submode | `0x64` = audio, `0x48` = video, `0x08` = data |
| 19 | Coding Info | Sample rate, stereo/mono, bit depth |
| 24–2327 | Payload | 2304 bytes of audio/video data |
| 2328–2351 | EDC/ECC | Error detection/correction |

### Submode Byte (`0x64`)

`0x64` = `0110 0100` in binary:

| Bit | Meaning |
|-----|---------|
| 6 | Real-time sector |
| 5 | Form 2 (2324-byte payload) |
| 2 | Audio data present |

---

## License

MIT
