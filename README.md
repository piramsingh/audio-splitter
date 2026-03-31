# Kirtan Audio Splitter

A local web app for splitting long kirtan recordings into individual tracks by kirtani. Upload an audio file, enter timestamps, and download a ZIP of named splits.

## Features

- Upload MP3, WAV, MP4, or M4A (any size)
- Paste YouTube or SoundCloud URL to import audio
- Name each kirtani — auto-completes "Bhai" / "Bibi" with project name
- Paste a bulk timestamp list to fill rows instantly
- Output as MP3 or M4A
- Download all splits as a ZIP
- Optional: upload splits directly to Google Drive or SoundCloud

## Prerequisites

- Python 3.10+ — `brew install python`
- ffmpeg — `brew install ffmpeg`

## Setup

```bash
git clone https://github.com/piramsingh/audiosplitter.git
cd ssa-sounds-splitter

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
source venv/bin/activate
python app.py
```

Open **http://localhost:5001** in your browser.

## Usage

1. **Step 1** — Upload an audio file or paste a YouTube/SoundCloud URL
2. **Step 2** — Enter a project name (e.g. `UCI Kirtan Night - 02.27.26`)
3. **Step 3** — Add kirtanis with start/end timestamps
   - Type `Bhai ` or `Bibi ` to autocomplete the full name format
   - Use **Paste timestamps** to bulk-import from a notes app
4. Click **Split & Download**

## Optional: Google Drive & SoundCloud Upload

To enable uploading splits directly to Google Drive or SoundCloud, create a `.env` file:

```
GD_CLIENT_ID=...
GD_CLIENT_SECRET=...
GD_REDIRECT_URI=http://localhost:5001/gdrive/callback

SC_CLIENT_ID=...
SC_CLIENT_SECRET=...
SC_REDIRECT_URI=http://localhost:5001/soundcloud/callback
```

These are optional — the app works fully without them.
