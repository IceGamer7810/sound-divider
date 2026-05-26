# sound-divider

Windows-only audio splitter mainly for Minecraft resource packs.
Works with almost every audio file- and even with some video file formats!

## Requirements
- Windows
- Python 3.10+
- VLC installed (used for decode/encode; no `pip install` required)

## What it does
- Decodes the selected input audio/video file to a temporary WAV.
- Splits it into fixed-length chunks (chunk length can be fractional seconds).
- Pads the last chunk with real silence to exact chunk length.
- Encodes chunks to the chosen output format (e.g. `ogg`).

## Usage
- Run: `python divider.py`

## Notes
- Output folder name: `<InputStem>_<ext>`
- `VLC_WORKERS` env var controls parallel VLC encodes (default up to 8).
