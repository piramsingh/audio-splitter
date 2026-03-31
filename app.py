import os
import sys
import json
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
import zipfile
import hashlib
import base64
import secrets
from io import BytesIO

import requests as http_requests
from dotenv import load_dotenv
from flask import Flask, request, send_file, render_template, jsonify, redirect
from urllib.parse import quote as url_quote

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024 * 1024  # 10 GB

_TMP_BASE   = os.environ.get("UPLOAD_BASE", os.path.dirname(__file__))
UPLOAD_DIR  = os.path.join(_TMP_BASE, "uploads")
YT_DIR      = os.path.join(_TMP_BASE, "yt_downloads")
SC_ZIP_DIR  = os.path.join(_TMP_BASE, "sc_zips")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(YT_DIR, exist_ok=True)
os.makedirs(SC_ZIP_DIR, exist_ok=True)

# Optional YouTube cookies for bypassing bot detection on server IPs.
# Set YOUTUBE_COOKIES env var to the contents of a Netscape-format cookies.txt
# (export from your browser with a "Get cookies.txt LOCALLY" extension).
_YT_COOKIES_PATH = os.path.join(_TMP_BASE, "yt_cookies.txt")
_yt_cookies_raw = os.environ.get("YOUTUBE_COOKIES", "")
if _yt_cookies_raw:
    with open(_YT_COOKIES_PATH, "w") as _f:
        _f.write(_yt_cookies_raw)
else:
    _YT_COOKIES_PATH = None

# SoundCloud config
SC_CLIENT_ID = os.environ.get("SC_CLIENT_ID", "")
SC_CLIENT_SECRET = os.environ.get("SC_CLIENT_SECRET", "")
SC_REDIRECT_URI = os.environ.get("SC_REDIRECT_URI", "http://localhost:5001/soundcloud/callback")

# Google Drive config
GD_CLIENT_ID     = os.environ.get("GD_CLIENT_ID", "")
GD_CLIENT_SECRET = os.environ.get("GD_CLIENT_SECRET", "")
GD_REDIRECT_URI  = os.environ.get("GD_REDIRECT_URI", "http://localhost:5001/gdrive/callback")

# In-memory maps
yt_files = {}        # file_id -> {"path": ..., "title": ...}
yt_progress = {}     # file_id -> {"status": ..., "percent": ..., "stage": ..., ...}
convert_progress = {}  # file_id -> {"status": ..., "percent": ..., "mp3_path": ...}
diarize_progress = {}  # job_id -> {"status": ..., "percent": ..., "stage": ..., "segments": [...]}
split_progress = {}    # job_id -> {"status": ..., "completed": N, "total": N, "zip_data": bytes}

# SoundCloud state
sc_pkce_states = {}    # state -> {code_verifier, job_id, project_name}
sc_pending = {}        # split_job_id -> {zip_data, project_name, track_names, file_ext}
sc_access_token = None  # set after successful OAuth
sc_upload_progress = {}  # upload_job_id -> {status, completed, total, playlist_url}
sc_latest_upload_job_id = None  # most-recent upload job (polled by main page)
sc_cover_art = {}              # split_job_id -> bytes (optional album cover image)

# Google Drive state
gd_oauth_states         = {}   # state -> {job_id, project_name}
gd_access_token         = None # string access token
gd_upload_progress      = {}   # upload_job_id -> {status, pct, folder_url}
gd_latest_upload_job_id = None


@app.errorhandler(413)
def too_large(_e):
    return jsonify({"error": "File too large. Maximum size is 10 GB."}), 413


@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": f"Server error: {e}"}), 500


@app.route("/")
def index():
    return render_template("index.html")


def format_duration(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _sec_to_hms(seconds):
    """Convert float seconds to HH:MM:SS string for chapter timestamps."""
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def get_duration(file_path):
    """Use ffprobe to get the duration in seconds."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        file_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe error: {result.stderr[:500]}")
    return float(result.stdout.strip())


# ── YouTube endpoints ────────────────────────────────────────────────

def _yt_download_worker(file_id, url, work_dir):
    """Background worker: download audio via yt-dlp."""
    venv_python = sys.executable

    try:
        yt_progress[file_id] = {"status": "downloading", "percent": 0, "stage": "Fetching video info..."}

        # ── Step 1: Metadata ──────────────────────────────────────────
        meta_cmd = [
            venv_python, "-m", "yt_dlp",
            "--no-download", "--no-playlist",
            "--extractor-args", "youtube:player_client=tv_embedded,ios,mweb",
            "--print", "%(title)s",
            "--print", "%(duration)s",
            "--print", "%(chapters)j",
            url,
        ]
        if _YT_COOKIES_PATH:
            meta_cmd += ["--cookies", _YT_COOKIES_PATH]
        meta_result = subprocess.run(meta_cmd, capture_output=True, text=True, timeout=60)
        ytdlp_meta_ok = meta_result.returncode == 0

        title = None
        duration_sec = 0
        chapters = []

        if ytdlp_meta_ok:
            meta_lines = meta_result.stdout.strip().split("\n")
            non_empty = [l for l in meta_lines if l.strip()]
            if len(non_empty) >= 2:
                title = non_empty[0]
                try:
                    duration_sec = float(non_empty[1])
                except ValueError:
                    pass
                if len(non_empty) >= 3:
                    try:
                        raw = json.loads(non_empty[2])
                        if isinstance(raw, list):
                            valid = [ch for ch in raw if isinstance(ch, dict) and "start_time" in ch]
                            for ch in valid:
                                chapters.append({
                                    "title": ch.get("title", f"Chapter {len(chapters) + 1}"),
                                    "start": _sec_to_hms(ch["start_time"]),
                                    "end":   "",
                                })
                    except (json.JSONDecodeError, KeyError, TypeError):
                        pass

        # ── Step 2: Download ──────────────────────────────────────────
        filepath = None

        if ytdlp_meta_ok:
            yt_progress[file_id]["stage"] = "Downloading audio..."
            output_template = os.path.join(work_dir, "%(title)s.%(ext)s")
            dl_cmd = [
                venv_python, "-m", "yt_dlp",
                "-f", "bestaudio",
                "-o", output_template,
                "--no-playlist",
                "--newline",
                "--concurrent-fragments", "4",
                "--extractor-args", "youtube:player_client=tv_embedded,ios,mweb",
                url,
            ]
            if _YT_COOKIES_PATH:
                dl_cmd += ["--cookies", _YT_COOKIES_PATH]
            proc = subprocess.Popen(dl_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            pct_re = re.compile(r"\[download\]\s+([\d.]+)%")
            for line in proc.stdout:
                match = pct_re.search(line)
                if match:
                    yt_progress[file_id]["percent"] = float(match.group(1))
            proc.wait()

            if proc.returncode == 0:
                audio_files = [f for f in os.listdir(work_dir) if not f.startswith(".")]
                if audio_files:
                    filepath = os.path.join(work_dir, audio_files[0])

        if not filepath or not os.path.exists(filepath):
            yt_progress[file_id] = {"status": "error", "error": "Download failed. YouTube may be blocking this server — try uploading the file directly instead."}
            return

        if not title:
            title = os.path.splitext(os.path.basename(filepath))[0]

        yt_files[file_id] = {"path": filepath, "title": title}
        yt_progress[file_id] = {
            "status": "done",
            "percent": 100,
            "stage": "Complete",
            "file_id": file_id,
            "title": title,
            "duration_seconds": duration_sec,
            "duration_formatted": format_duration(duration_sec),
            "chapters": chapters,
        }

    except subprocess.TimeoutExpired:
        shutil.rmtree(work_dir, ignore_errors=True)
        yt_progress[file_id] = {"status": "error", "error": "Download timed out"}
    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        yt_progress[file_id] = {"status": "error", "error": str(e)}


@app.route("/youtube", methods=["POST"])
def youtube_convert():
    data = request.get_json()
    if not data or not data.get("url"):
        return jsonify({"error": "No YouTube URL provided"}), 400

    url = data["url"].strip()
    file_id = uuid.uuid4().hex[:12]
    os.makedirs(YT_DIR, exist_ok=True)
    work_dir = os.path.join(YT_DIR, file_id)
    os.makedirs(work_dir, exist_ok=True)

    # Start download in background thread
    thread = threading.Thread(target=_yt_download_worker, args=(file_id, url, work_dir), daemon=True)
    thread.start()

    return jsonify({"file_id": file_id})


@app.route("/youtube/progress/<file_id>")
def youtube_progress(file_id):
    progress = yt_progress.get(file_id)
    if not progress:
        return jsonify({"status": "unknown"}), 404
    return jsonify(progress)


def _convert_worker(file_id, src, mp3_path, duration_sec):
    """Background worker: convert audio to MP3 with progress tracking."""
    try:
        convert_progress[file_id] = {"status": "converting", "percent": 0}

        cmd = [
            "ffmpeg", "-y", "-i", src,
            "-vn", "-codec:a", "libmp3lame", "-b:a", "192k",
            "-progress", "pipe:1",
            mp3_path,
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        time_re = re.compile(r"out_time_ms=(\d+)")
        for line in proc.stdout:
            match = time_re.search(line)
            if match and duration_sec > 0:
                out_sec = int(match.group(1)) / 1_000_000
                pct = min(99, (out_sec / duration_sec) * 100)
                convert_progress[file_id]["percent"] = round(pct, 1)

        proc.wait()
        if proc.returncode != 0:
            convert_progress[file_id] = {"status": "error", "error": "Conversion failed"}
            return

        convert_progress[file_id] = {"status": "done", "percent": 100, "mp3_path": mp3_path}

    except Exception as e:
        convert_progress[file_id] = {"status": "error", "error": str(e)}


@app.route("/youtube/convert/<file_id>", methods=["POST"])
def youtube_convert_mp3(file_id):
    info = yt_files.get(file_id)
    if not info or not os.path.exists(info["path"]):
        return jsonify({"error": "File not found or expired"}), 404

    src = info["path"]

    # Already MP3 — no conversion needed
    if src.endswith(".mp3"):
        convert_progress[file_id] = {"status": "done", "percent": 100, "mp3_path": src}
        return jsonify({"status": "done"})

    mp3_path = os.path.splitext(src)[0] + ".mp3"

    # Already converted
    if os.path.exists(mp3_path):
        convert_progress[file_id] = {"status": "done", "percent": 100, "mp3_path": mp3_path}
        return jsonify({"status": "done"})

    # Get duration for progress calculation
    duration_sec = 0
    prog = yt_progress.get(file_id)
    if prog and prog.get("duration_seconds"):
        duration_sec = prog["duration_seconds"]

    thread = threading.Thread(
        target=_convert_worker,
        args=(file_id, src, mp3_path, duration_sec),
        daemon=True,
    )
    thread.start()
    return jsonify({"status": "started"})


@app.route("/youtube/convert-progress/<file_id>")
def youtube_convert_progress(file_id):
    prog = convert_progress.get(file_id)
    if not prog:
        return jsonify({"status": "unknown"}), 404
    return jsonify(prog)


@app.route("/youtube/download/<file_id>")
def youtube_download(file_id):
    info = yt_files.get(file_id)
    if not info or not os.path.exists(info["path"]):
        return jsonify({"error": "File not found or expired"}), 404

    # Serve the converted MP3 if available
    prog = convert_progress.get(file_id)
    if prog and prog.get("status") == "done" and prog.get("mp3_path"):
        return send_file(prog["mp3_path"], mimetype="audio/mpeg", as_attachment=True,
                         download_name=f"{info['title']}.mp3")

    # Fallback: serve source directly
    src = info["path"]
    if src.endswith(".mp3"):
        return send_file(src, mimetype="audio/mpeg", as_attachment=True,
                         download_name=f"{info['title']}.mp3")

    return jsonify({"error": "MP3 not ready. Start conversion first."}), 400


# ── Diarization endpoints ────────────────────────────────────────────

def _merge_speaker_segments(segments, gap_tolerance=10.0):
    """Merge consecutive same-speaker segments that have small gaps between them."""
    if not segments:
        return segments
    segments = sorted(segments, key=lambda s: s["start"])
    merged = [dict(segments[0])]
    for seg in segments[1:]:
        last = merged[-1]
        if seg["speaker"] == last["speaker"] and (seg["start"] - last["end"]) <= gap_tolerance:
            last["end"] = seg["end"]
        else:
            merged.append(dict(seg))
    return merged


def _diarize_worker(job_id, audio_path, num_speakers, cleanup_dir=None):
    """Fast diarization using pyannote speaker embeddings on sampled clips.

    Instead of running the full sliding-window pipeline on the entire audio
    (slow), we:
      1. Sample N evenly-spaced 30s clips from the recording
      2. Compute a speaker embedding for each clip using pyannote's embedding
         model directly (much faster than the full pipeline)
      3. Find the N-1 largest voice jumps between consecutive samples
         → those are the kirtani transition points

    For an 8-hour recording with 60 samples this runs in ~2-3 minutes.
    Falls back to the full pipeline (capped at 90 min) if embedding
    extraction fails.
    """
    wav_path = None
    try:
        hf_token = os.environ.get("HF_TOKEN")
        if not hf_token:
            diarize_progress[job_id] = {
                "status": "error",
                "error": (
                    "HuggingFace token not configured. "
                    "Add HF_TOKEN=your_token to your .env file. "
                    "See README for setup instructions."
                ),
            }
            return

        # ── Stage 1: Convert to 16 kHz mono WAV ──────────────────────────
        diarize_progress[job_id] = {
            "status": "processing", "percent": 3, "stage": "Preparing audio...",
        }
        wav_path = audio_path + "_diarize.wav"
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path, "-ar", "16000", "-ac", "1", wav_path],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            diarize_progress[job_id] = {
                "status": "error",
                "error": f"Audio preparation failed: {result.stderr[:300]}",
            }
            return

        duration = get_duration(wav_path)

        # ── Stage 2: Load model ───────────────────────────────────────────
        diarize_progress[job_id] = {
            "status": "processing", "percent": 10,
            "stage": "Loading speaker model (first run downloads ~1 GB)...",
        }

        import torch
        import numpy as np
        from pyannote.audio import Pipeline as PyannotePipeline

        pipeline = PyannotePipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1", token=hf_token,
        )
        if torch.backends.mps.is_available():
            pipeline.to(torch.device("mps"))
        elif torch.cuda.is_available():
            pipeline.to(torch.device("cuda"))

        for attr, val in [
            ("segmentation_batch_size", 128),
            ("embedding_batch_size", 128),
        ]:
            try:
                setattr(pipeline, attr, val)
            except Exception:
                pass

        n_speakers = 3
        if num_speakers:
            try:
                n_speakers = int(num_speakers)
            except (ValueError, TypeError):
                pass

        # ── Stage 3: Fast path — load audio once, embed sampled clips ───────
        # 10s sample every ~5 min; min 20, max 100 samples total.
        clip_sec = 10
        n_samples = max(20, min(100, int(duration / 300)))
        sample_times = [i * duration / n_samples for i in range(n_samples)]

        # Load the full WAV into memory once — eliminates per-clip ffmpeg overhead.
        diarize_progress[job_id] = {
            "status": "processing", "percent": 18,
            "stage": "Loading audio into memory...",
        }

        import torchaudio

        waveform = None
        sample_rate = None
        try:
            waveform, sample_rate = torchaudio.load(wav_path)
            if waveform.shape[0] > 1:
                waveform = waveform.mean(dim=0, keepdim=True)  # force mono
        except Exception:
            pass  # fall through to full pipeline below

        embedding_fn = getattr(pipeline, "_embedding", None) if waveform is not None else None

        embeddings = []
        valid_times = []

        if embedding_fn is not None:
            diarize_progress[job_id] = {
                "status": "processing", "percent": 20,
                "stage": f"Analyzing {n_samples} audio samples...",
            }

        for i, t in enumerate(sample_times):
            if embedding_fn is None:
                break  # fast path unavailable, fall through to full pipeline

            actual_dur = min(clip_sec, duration - t)
            if actual_dur < 3:
                continue

            try:
                start_s = int(t * sample_rate)
                end_s = min(start_s + int(actual_dur * sample_rate), waveform.shape[1])
                clip_waveform = waveform[:, start_s:end_s]

                raw = embedding_fn({"waveform": clip_waveform, "sample_rate": sample_rate})
                emb = np.array(raw)
                if emb.ndim == 2:
                    emb = emb.mean(axis=0)  # average sliding-window frames → 1-D
                embeddings.append(emb.flatten())
                valid_times.append(t)

            except Exception:
                embedding_fn = None  # disable on first failure

            diarize_progress[job_id]["percent"] = 20 + int(((i + 1) / n_samples) * 62)
            diarize_progress[job_id]["stage"] = (
                f"Sampling audio ({i + 1}/{n_samples})..."
            )

        if len(embeddings) >= max(2, n_speakers):
            # ── Fast path: cluster embeddings ─────────────────────────────
            diarize_progress[job_id] = {
                "status": "processing", "percent": 84,
                "stage": "Finding speaker transitions...",
            }

            E = np.array(embeddings)
            ts = np.array(valid_times)

            # Normalise
            E = E / (np.linalg.norm(E, axis=1, keepdims=True) + 1e-8)

            # Cosine distance between consecutive samples
            dists = 1.0 - (E[:-1] * E[1:]).sum(axis=1)

            # The n_speakers-1 largest jumps are speaker transitions
            n_breaks = n_speakers - 1
            break_pos = set(np.argsort(dists)[::-1][:n_breaks].tolist())

            # Assign sequential labels
            labels = []
            lbl = 0
            for idx in range(len(ts)):
                if idx > 0 and (idx - 1) in break_pos:
                    lbl += 1
                labels.append(lbl)

            # Build segments: boundary = midpoint between the two adjacent samples
            raw_segments = []
            seg_start = 0.0
            prev = labels[0]
            for idx in range(1, len(ts)):
                if labels[idx] != prev:
                    boundary = (ts[idx - 1] + clip_sec / 2 + ts[idx]) / 2
                    raw_segments.append({
                        "start": seg_start,
                        "end": boundary,
                        "speaker": f"SPEAKER_{prev}",
                    })
                    seg_start = boundary
                    prev = labels[idx]
            raw_segments.append({
                "start": seg_start, "end": duration,
                "speaker": f"SPEAKER_{prev}",
            })

        else:
            # ── Fallback: full pipeline, capped at 90 min ─────────────────
            diarize_progress[job_id] = {
                "status": "processing", "percent": 22,
                "stage": "Running full analysis (may take ~10 min)...",
            }

            process_path = wav_path
            if duration > 5400:  # cap at 90 min to stay tractable
                trimmed = wav_path + "_trim.wav"
                subprocess.run(
                    ["ffmpeg", "-y", "-i", wav_path, "-t", "5400", trimmed],
                    capture_output=True,
                )
                process_path = trimmed

            for attr, val in [
                ("segmentation_step", 0.5),
                ("min_duration_on", 2.0),
                ("min_duration_off", 0.5),
            ]:
                try:
                    setattr(pipeline, attr, val)
                except Exception:
                    pass

            from pyannote.audio.pipelines.utils.hook import ProgressHook

            class _Hook(ProgressHook):
                def __call__(self, step_name, step_artifact,
                             file=None, total=None, completed=None):
                    super().__call__(step_name, step_artifact,
                                     file=file, total=total, completed=completed)
                    if total and completed is not None and total > 0:
                        pct = 22 + int((completed / total) * 65)
                        diarize_progress[job_id]["percent"] = min(87, pct)
                        diarize_progress[job_id]["stage"] = (
                            f"Analyzing: {step_name}..."
                        )

            with _Hook() as hook:
                diarization = pipeline(
                    process_path, hook=hook, num_speakers=n_speakers,
                )

            # pyannote 4.x returns DiarizeOutput; 3.x returned Annotation directly
            annotation = getattr(diarization, "diarization", diarization)
            raw_segments = [
                {"start": turn.start, "end": turn.end, "speaker": speaker}
                for turn, _, speaker in annotation.itertracks(yield_label=True)
            ]

            if process_path != wav_path and os.path.exists(process_path):
                try:
                    os.remove(process_path)
                except OSError:
                    pass

        # ── Stage 4: Format and done ──────────────────────────────────────
        diarize_progress[job_id] = {
            "status": "processing", "percent": 96, "stage": "Processing results...",
        }

        merged = _merge_speaker_segments(raw_segments)
        formatted = [
            {
                "start": format_duration(seg["start"]),
                "end": format_duration(seg["end"]),
                "speaker": seg["speaker"],
            }
            for seg in merged
        ]

        diarize_progress[job_id] = {
            "status": "done", "percent": 100, "stage": "Complete",
            "segments": formatted,
        }

    except Exception as e:
        diarize_progress[job_id] = {"status": "error", "error": str(e)}
    finally:
        if wav_path and os.path.exists(wav_path):
            try:
                os.remove(wav_path)
            except OSError:
                pass
        if cleanup_dir and os.path.isdir(cleanup_dir):
            shutil.rmtree(cleanup_dir, ignore_errors=True)


@app.route("/diarize", methods=["POST"])
def diarize_audio():
    num_speakers = request.form.get("num_speakers")
    file_id = request.form.get("file_id")
    audio_file = request.files.get("audio")

    if file_id:
        info = yt_files.get(file_id)
        if not info or not os.path.exists(info["path"]):
            return jsonify({"error": "YouTube file not found or expired"}), 404
        audio_path = info["path"]
        cleanup_dir = None
    elif audio_file:
        work_dir = tempfile.mkdtemp(dir=UPLOAD_DIR)
        audio_path = os.path.join(work_dir, audio_file.filename or "input")
        audio_file.save(audio_path)
        cleanup_dir = work_dir
    else:
        return jsonify({"error": "No audio file provided"}), 400

    job_id = uuid.uuid4().hex[:12]
    diarize_progress[job_id] = {"status": "queued", "percent": 0, "stage": "Starting..."}

    thread = threading.Thread(
        target=_diarize_worker,
        args=(job_id, audio_path, num_speakers, cleanup_dir),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/diarize/progress/<job_id>")
def diarize_progress_endpoint(job_id):
    progress = diarize_progress.get(job_id)
    if not progress:
        return jsonify({"status": "unknown"}), 404
    return jsonify(progress)


# ── Duration endpoint ────────────────────────────────────────────────

@app.route("/duration", methods=["POST"])
def audio_duration():
    audio_file = request.files.get("audio")
    if not audio_file:
        return jsonify({"error": "No audio file provided"}), 400

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    work_dir = tempfile.mkdtemp(dir=UPLOAD_DIR)
    try:
        input_filename = audio_file.filename or "input"
        input_path = os.path.join(work_dir, input_filename)
        audio_file.save(input_path)

        seconds = get_duration(input_path)
        return jsonify({
            "duration_seconds": seconds,
            "duration_formatted": format_duration(seconds),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ── Split endpoint ───────────────────────────────────────────────────

def _hms_to_sec(t):
    parts = t.strip().split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return float(parts[0])


def ffmpeg_split(input_path, output_path, start_time, end_time, output_format):
    """Extract a segment using fast input seeking + stream copy (no re-encode)."""
    duration = _hms_to_sec(end_time) - _hms_to_sec(start_time)

    # -ss before -i = fast input seek (jumps directly, no decode from start)
    # -c copy = no re-encoding, just byte extraction — 10-100x faster
    cmd = [
        "ffmpeg", "-y",
        "-ss", start_time,
        "-i", input_path,
        "-t", str(duration),
        "-vn",
        "-c", "copy",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        return

    # Fallback: re-encode if copy fails (e.g. format mismatch)
    if output_format == "mp3":
        codec_args = ["-codec:a", "libmp3lame", "-b:a", "192k"]
    else:
        codec_args = ["-codec:a", "aac", "-b:a", "192k"]

    cmd = [
        "ffmpeg", "-y",
        "-ss", start_time,
        "-i", input_path,
        "-t", str(duration),
        "-vn",
    ] + codec_args + [output_path]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg error: {result.stderr[:500]}")


def _split_worker(job_id, input_path, segments, output_format, project_name, work_dir):
    """Background worker: split each segment, update per-track progress, zip results."""
    total = len(segments)
    split_progress[job_id] = {"status": "processing", "completed": 0, "total": total}
    file_ext = ".mp3" if output_format == "mp3" else ".m4a"

    try:
        clip_paths = []
        track_names = []
        for i, seg in enumerate(segments):
            name = seg.get("name", f"Track {i + 1}").strip()
            clip_filename = f"{name}{file_ext}"
            clip_path = os.path.join(work_dir, clip_filename)
            ffmpeg_split(input_path, clip_path, seg["start"].strip(), seg["end"].strip(), output_format)
            clip_paths.append((clip_path, clip_filename))
            track_names.append(name)
            split_progress[job_id]["completed"] = i + 1

        # Build zip in memory so we can clean up work_dir immediately
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for clip_path, clip_filename in clip_paths:
                zf.write(clip_path, f"{project_name}/{clip_filename}")

        zip_data = zip_buffer.getvalue()

        split_progress[job_id] = {
            "status": "done",
            "completed": total,
            "total": total,
            "zip_data": zip_data,
            "project_name": project_name,
        }

        # Keep zip + metadata available for SoundCloud upload.
        # Also write to disk so it survives a server restart (Flask reloader, etc.)
        sc_meta = {"project_name": project_name, "track_names": track_names, "file_ext": file_ext}
        try:
            with open(os.path.join(SC_ZIP_DIR, f"{job_id}.zip"), "wb") as fz:
                fz.write(zip_data)
            with open(os.path.join(SC_ZIP_DIR, f"{job_id}.json"), "w") as fm:
                json.dump(sc_meta, fm)
        except OSError:
            pass  # disk write failed — in-memory fallback still works

        sc_pending[job_id] = {**sc_meta, "zip_data": zip_data}

    except Exception as e:
        split_progress[job_id] = {"status": "error", "error": str(e)}
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@app.route("/split", methods=["POST"])
def split_audio():
    segments_json = request.form.get("segments")
    if not segments_json:
        return jsonify({"error": "No segments provided"}), 400

    output_format = request.form.get("format", "mp3").lower()
    if output_format not in ("mp3", "mp4"):
        return jsonify({"error": "Output format must be mp3 or mp4"}), 400

    project_name = request.form.get("project_name", "ssa_sounds_splits").strip()
    if not project_name:
        project_name = "ssa_sounds_splits"

    try:
        segments = json.loads(segments_json)
    except json.JSONDecodeError:
        return jsonify({"error": "Invalid segments JSON"}), 400

    if not segments:
        return jsonify({"error": "At least one segment is required"}), 400

    file_id = request.form.get("file_id")
    audio_file = request.files.get("audio")

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(SC_ZIP_DIR, exist_ok=True)

    if file_id:
        info = yt_files.get(file_id)
        if not info or not os.path.exists(info["path"]):
            return jsonify({"error": "YouTube file not found or expired"}), 404
        input_path = info["path"]
        work_dir = tempfile.mkdtemp(dir=UPLOAD_DIR)
    elif audio_file:
        work_dir = tempfile.mkdtemp(dir=UPLOAD_DIR)
        input_filename = audio_file.filename or "input"
        input_path = os.path.join(work_dir, input_filename)
        audio_file.save(input_path)
    else:
        return jsonify({"error": "No audio file provided"}), 400

    job_id = uuid.uuid4().hex[:12]
    split_progress[job_id] = {"status": "queued", "completed": 0, "total": len(segments)}

    thread = threading.Thread(
        target=_split_worker,
        args=(job_id, input_path, segments, output_format, project_name, work_dir),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/split/progress/<job_id>")
def split_progress_endpoint(job_id):
    prog = split_progress.get(job_id)
    if not prog:
        return jsonify({"status": "unknown"}), 404
    # Don't send the binary zip_data in progress polls
    return jsonify({k: v for k, v in prog.items() if k != "zip_data"})


@app.route("/split/download/<job_id>")
def split_download(job_id):
    prog = split_progress.pop(job_id, None)
    if not prog or prog.get("status") != "done":
        return jsonify({"error": "Not ready or not found"}), 404
    return send_file(
        BytesIO(prog["zip_data"]),
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{prog['project_name']}.zip",
    )


# ── SoundCloud endpoints ─────────────────────────────────────────────

def _sc_error_page(msg):
    """Return a simple HTML error page shown in the SC tab."""
    safe = msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Error – SoundCloud Upload</title>
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;
       display:flex;align-items:center;justify-content:center;min-height:100vh;
       margin:0;background:#fff;color:#1a1a2a}}
  .box{{text-align:center;max-width:420px;padding:2rem}}
  h2{{color:#ef4444;margin-bottom:.6rem;font-size:1.2rem}}
  p{{color:#6b7280;font-size:.9rem;line-height:1.5}}
  .hint{{margin-top:1rem;font-size:.8rem;color:#9ca3af}}
</style></head><body>
<div class="box">
  <h2>Upload failed</h2>
  <p>{safe}</p>
  <p class="hint">Close this tab and try again.</p>
</div></body></html>"""


def _sc_progress_page(upload_job_id, project_name, split_job_id=None):
    """Return an HTML progress page that polls upload status then redirects to SoundCloud."""
    safe_name = project_name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    cover_html = ""
    if split_job_id and sc_cover_art.get(split_job_id):
        cover_html = (
            f'<img src="/soundcloud/cover/{split_job_id}" '
            f'style="width:88px;height:88px;object-fit:cover;border-radius:12px;'
            f'margin-bottom:1rem;box-shadow:0 4px 16px rgba(0,0,0,.12);">'
        )
    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Uploading to SoundCloud – {safe_name}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;
       background:#fff;color:#1a1a2a;display:flex;flex-direction:column;
       align-items:center;justify-content:center;min-height:100vh;padding:2rem}}
  .orb{{width:64px;height:64px;background:#ff5500;border-radius:16px;display:flex;
        align-items:center;justify-content:center;margin-bottom:1.25rem;
        box-shadow:0 8px 24px rgba(255,85,0,.35)}}
  h1{{font-size:1.25rem;font-weight:700;margin-bottom:.3rem;text-align:center}}
  .subtitle{{font-size:.85rem;color:#6b7280;margin-bottom:2rem;text-align:center}}
  .track{{width:340px;max-width:88vw;height:6px;background:#f3f4f6;border-radius:3px;
          overflow:hidden;margin-bottom:.65rem}}
  .fill{{height:100%;background:linear-gradient(90deg,#ff5500,#ff8040);
         border-radius:3px;transition:width .6s cubic-bezier(.4,0,.2,1);width:0%}}
  .status{{font-size:.82rem;color:#9ca3af;text-align:center;min-height:1.2rem;letter-spacing:.01em}}
  .done-msg{{font-size:.9rem;color:#ff5500;font-weight:600;text-align:center;display:none}}
  .err{{color:#ef4444;font-size:.85rem;text-align:center;max-width:340px;
        padding:.9rem;background:#fef2f2;border:1px solid #fecaca;border-radius:10px;
        display:none;margin-top:1rem;line-height:1.5}}
</style></head><body>
<div class="orb">
  <svg viewBox="0 0 24 24" fill="none" width="30" height="30">
    <path stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"
          d="M12 15.5V7M9 10l3-3 3 3M5.5 19.5h13"/>
  </svg>
</div>
{cover_html}<h1>{safe_name}</h1>
<p class="subtitle">Uploading your kirtan recordings to SoundCloud...</p>
<div class="track"><div class="fill" id="fill"></div></div>
<p class="status" id="status">Preparing...</p>
<p class="done-msg" id="doneMsg">Done! Redirecting to SoundCloud...</p>
<p class="err" id="err"></p>
<script>
const JOB = {json.dumps(upload_job_id)};
async function poll() {{
  for (;;) {{
    await new Promise(r => setTimeout(r, 1500));
    let prog;
    try {{
      const r = await fetch('/soundcloud/upload-progress/' + JOB);
      if (!r.ok) continue;
      prog = await r.json();
    }} catch(e) {{ continue; }}
    const c = prog.completed || 0, t = prog.total || 1;
    document.getElementById('fill').style.width = Math.round(c / t * 100) + '%';
    if (prog.status === 'error') {{
      document.getElementById('status').style.display = 'none';
      const e = document.getElementById('err');
      e.style.display = 'block';
      e.textContent = prog.error || 'Upload failed.';
      return;
    }}
    if (prog.status === 'done') {{
      document.getElementById('fill').style.width = '100%';
      document.getElementById('status').style.display = 'none';
      document.getElementById('doneMsg').style.display = 'block';
      setTimeout(() => {{
        window.location.href = prog.playlist_url || 'https://soundcloud.com/you/tracks';
      }}, 1200);
      return;
    }}
    document.getElementById('status').textContent =
      c < t ? 'Uploading track ' + (c + 1) + ' of ' + t + '...' : 'Creating album...';
  }}
}}
poll();
</script></body></html>"""


def _sc_start_upload(split_job_id, project_name):
    """Load split data and kick off the upload worker.
    Returns {"upload_job_id": str} on success or {"error": str} on failure.
    """
    global sc_latest_upload_job_id
    pending = sc_pending.get(split_job_id)
    if not pending:
        zip_path  = os.path.join(SC_ZIP_DIR, f"{split_job_id}.zip")
        meta_path = os.path.join(SC_ZIP_DIR, f"{split_job_id}.json")
        if os.path.exists(zip_path) and os.path.exists(meta_path):
            try:
                with open(zip_path, "rb") as fz:
                    zip_data = fz.read()
                with open(meta_path) as fm:
                    meta = json.load(fm)
                pending = {**meta, "zip_data": zip_data}
                sc_pending[split_job_id] = pending
            except Exception as e:
                return {"error": f"Could not load split data: {e}"}
        else:
            return {"error": "Split data not found. Please split the audio again."}

    _name = (project_name or pending["project_name"]).strip() or "Kirtan Recording"
    cover_data = sc_cover_art.pop(split_job_id, None)
    upload_job_id = uuid.uuid4().hex[:12]
    sc_latest_upload_job_id = upload_job_id

    thread = threading.Thread(
        target=_sc_upload_worker,
        args=(upload_job_id, pending["zip_data"], pending["track_names"],
              pending["file_ext"], pending["project_name"], _name, cover_data),
        daemon=True,
    )
    thread.start()
    return {"upload_job_id": upload_job_id, "split_job_id": split_job_id}


@app.route("/soundcloud/start")
def sc_start():
    """New-tab entry point: authenticate if needed, then immediately start upload."""
    job_id = request.args.get("job_id", "").strip()
    project_name = request.args.get("project_name", "").strip()

    if not job_id:
        return _sc_error_page("No split job ID provided. Please go back and split your audio first.")

    if sc_access_token:
        result = _sc_start_upload(job_id, project_name)
        if "error" in result:
            return _sc_error_page(result["error"])
        return """<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;
             display:flex;flex-direction:column;align-items:center;justify-content:center;
             min-height:100vh;margin:0;background:#fff;color:#1a1a2a;text-align:center;padding:2rem">
<div style="width:52px;height:52px;background:#ff5500;border-radius:14px;display:flex;
            align-items:center;justify-content:center;margin-bottom:1rem;
            box-shadow:0 6px 20px rgba(255,85,0,.3)">
  <svg viewBox="0 0 24 24" fill="none" width="26" height="26">
    <path stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"
          d="M12 15.5V7M9 10l3-3 3 3M5.5 19.5h13"/>
  </svg>
</div>
<p style="font-weight:700;font-size:1.05rem;margin-bottom:.3rem">Upload started!</p>
<p style="color:#6b7280;font-size:.85rem">You can close this window.</p>
<script>setTimeout(() => window.close(), 1200);</script>
</body></html>"""

    # Not yet authenticated — redirect through OAuth, carrying job_id + project_name
    from flask import url_for
    return redirect(url_for("sc_login", job_id=job_id, project_name=project_name))


@app.route("/soundcloud/login")
def sc_login():
    """Initiate SoundCloud OAuth 2.1 PKCE flow."""
    job_id = request.args.get("job_id", "").strip()
    project_name = request.args.get("project_name", "").strip()

    if not SC_CLIENT_ID or not SC_CLIENT_SECRET:
        return _sc_error_page(
            "SoundCloud credentials not configured. "
            "Add SC_CLIENT_ID and SC_CLIENT_SECRET to your .env file."
        )

    state = secrets.token_urlsafe(16)
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()

    sc_pkce_states[state] = {
        "code_verifier": code_verifier,
        "job_id": job_id,
        "project_name": project_name,
    }

    auth_url = (
        "https://secure.soundcloud.com/authorize"
        f"?client_id={SC_CLIENT_ID}"
        f"&redirect_uri={SC_REDIRECT_URI}"
        "&response_type=code"
        f"&code_challenge={code_challenge}"
        "&code_challenge_method=S256"
        f"&state={state}"
        "&scope=*"
    )
    return redirect(auth_url)


@app.route("/soundcloud/callback")
def sc_callback():
    """Handle SoundCloud OAuth callback, exchange code for token, start upload."""
    global sc_access_token

    code = request.args.get("code")
    state = request.args.get("state")

    if not code or not state:
        return _sc_error_page("No authorization code received from SoundCloud.")

    state_data = sc_pkce_states.pop(state, None)
    if not state_data:
        return _sc_error_page("Invalid or expired OAuth state. Please try again.")

    # Support both old (plain string) and new (dict) state formats
    if isinstance(state_data, str):
        code_verifier = state_data
        job_id = ""
        project_name = ""
    else:
        code_verifier = state_data["code_verifier"]
        job_id = state_data.get("job_id", "")
        project_name = state_data.get("project_name", "")

    try:
        token_resp = http_requests.post(
            "https://secure.soundcloud.com/oauth/token",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json;charset=utf-8",
                "User-Agent": "Mozilla/5.0 (compatible; SSASoundsSplitter/1.0)",
            },
            data={
                "grant_type": "authorization_code",
                "client_id": SC_CLIENT_ID,
                "client_secret": SC_CLIENT_SECRET,
                "redirect_uri": SC_REDIRECT_URI,
                "code": code,
                "code_verifier": code_verifier,
            },
            timeout=20,
        )
        token_resp.raise_for_status()
        sc_access_token = token_resp.json()["access_token"]
    except Exception as e:
        return _sc_error_page(f"Authentication failed: {str(e)[:300]}")

    if job_id:
        result = _sc_start_upload(job_id, project_name)
        if "error" in result:
            return _sc_error_page(result["error"])

    return """<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;
             display:flex;flex-direction:column;align-items:center;justify-content:center;
             min-height:100vh;margin:0;background:#fff;color:#1a1a2a;text-align:center;padding:2rem">
<div style="width:52px;height:52px;background:#ff5500;border-radius:14px;display:flex;
            align-items:center;justify-content:center;margin-bottom:1rem;
            box-shadow:0 6px 20px rgba(255,85,0,.3)">
  <svg viewBox="0 0 24 24" fill="none" width="26" height="26">
    <path stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"
          d="M12 15.5V7M9 10l3-3 3 3M5.5 19.5h13"/>
  </svg>
</div>
<p style="font-weight:700;font-size:1.05rem;margin-bottom:.3rem">Connected!</p>
<p style="color:#6b7280;font-size:.85rem">Upload started. You can close this window.</p>
<script>setTimeout(() => window.close(), 1200);</script>
</body></html>"""


@app.route("/soundcloud/trigger-upload")
def sc_trigger_upload():
    """Trigger upload if already authenticated; otherwise tell the client to do OAuth."""
    if not sc_access_token:
        return jsonify({"status": "needs_auth"})
    job_id = request.args.get("job_id", "").strip()
    project_name = request.args.get("project_name", "").strip()
    if not job_id:
        return jsonify({"error": "No job ID"}), 400
    result = _sc_start_upload(job_id, project_name)
    if "error" in result:
        return jsonify({"status": "error", "error": result["error"]}), 400
    return jsonify({"status": "started", "upload_job_id": result["upload_job_id"]})


@app.route("/soundcloud/auth-status")
def sc_auth_status():
    return jsonify({"authenticated": sc_access_token is not None})


@app.route("/soundcloud/set-cover/<split_job_id>", methods=["POST"])
def sc_set_cover(split_job_id):
    """Store cover art image for a split job (called before opening upload tab)."""
    img = request.files.get("cover")
    if not img:
        return jsonify({"error": "No image provided"}), 400
    sc_cover_art[split_job_id] = img.read()
    return jsonify({"status": "ok"})


@app.route("/soundcloud/cover/<split_job_id>")
def sc_cover_image(split_job_id):
    """Serve stored cover art (used by the progress tab to show preview)."""
    data = sc_cover_art.get(split_job_id)
    if not data:
        return "", 404
    return data, 200, {"Content-Type": "image/jpeg", "Cache-Control": "no-store"}


@app.route("/soundcloud/latest-upload")
def sc_latest_upload_endpoint():
    """Return the most recent upload job's progress (polled by the main page)."""
    if not sc_latest_upload_job_id:
        return jsonify({"status": "none"})
    prog = sc_upload_progress.get(sc_latest_upload_job_id, {})
    return jsonify({"upload_job_id": sc_latest_upload_job_id, **prog})


def _sc_upload_worker(upload_job_id, zip_data, track_names, file_ext, zip_folder, project_name,
                      cover_data=None):
    """Upload each split track to SoundCloud, then create a dated playlist/set."""
    import time as _time
    from datetime import datetime, timezone
    token = sc_access_token
    total = len(track_names)
    sc_upload_progress[upload_job_id] = {"status": "uploading", "completed": 0, "total": total}
    headers = {"Authorization": f"OAuth {token}"}
    track_ids = []

    try:
        mime = "audio/mpeg" if file_ext == ".mp3" else "audio/mp4"
        with zipfile.ZipFile(BytesIO(zip_data)) as zf:
            for i, name in enumerate(track_names):
                zip_entry = f"{zip_folder}/{name}{file_ext}"
                filename = f"{name}{file_ext}"
                try:
                    audio_data = zf.read(zip_entry)
                except KeyError:
                    raise RuntimeError(f"Track file '{zip_entry}' not found in archive")

                upload_resp = http_requests.post(
                    "https://api.soundcloud.com/tracks",
                    headers=headers,
                    files={"track[asset_data]": (filename, audio_data, mime)},
                    data={"track[title]": name, "track[sharing]": "public"},
                    timeout=600,
                )
                upload_resp.raise_for_status()
                track_ids.append(int(upload_resp.json()["id"]))
                sc_upload_progress[upload_job_id]["completed"] = i + 1

        # Brief pause to let SoundCloud begin transcoding before playlist creation
        _time.sleep(2)

        # Today's date for the release_date field (ISO 8601, UTC)
        release_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Create playlist — use multipart when we have cover art, JSON otherwise
        playlist_url = ""
        try:
            if cover_data:
                # Multipart form (required to attach artwork)
                form_data = (
                    [("playlist[title]", project_name),
                     ("playlist[release_date]", release_date)]
                    + [("playlist[tracks][][id]", str(tid)) for tid in track_ids]
                )
                pl_resp = http_requests.post(
                    "https://api.soundcloud.com/playlists",
                    headers=headers,
                    files={"playlist[artwork_data]": ("cover.jpg", cover_data, "image/jpeg")},
                    data=form_data,
                    timeout=60,
                )
            else:
                # JSON (no artwork)
                pl_resp = http_requests.post(
                    "https://api.soundcloud.com/playlists",
                    headers={**headers, "Content-Type": "application/json; charset=utf-8"},
                    json={"playlist": {"title": project_name,
                                       "release_date": release_date,
                                       "tracks": [{"id": tid} for tid in track_ids]}},
                    timeout=30,
                )

            if pl_resp.ok:
                playlist_url = pl_resp.json().get("permalink_url", "")
            else:
                # Fallback: plain form-encoded without artwork
                form_data2 = (
                    [("playlist[title]", project_name),
                     ("playlist[release_date]", release_date)]
                    + [("playlist[tracks][][id]", str(tid)) for tid in track_ids]
                )
                pl_resp2 = http_requests.post(
                    "https://api.soundcloud.com/playlists",
                    headers=headers,
                    data=form_data2,
                    timeout=30,
                )
                if pl_resp2.ok:
                    playlist_url = pl_resp2.json().get("permalink_url", "")
        except Exception:
            pass  # Graceful degradation — tracks are uploaded even if playlist fails

        sc_upload_progress[upload_job_id] = {
            "status": "done",
            "completed": total,
            "total": total,
            "playlist_url": playlist_url or "https://soundcloud.com/you/tracks",
        }

    except Exception as e:
        sc_upload_progress[upload_job_id] = {"status": "error", "error": str(e)}


@app.route("/soundcloud/upload/<split_job_id>", methods=["POST"])
def sc_upload_endpoint(split_job_id):
    """Legacy endpoint kept for backward compatibility."""
    if not sc_access_token:
        return jsonify({"error": "Not authenticated with SoundCloud. Please connect first."}), 401

    data = request.get_json() or {}
    project_name = data.get("project_name", "")
    result = _sc_start_upload(split_job_id, project_name)
    if "error" in result:
        return jsonify({"error": result["error"]}), 404
    return jsonify({"upload_job_id": result["upload_job_id"]})


@app.route("/soundcloud/upload-progress/<upload_job_id>")
def sc_upload_progress_endpoint(upload_job_id):
    prog = sc_upload_progress.get(upload_job_id)
    if not prog:
        return jsonify({"status": "unknown"}), 404
    return jsonify(prog)


# ── Google Drive ──────────────────────────────────────────────────────────────

def _gd_error_page(msg):
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="font-family:system-ui;display:flex;flex-direction:column;align-items:center;
             justify-content:center;height:100vh;margin:0;background:#f9fafb;">
  <p style="font-weight:700;font-size:1.1rem;color:#dc2626;">Google Drive Error</p>
  <p style="color:#6b7280;font-size:0.9rem;max-width:360px;text-align:center;">{msg}</p>
  <button onclick="window.close()" style="margin-top:1rem;padding:0.5rem 1.2rem;
          border:1px solid #e5e7eb;border-radius:8px;cursor:pointer;">Close</button>
</body></html>"""


def _gd_start_upload(split_job_id, project_name):
    """Load split data and kick off the Google Drive upload worker."""
    global gd_latest_upload_job_id
    pending = sc_pending.get(split_job_id)  # reuse the same zip already in memory
    if not pending:
        zip_path  = os.path.join(SC_ZIP_DIR, f"{split_job_id}.zip")
        meta_path = os.path.join(SC_ZIP_DIR, f"{split_job_id}.json")
        if os.path.exists(zip_path) and os.path.exists(meta_path):
            try:
                with open(zip_path, "rb") as fz:
                    zip_data = fz.read()
                with open(meta_path) as fm:
                    meta = json.load(fm)
                pending = {**meta, "zip_data": zip_data}
                sc_pending[split_job_id] = pending
            except Exception as e:
                return {"error": f"Could not load split data: {e}"}
        else:
            return {"error": "Split data not found. Please split the audio again."}

    _name = (project_name or pending["project_name"]).strip() or "Recording"
    upload_job_id = uuid.uuid4().hex[:12]
    gd_latest_upload_job_id = upload_job_id

    thread = threading.Thread(
        target=_gd_upload_worker,
        args=(upload_job_id, pending["zip_data"], _name),
        daemon=True,
    )
    thread.start()
    return {"upload_job_id": upload_job_id}


def _gd_upload_worker(upload_job_id, zip_data, project_name):
    """Create a Drive folder named after the project, upload each track individually."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload

    gd_upload_progress[upload_job_id] = {"status": "uploading", "pct": 5, "completed": 0, "total": 0}
    try:
        creds = Credentials(
            token=gd_access_token,
            client_id=GD_CLIENT_ID,
            client_secret=GD_CLIENT_SECRET,
            token_uri="https://oauth2.googleapis.com/token",
        )
        service = build("drive", "v3", credentials=creds, cache_discovery=False)

        # Create a folder named after the project
        folder = service.files().create(
            body={"name": project_name, "mimeType": "application/vnd.google-apps.folder"},
            fields="id,webViewLink",
        ).execute()
        folder_id  = folder["id"]
        folder_url = folder.get("webViewLink", "https://drive.google.com/drive/my-drive")

        # Extract and upload each track individually
        with zipfile.ZipFile(BytesIO(zip_data)) as zf:
            # Only upload audio files, skip directories
            entries = [e for e in zf.infolist() if not e.is_dir()]
            total = len(entries)
            gd_upload_progress[upload_job_id].update({"pct": 10, "total": total})

            for i, entry in enumerate(entries):
                filename = os.path.basename(entry.filename)
                if not filename:
                    continue
                ext = os.path.splitext(filename)[1].lower()
                mime = "audio/mpeg" if ext == ".mp3" else "audio/mp4" if ext in (".mp4", ".m4a") else "application/octet-stream"

                file_bytes = zf.read(entry.filename)
                media = MediaIoBaseUpload(BytesIO(file_bytes), mimetype=mime, resumable=True)
                service.files().create(
                    body={"name": filename, "parents": [folder_id]},
                    media_body=media,
                    fields="id",
                ).execute()

                pct = 10 + int((i + 1) / total * 88)
                gd_upload_progress[upload_job_id].update({"pct": pct, "completed": i + 1})

        gd_upload_progress[upload_job_id] = {
            "status": "done",
            "pct": 100,
            "folder_url": folder_url,
        }
    except Exception as e:
        gd_upload_progress[upload_job_id] = {"status": "error", "error": str(e)}


@app.route("/gdrive/login")
def gd_login():
    job_id       = request.args.get("job_id", "").strip()
    project_name = request.args.get("project_name", "").strip()

    if not GD_CLIENT_ID or not GD_CLIENT_SECRET:
        return _gd_error_page("Google Drive credentials not configured. "
                              "Set GD_CLIENT_ID and GD_CLIENT_SECRET in your .env file.")

    state = secrets.token_urlsafe(16)
    gd_oauth_states[state] = {"job_id": job_id, "project_name": project_name}

    scope    = "https://www.googleapis.com/auth/drive.file"
    auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={url_quote(GD_CLIENT_ID)}"
        f"&redirect_uri={url_quote(GD_REDIRECT_URI)}"
        "&response_type=code"
        f"&scope={url_quote(scope)}"
        f"&state={state}"
        "&access_type=offline"
        "&prompt=consent"
    )
    return redirect(auth_url)


@app.route("/gdrive/callback")
def gd_callback():
    global gd_access_token

    error = request.args.get("error")
    if error:
        return _gd_error_page(f"Google Drive authorization was denied: {error}")

    code  = request.args.get("code")
    state = request.args.get("state")
    if not code or not state:
        return _gd_error_page("No authorization code received from Google.")

    state_data = gd_oauth_states.pop(state, None)
    if not state_data:
        return _gd_error_page("Invalid or expired OAuth state. Please try again.")

    try:
        token_resp = http_requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": GD_CLIENT_ID,
                "client_secret": GD_CLIENT_SECRET,
                "redirect_uri": GD_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            timeout=20,
        )
        token_resp.raise_for_status()
        gd_access_token = token_resp.json()["access_token"]
    except Exception as e:
        return _gd_error_page(f"Authentication failed: {str(e)[:300]}")

    job_id       = state_data.get("job_id", "")
    project_name = state_data.get("project_name", "")
    if job_id:
        result = _gd_start_upload(job_id, project_name)
        if "error" in result:
            return _gd_error_page(result["error"])

    return """<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="font-family:system-ui;display:flex;flex-direction:column;align-items:center;
             justify-content:center;height:100vh;margin:0;background:#f9fafb;">
  <p style="font-weight:700;font-size:1.1rem;color:#1a1a2a;">Connected to Google Drive!</p>
  <p style="color:#6b7280;font-size:0.9rem;">Upload started. You can close this window.</p>
  <script>setTimeout(() => window.close(), 1200);</script>
</body></html>"""


@app.route("/gdrive/trigger-upload")
def gd_trigger_upload():
    if not gd_access_token:
        return jsonify({"status": "needs_auth"})
    job_id       = request.args.get("job_id", "").strip()
    project_name = request.args.get("project_name", "").strip()
    if not job_id:
        return jsonify({"error": "No job ID"}), 400
    result = _gd_start_upload(job_id, project_name)
    if "error" in result:
        return jsonify({"status": "error", "error": result["error"]}), 400
    return jsonify({"status": "started", "upload_job_id": result["upload_job_id"]})


@app.route("/gdrive/latest-upload")
def gd_latest_upload():
    if not gd_latest_upload_job_id:
        return jsonify({"status": "none"})
    prog = gd_upload_progress.get(gd_latest_upload_job_id, {})
    return jsonify({"upload_job_id": gd_latest_upload_job_id, **prog})


@app.route("/gdrive/auth-status")
def gd_auth_status():
    return jsonify({"authenticated": gd_access_token is not None})


if __name__ == "__main__":
    app.run(
        debug=True,
        port=5001,
        exclude_patterns=["uploads/*", "yt_downloads/*", ".env"],
    )
