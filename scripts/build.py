#!/usr/bin/env python3
"""LATS Radio nightly build.

Reads the clip sheet, downloads each clip's audio section, normalizes loudness,
speaks the intro, joins them into one MP3 per item, writes episode.json and
feed.xml, prunes old audio, and pushes to GitHub Pages if anything changed.

Run:  ~/lats-radio/.venv/bin/python ~/lats-radio/scripts/build.py [--no-push]
"""
import csv
import datetime as dt
import difflib
import fcntl
import glob
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import ssl
import string
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".work"
LOGS = ROOT / "logs"
CONFIG = ROOT / "config.local.json"
SHEET_ID = "1k78ZRFYSUAk-btLm5SWvv2-kU7NZ1RViqzb9gY4uWao"
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv"
SITE = "https://brayholcomb-max.github.io/lats-radio/"
ET = ZoneInfo("America/New_York")
PIPELINE = "v3"          # bump to force every clip to be rebuilt
TARGET_LUFS = -16.0
GAP = 0.7                # seconds of silence between spoken intro and clip
FADE = 0.5
PAD_BEFORE, PAD_AFTER = 6.0, 8.0   # extra audio fetched around a clip so cuts can land on a pause
KEEP_DAYS = 3
NO_PUSH = "--no-push" in sys.argv
LESSON_DIRS = [Path(os.environ.get("LATS_LESSON_DIR") or Path.home() / "Downloads")]   # where the nightly task's browser saves the NotebookLM audio
LESSON_EXT = {".m4a", ".mp3", ".wav", ".aac", ".ogg", ".opus", ".flac"}
LESSON_MAX_AGE = 36 * 3600   # a lesson file older than this belongs to an earlier night

try:                     # python.org Python ships without root certificates
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except Exception:
    SSL_CTX = ssl.create_default_context()

os.environ["PATH"] = os.pathsep.join(
    [str(Path.home() / ".local/bin"), "/usr/local/bin", "/opt/homebrew/bin",
     os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")])


def log(msg):
    line = f"{dt.datetime.now(ET):%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        LOGS.mkdir(exist_ok=True)
        with open(LOGS / "build.log", "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def run(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)


def ffmpeg_exe():
    for name in ("ffmpeg",):
        p = shutil.which(name)
        if p:
            return p
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


FF = ffmpeg_exe()
YTDLP = str(Path(sys.executable).parent / "yt-dlp")
if not Path(YTDLP).exists():
    YTDLP = shutil.which("yt-dlp") or "yt-dlp"


# ---------- config ----------

def audio_dir_name():
    if CONFIG.exists():
        name = json.loads(CONFIG.read_text()).get("audio_dir")
        if name:
            return name
    for p in ROOT.iterdir():            # config lost: reuse the folder already in the repo
        if p.is_dir() and re.fullmatch(r"[a-z0-9]{24}", p.name):
            name = p.name
            break
    else:
        name = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(24))
    CONFIG.write_text(json.dumps({"audio_dir": name}, indent=2) + "\n")
    return name


# ---------- sheet ----------

def fetch_sheet():
    local = os.environ.get("LATS_SHEET_CSV")     # tests only: read rows from a local file
    if local:
        return Path(local).read_text(encoding="utf-8-sig")
    last = None
    for attempt in range(6):
        try:
            req = urllib.request.Request(SHEET_URL, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
                body = r.read().decode("utf-8-sig")
            if body.lstrip().lower().startswith(("<!doctype", "<html")):
                raise RuntimeError("sheet returned a sign-in page (not link-shared)")
            return body
        except Exception as e:           # network may not be up right after wake
            last = e
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"could not fetch sheet: {last}")


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "clip"


def read_rows(body):
    rows, seen = [], set()
    for n, r in enumerate(csv.DictReader(io.StringIO(body)), start=2):
        r = {(k or "").strip(): (v.strip() if isinstance(v, str) else "") for k, v in r.items()}
        vid = r.get("videoId", "")
        if r.get("source", "").lower().startswith("lesson") or vid.upper() in ("NOTEBOOKLM", "LESSON"):
            # a NotebookLM lesson: its audio is a file the nightly task downloaded, not a YouTube section
            def num(v):
                try:
                    return float(v)
                except ValueError:
                    return 0.0
            rid = slugify(r.get("id") or f"lesson-{n}")
            while rid in seen:
                rid += "-x"
            seen.add(rid)
            r.update(id=rid, kind="lesson", videoId="NOTEBOOKLM",
                     start=num(r.get("start", "")), end=num(r.get("end", "")))
            rows.append(r)
            continue
        try:
            start, end = float(r.get("start", "")), float(r.get("end", ""))
        except ValueError:
            log(f"SKIP row {n}: start/end not numbers ({r.get('id', '')})")
            continue
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
            log(f"SKIP row {n}: bad videoId {vid!r} ({r.get('id', '')})")
            continue
        if not end > start:
            log(f"SKIP row {n}: end is not after start ({r.get('id', '')})")
            continue
        rid = slugify(r.get("id") or f"row-{n}")
        while rid in seen:
            rid += "-x"
        seen.add(rid)
        r.update(id=rid, start=start, end=end)
        rows.append(r)
    return rows


# ---------- audio ----------

def duration(path):
    """Decoded duration in seconds."""
    p = run([FF, "-hide_banner", "-nostdin", "-i", path, "-f", "null", "-"])
    times = re.findall(r"time=(\d+):(\d+):(\d+\.\d+)", p.stderr)
    if not times:
        raise RuntimeError(f"cannot read duration of {path}")
    h, m, s = times[-1]
    return int(h) * 3600 + int(m) * 60 + float(s)


def loudness(path):
    """Integrated loudness (LUFS)."""
    p = run([FF, "-hide_banner", "-nostdin", "-i", path, "-af", "ebur128", "-f", "null", "-"])
    m = re.findall(r"I:\s+(-?\d+(?:\.\d+)?) LUFS", p.stderr)
    return float(m[-1]) if m else float("nan")


def normalize(src, dst, fade, trim=None, fade_in=FADE, fade_out=FADE):
    """Mono, optional trim, two-pass loudnorm to TARGET_LUFS, optional fades, 44.1 kHz FLAC."""
    pre = "aformat=channel_layouts=mono"
    if trim:
        pre += f",atrim=start={trim[0]:.3f}:end={trim[1]:.3f},asetpts=PTS-STARTPTS"
    base = f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11"
    p = run([FF, "-hide_banner", "-nostdin", "-i", src, "-af",
             f"{pre},{base}:print_format=json", "-f", "null", "-"])
    ln = base
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", p.stderr, re.S)
    if m:
        j = json.loads(m.group(0))
        if "inf" not in (j["input_i"] + j["input_tp"] + j["input_thresh"]):
            ln = (f"{base}:measured_I={j['input_i']}:measured_TP={j['input_tp']}"
                  f":measured_LRA={j['input_lra']}:measured_thresh={j['input_thresh']}"
                  f":offset={j['target_offset']}:linear=true")
    filters = [pre, ln]
    if fade:
        d = (trim[1] - trim[0]) if trim else duration(src)
        filters += [f"afade=t=in:st=0:d={fade_in:.2f}",
                    f"afade=t=out:st={max(0, d - fade_out):.3f}:d={fade_out:.2f}"]
    filters.append("aresample=44100")
    p = run([FF, "-hide_banner", "-nostdin", "-y", "-i", src, "-af", ",".join(filters),
             "-ac", "1", "-c:a", "flac", dst])
    if p.returncode != 0 or not Path(dst).exists():
        raise RuntimeError(f"ffmpeg normalize failed: {p.stderr[-400:]}")


_whisper = None
_wlock = threading.Lock()


def listen(path, ss, t):
    """Speech recognition plus silence detection for a window of an audio file.
    Returns (words, silences) in absolute seconds, or (None, None) if unavailable.
    words = [(text, start, end)], silences = [(start, end)] for quiet runs of 0.3 s or more."""
    global _whisper
    try:
        import numpy as np
        from faster_whisper import WhisperModel
    except Exception:
        return None, None
    with _wlock:
        try:
            if _whisper is None:
                _whisper = WhisperModel("small.en", device="cpu", compute_type="int8")
            pcm = subprocess.run([FF, "-loglevel", "error", "-ss", f"{ss:.3f}", "-t", f"{t:.3f}", "-i", str(path),
                                  "-f", "s16le", "-ar", "16000", "-ac", "1", "-"],
                                 capture_output=True, check=True).stdout
            audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
            segs, _ = _whisper.transcribe(audio, language="en", word_timestamps=True, beam_size=5,
                                          vad_filter=False, condition_on_previous_text=False)
            words = [(w.word.strip(), ss + float(w.start), ss + float(w.end)) for sg in segs for w in (sg.words or [])]
            n = len(audio) // 320                                   # 20 ms frames
            rms = np.sqrt((audio[:n * 320].reshape(n, 320) ** 2).mean(axis=1) + 1e-12)
            quiet = rms < max(float(np.percentile(rms, 90)) * 0.06, 0.002)
            sil, start = [], None
            for i, q in enumerate(list(quiet) + [False]):
                if q and start is None:
                    start = i
                elif not q and start is not None:
                    if (i - start) * 0.02 >= 0.3:
                        sil.append((ss + start * 0.02, ss + i * 0.02))
                    start = None
            return words, sil
        except Exception as ex:
            log(f"speech check unavailable ({ex}); using the sheet's exact times")
            return None, None


def snap(raw, t0, t1, total):
    """Move the sheet's start/end (t0, t1, seconds into raw) to a nearby sentence boundary or pause,
    so a clip does not begin or end in the middle of a sentence.
    Returns (start, end, fade_in, fade_out, note)."""
    a, b, fin, fout = t0, min(t1, total), FADE, FADE

    # ---- start: the beginning of a sentence near t0 (at most 4 s earlier or 6 s later)
    w0 = max(0.0, t0 - PAD_BEFORE)
    words, sil = listen(raw, w0, min(total, t0 + 10.0) - w0)
    if words:
        cands = [(st, 0.0) for i, (w, st, en) in enumerate(words) if i > 0 and words[i - 1][0][-1:] in ".?!"]
        cands += [(se, 0.8) for ss_, se in sil if se - ss_ >= 0.5 and se < min(total, t0 + 10.0) - 0.3]
        cands += [(st, 0.6) for i, (w, st, en) in enumerate(words) if i > 0 and st - words[i - 1][2] >= 0.35]
        cands = [c for c in cands if max(0.0, t0 - 4.0) <= c[0] <= t0 + 6.0]
        if not cands:                                               # last resort: at least not mid-word
            cands = [(st, 0.0) for w, st, en in words if t0 - 1.5 <= st <= t0 + 1.5]
        if cands:
            st = min(cands, key=lambda c: abs(c[0] - t0) + c[1])[0]
            lead = [x for x in sil if x[1] >= st - 0.25 and x[0] < st]        # quiet just before the sentence
            a = max(lead[-1][0], st - FADE, 0.0) if lead else max(st - 0.12, 0.0)
            fin = min(FADE, max(0.1, st - a))

    # ---- end: the end of a sentence near t1 (at most 4 s earlier, or as late as the padding allows)
    w1 = max(0.0, t1 - 10.0)
    words, sil = listen(raw, w1, total - w1)
    if words:
        cands = [(en, 0.0) for i, (w, st, en) in enumerate(words) if w[-1:] in ".?!" and i + 1 < len(words)]
        cands += [(ss_, 0.8) for ss_, se in sil if se - ss_ >= 0.5 and ss_ > w1 + 0.3]
        cands = [c for c in cands if max(a + 20.0, t1 - 4.0) <= c[0] <= total - 0.2]
        if not cands:                                               # last resort: at least not mid-word
            cands = [(en, 0.0) for i, (w, st, en) in enumerate(words) if i + 1 < len(words) and t1 - 1.5 <= en <= t1 + 1.5]
        if cands:      # prefer not to end earlier than the sheet says
            en = min(cands, key=lambda c: abs(c[0] - t1) + c[1] + (0.5 if c[0] < t1 - 0.5 else 0.0))[0]
            tail = [x for x in sil if x[0] <= en + 0.3 and x[1] > en]         # quiet just after the sentence
            b = min(max(tail[0][1], en + 0.15), en + FADE, total) if tail else min(en + 0.15, total)
            fout = min(FADE, max(0.1, b - en - 0.03))
    return a, b, fin, fout, f"start {a - t0:+.1f}s, end {b - t1:+.1f}s"


def download_clip(r):
    """Download one section and return the processed FLAC path."""
    s, e = r["start"], r["end"]
    stem = f"{r['videoId']}-{s:g}-{e:g}"
    out = WORK / f"clip-{stem}-{PIPELINE}.flac"
    if out.exists():
        return out
    ds, de = max(0.0, s - PAD_BEFORE), e + PAD_AFTER
    want = de - ds
    last = ""
    for attempt in range(3):
        for old in glob.glob(str(WORK / f"raw-{stem}.*")):
            os.remove(old)
        cmd = [YTDLP, "--no-playlist", "-f", "bestaudio/best",
               "--download-sections", f"*{ds:g}-{de:g}", "--force-keyframes-at-cuts",   # exact cuts
               "--ffmpeg-location", FF, "--retries", "5", "--fragment-retries", "5",
               "--socket-timeout", "30", "--no-progress", "-q", "--no-warnings",
               "-o", str(WORK / f"raw-{stem}.%(ext)s"),
               f"https://www.youtube.com/watch?v={r['videoId']}"]
        node = shutil.which("node")
        if node:
            cmd[1:1] = ["--js-runtimes", f"node:{node}"]
        p = run(cmd, timeout=900)
        raws = [x for x in glob.glob(str(WORK / f"raw-{stem}.*")) if not x.endswith(".part")]
        if p.returncode == 0 and raws:
            got = duration(raws[0])
            if (e - ds) - 2.0 <= got <= want + 2.0:      # the video may end before the padding does
                a, b, fin, fout, note = snap(raws[0], s - ds, e - ds, got)
                normalize(raws[0], out, fade=True, trim=(a, b), fade_in=fin, fade_out=fout)
                out.with_suffix(".note").write_text(note)
                os.remove(raws[0])
                return out
            last = f"got {got:.1f}s, wanted {want:.1f}s"
        else:
            last = (p.stderr or p.stdout).strip()[-300:]
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(last or "download failed")


def _plain(s):
    return re.sub(r"[^a-z0-9]+", "", re.sub(r"\s*\(\d+\)$", "", s).lower())


def find_lesson_file(r):
    """The downloaded NotebookLM audio for a lesson row, or None.

    The row's `channel` holds the Audio Overview's title exactly as NotebookLM shows it
    (the download is named after it); `end` holds its length in seconds. A file matches
    by name, or failing that by length if it was downloaded in the last 14 hours.
    """
    want = _plain(r.get("channel", ""))
    secs = r["end"] - r["start"]
    now = time.time()
    best = None
    for d in LESSON_DIRS:
        try:
            files = [f for f in d.iterdir() if f.suffix.lower() in LESSON_EXT]
        except OSError as e:
            log(f"cannot read {d}: {e}")
            continue
        for f in files:
            try:
                st = f.stat()
            except OSError:
                continue
            age = now - st.st_mtime
            if age > LESSON_MAX_AGE or age < 20 or st.st_size < 500_000:
                continue
            have = _plain(f.stem)
            named = len(want) >= 8 and len(have) >= 8 and (
                have.startswith(want[:24]) or want.startswith(have[:24])
                or difflib.SequenceMatcher(None, have, want).ratio() >= 0.8)
            timed = False
            if not named and secs >= 120 and age < 14 * 3600:
                try:
                    timed = abs(duration(f) - secs) <= 20
                except Exception:
                    timed = False
            if named or timed:
                rank = (named, st.st_mtime)
                if best is None or rank > best[0]:
                    best = (rank, f)
    return best[1] if best else None


def lesson_clip(r):
    """Processed FLAC for a lesson row. The source file is copied into the work cache,
    so a rebuild still works if the download is later moved or renamed."""
    ident = hashlib.sha1(json.dumps([r.get("channel", ""), r.get("url", ""), r["end"]]).encode()).hexdigest()[:8]
    src = find_lesson_file(r)
    if src is not None:
        st = src.stat()
        h = hashlib.sha1(f"{src.name}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:8]
        cached = WORK / f"lesson-{ident}-{h}{src.suffix.lower()}"
        if not cached.exists():
            shutil.copy2(src, cached)
            os.utime(cached, None)
            log(f"lesson {r['id']}: using {src.name} ({st.st_size / 1e6:.1f} MB)")
    else:
        old = sorted((f for f in WORK.glob(f"lesson-{ident}-*") if f.suffix.lower() in LESSON_EXT - {".flac"}),
                     key=lambda f: f.stat().st_mtime)
        if not old:
            raise RuntimeError("lesson audio is not in the Downloads folder yet "
                               f"(looking for an audio file named like {r.get('channel', '')!r})")
        cached = old[-1]
        h = cached.stem.rsplit("-", 1)[-1]
    out = WORK / f"lessonclip-{ident}-{h}-{PIPELINE}.flac"
    if not out.exists():
        got = duration(cached)
        if got < 60:
            raise RuntimeError(f"lesson audio is only {got:.0f}s long")
        normalize(cached, out, fade=True, fade_in=0.3, fade_out=0.8)
        out.with_suffix(".note").write_text(f"lesson {h}")
    return out


def get_audio(r):
    return lesson_clip(r) if r.get("kind") == "lesson" else download_clip(r)


KOKORO_MODEL = ROOT / ".models" / "kokoro-v1.0.onnx"
KOKORO_VOICES = ROOT / ".models" / "voices-v1.0.bin"
KOKORO_VOICE = "af_heart"
_kokoro = None


def kokoro():
    global _kokoro
    if _kokoro is None:
        from kokoro_onnx import Kokoro
        _kokoro = Kokoro(str(KOKORO_MODEL), str(KOKORO_VOICES))
    return _kokoro


def pick_voice():
    """Neural voice (Kokoro, local and free) if installed, else the best Mac voice."""
    if KOKORO_MODEL.exists() and KOKORO_VOICES.exists():
        try:
            import kokoro_onnx, soundfile  # noqa: F401
            return f"kokoro:{KOKORO_VOICE}"
        except Exception:
            pass
    return mac_voice()


def mac_voice():
    out = run(["say", "-v", "?"]).stdout
    voices = []
    for line in out.splitlines():
        m = re.match(r"^(.+?)\s+([a-z]{2}_[A-Z]{2})\s+#", line)
        if m and m.group(2).startswith("en_"):
            voices.append((m.group(1).strip(), m.group(2)))
    prefer = ["Ava", "Zoe", "Evan", "Nathan", "Allison", "Samantha", "Susan", "Tom", "Alex",
              "Karen", "Daniel", "Moira", "Tessa"]

    def rank(v):
        name, loc = v
        tier = 0 if "(Premium)" in name else 1 if "(Enhanced)" in name else 2 if name.startswith("Siri") else 3
        base = re.sub(r"\s*\(.*\)", "", name)
        pref = prefer.index(base) if base in prefer else 99
        return (tier, 0 if loc == "en_US" else 1, pref, name)

    voices.sort(key=rank)
    return voices[0][0] if voices and rank(voices[0])[2] < 99 else "Samantha"


def speakable(text):
    text = re.sub(r"\$(\d+(?:\.\d+)?)\s?M\b", r"\1 million dollars", text)
    text = re.sub(r"\$(\d+(?:\.\d+)?)\s?[Kk]\b", r"\1 thousand dollars", text)
    text = re.sub(r"\$(\d+(?:\.\d+)?)\s?B\b", r"\1 billion dollars", text)
    text = re.sub(r"\$(\d[\d,]*(?:\.\d+)?)\s+(thousand|million|billion)", r"\1 \2 dollars", text)
    text = re.sub(r"(\d)\s?%", r"\1 percent", text)
    text = re.sub(r"\bvs\.?(?=\s)", "versus", text)
    text = text.replace("~", "about ").replace("e.g.", "for example").replace("i.e.", "that is")
    return text.replace("&", " and ")


def speak(text, voice):
    key = hashlib.sha1(f"{voice}|{text}|{PIPELINE}".encode()).hexdigest()[:16]
    out = WORK / f"say-{key}.flac"
    if out.exists():
        return out
    if voice.startswith("kokoro:"):
        try:
            import soundfile as sf
            audio, sr = kokoro().create(speakable(text), voice=voice.split(":", 1)[1], speed=1.0, lang="en-us")
            wav = WORK / f"say-{key}.wav"
            sf.write(str(wav), audio, sr)
            normalize(wav, out, fade=False)
            wav.unlink()
            return out
        except Exception as e:
            log(f"neural voice failed ({e}); using the Mac voice instead")
            voice = mac_voice()
    txt, aiff = WORK / f"say-{key}.txt", WORK / f"say-{key}.aiff"
    txt.write_text(speakable(text))
    p = run(["say", "-v", voice, "-o", aiff, "-f", txt])
    if p.returncode != 0 or not aiff.exists():
        raise RuntimeError(f"say failed: {p.stderr.strip()}")
    normalize(aiff, out, fade=False)
    aiff.unlink()
    txt.unlink()
    return out


def join(parts, dst, tags):
    """parts (FLAC) joined with GAP silence between them -> 64 kbps mono MP3."""
    cmd = [FF, "-hide_banner", "-nostdin", "-y"]
    for p in parts:
        cmd += ["-i", p]
    chain, labels = [], []
    for i in range(len(parts)):
        labels.append(f"[{i}:a]")
        if i < len(parts) - 1:
            chain.append(f"anullsrc=r=44100:cl=mono:d={GAP}[g{i}]")
            labels.append(f"[g{i}]")
    chain.append("".join(labels) + f"concat=n={len(labels)}:v=0:a=1[a]")
    cmd += ["-filter_complex", ";".join(chain), "-map", "[a]", "-c:a", "libmp3lame",
            "-b:a", "64k", "-ar", "44100", "-ac", "1", "-id3v2_version", "3"]
    for k, v in tags.items():
        cmd += ["-metadata", f"{k}={v}"]
    p = run(cmd + [dst])
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg join failed: {p.stderr[-400:]}")


# ---------- outputs ----------

def write_feed(ep):
    day = dt.date.fromisoformat(ep["date"])
    base = dt.datetime(day.year, day.month, day.day, 6, 0, tzinfo=ET)
    x = ['<?xml version="1.0" encoding="UTF-8"?>',
         '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" '
         'xmlns:atom="http://www.w3.org/2005/Atom">', "<channel>",
         "<title>LATS Radio</title>", f"<link>{SITE}</link>",
         f'<atom:link href="{SITE}feed.xml" rel="self" type="application/rss+xml"/>',
         "<language>en-us</language>",
         "<description>Daily learning clips for the LATS startup search. Private feed.</description>",
         "<itunes:author>LATS</itunes:author>", "<itunes:type>serial</itunes:type>",
         f'<itunes:image href="{SITE}cover.png"/>', '<itunes:category text="Business"/>',
         "<itunes:explicit>false</itunes:explicit>", "<itunes:block>Yes</itunes:block>",
         f"<lastBuildDate>{format_datetime(dt.datetime.now(ET))}</lastBuildDate>"]
    for n, it in enumerate(ep["items"], start=1):
        url = SITE + it["file"]
        desc = " ".join(s for s in [it.get("intro", ""),
                                    f"Listen for: {it['listenFor']}" if it.get("listenFor") else "",
                                    f"Claim: {it['claim']}" if it.get("claim") else "",
                                    it.get("url", "")] if s)
        x += ["<item>", f"<title>{escape(f'{n:02d}. ' + it['title'])}</title>",
              f"<description>{escape(desc)}</description>",
              f"<enclosure url={quoteattr(url)} length=\"{it['bytes']}\" type=\"audio/mpeg\"/>",
              f"<guid isPermaLink=\"false\">lats-radio-{ep['date']}-{escape(Path(it['file']).stem)}</guid>",
              f"<pubDate>{format_datetime(base + dt.timedelta(minutes=n))}</pubDate>",
              f"<itunes:duration>{round(it['seconds'])}</itunes:duration>",
              f"<itunes:episode>{n}</itunes:episode>",
              f"<itunes:author>{escape(it.get('speaker', ''))}</itunes:author>",
              "<itunes:explicit>false</itunes:explicit>", "</item>"]
    x += ["</channel>", "</rss>"]
    (ROOT / "feed.xml").write_text("\n".join(x) + "\n")


def git(*args, check=True):
    p = run(["git", "-C", ROOT, *args])
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()[-300:]}")
    return p.stdout.strip()


def publish(message):
    """Commit and push only if something changed. main is kept as a single
    commit so deleted audio does not pile up in history."""
    if not (ROOT / ".git").exists():
        log("no git repo yet; skipping push")
        return False
    git("add", "-A")
    tree = git("write-tree")
    head_tree = run(["git", "-C", ROOT, "rev-parse", "HEAD^{tree}"]).stdout.strip()
    remote = run(["git", "-C", ROOT, "rev-parse", "refs/remotes/origin/main^{tree}"]).stdout.strip()
    if tree == head_tree and tree == remote:
        log("nothing changed; no commit")
        return False
    if tree != head_tree:
        commit = git("commit-tree", tree, "-m", message)      # no parent: squashed
        git("update-ref", "HEAD", commit)
    if NO_PUSH or not git("remote", check=False):
        log("committed locally (push skipped)")
        return False
    git("push", "--force", "origin", "HEAD:main")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    git("reflog", "expire", "--expire=now", "--all", check=False)
    git("gc", "--prune=now", "-q", check=False)
    log("pushed to GitHub")
    return True


def wait_live(sig):
    for _ in range(40):
        try:
            with urllib.request.urlopen(f"{SITE}episode.json?t={time.time()}", timeout=20, context=SSL_CTX) as r:
                if json.loads(r.read().decode()).get("sig") == sig:
                    return True
        except Exception:
            pass
        time.sleep(15)
    return False


# ---------- main ----------

def main():
    WORK.mkdir(exist_ok=True)
    lock = open(WORK / "build.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another build is running; exiting")
        return 0

    now = dt.datetime.now(ET)
    day = (now + dt.timedelta(days=1)).date() if now.hour >= 18 else now.date()
    date = day.isoformat()
    adir = audio_dir_name()
    voice = pick_voice()
    log(f"START build for {date} (voice: {voice})")

    rows = read_rows(fetch_sheet())
    if not rows:
        log("FAILED: the sheet has no usable rows; keeping the previous episode")
        return 1
    fields = ["id", "videoId", "start", "end", "title", "speaker", "idea", "intro",
              "listenFor", "claim", "url"]
    sig = hashlib.sha1(json.dumps([[r.get(f, "") for f in fields] for r in rows]
                                  + [date, voice, PIPELINE]
                                  + [[r.get("channel", ""), "lesson"] for r in rows if r.get("kind") == "lesson"]
                                  ).encode()).hexdigest()[:16]

    ep_path = ROOT / "episode.json"
    old = json.loads(ep_path.read_text()) if ep_path.exists() else {}
    if (old.get("sig") == sig and old.get("complete")
            and all((ROOT / i["file"]).exists() for i in old.get("items", []))):
        log(f"sheet unchanged for {date}: {len(old['items'])} clips already built")
        pushed = publish(f"LATS Radio {date}")
        if pushed and not wait_live(sig):
            log("WARNING: pushed, but the live site has not updated yet")
        log("SUCCESS (no rebuild needed)")
        return 0

    # 1. clips and spoken intros
    built, failed = [], []
    with ThreadPoolExecutor(max_workers=6) as pool:      # YouTube throttles each stream
        futures = [pool.submit(get_audio, r) for r in rows]
    for r, fut in zip(rows, futures):
        try:
            clip = fut.result()
            intro = speak(r["intro"], voice) if r.get("intro") else None
            built.append((r, clip, intro))
            note = clip.with_suffix(".note").read_text() if clip.with_suffix(".note").exists() else "exact"
            if r.get("kind") == "lesson":
                log(f"ok   {r['id']}: NotebookLM lesson {duration(clip) / 60:.1f} min")
            else:
                log(f"ok   {r['id']}: clip {duration(clip):.1f}s (sheet {r['end'] - r['start']:.0f}s; cut moved to a pause: {note})")
        except Exception as e:
            failed.append(r["id"])
            log(f"SKIP {r['id']}: {str(e).splitlines()[-1][:300] if str(e) else e!r}")
    if not built:
        log("FAILED: no clip could be built; keeping the previous episode")
        return 1

    # 2. spoken overview for the first item
    durs = [duration(c) + ((duration(i) + GAP) if i else 0) for _, c, i in built]
    subjects = []
    for r, _, _ in built:
        s = r.get("idea", "")
        if s and s not in subjects:
            subjects.append(s)
    subj = (", ".join(subjects[:-1]) + " and " + subjects[-1]) if len(subjects) > 1 else "".join(subjects)
    minutes = round((sum(durs) + 9) / 60)
    nl = sum(1 for r, _, _ in built if r.get("kind") == "lesson")
    n = len(built) - nl
    count = f"{n} clip{'s' if n != 1 else ''}" if n else ""
    if nl:
        count += (" and " if count else "") + ("one lesson" if nl == 1 else f"{nl} lessons")
    overview = (f"LATS Radio for {day:%A, %B} {day.day}: {count}, "
                f"about {minutes} minutes" + (f", on {subj}." if subj else "."))
    ov = speak(overview, voice)

    # 3. one MP3 per item
    ddir = ROOT / adir / date
    ddir.mkdir(parents=True, exist_ok=True)
    items, keep = [], set()
    for k, (r, clip, intro) in enumerate(built, start=1):
        parts = ([ov] if k == 1 else []) + ([intro] if intro else []) + [clip]
        cut = clip.with_suffix(".note").read_text() if clip.with_suffix(".note").exists() else ""
        key = hashlib.sha1(json.dumps([r["videoId"], r["start"], r["end"], r.get("intro", ""), voice,
                                       PIPELINE, overview if k == 1 else "", cut]).encode()).hexdigest()[:8]
        mp3 = ddir / f"{k:02d}-{r['id']}-{key}.mp3"
        if not mp3.exists():
            join(parts, mp3, {"title": r.get("title", r["id"]), "artist": r.get("speaker", ""),
                              "album": "LATS Radio", "track": str(k)})
        secs = duration(mp3)
        expect = sum(duration(p) for p in parts) + GAP * (len(parts) - 1)
        lufs = loudness(mp3)
        flag = "" if abs(secs - expect) < 0.5 else "  <-- LENGTH MISMATCH"
        log(f"mp3  {mp3.name}: {secs:.1f}s (expected {expect:.1f}s), {lufs:.1f} LUFS{flag}")
        keep.add(mp3.name)
        items.append({"id": r["id"], "kind": r.get("kind", "clip"),
                      "title": r.get("title", ""), "speaker": r.get("speaker", ""),
                      "idea": r.get("idea", ""), "intro": r.get("intro", ""),
                      "listenFor": r.get("listenFor", ""), "claim": r.get("claim", ""),
                      "url": r.get("url", ""), "file": f"{adir}/{date}/{mp3.name}",
                      "seconds": round(secs, 1), "bytes": mp3.stat().st_size,
                      "clipSeconds": round(duration(clip), 1), "sheetSeconds": round(r["end"] - r["start"], 1), "lufs": round(lufs, 1)})
    for f in ddir.glob("*.mp3"):                     # leftovers from an earlier build today
        if f.name not in keep:
            f.unlink()

    # 4. keep only the last KEEP_DAYS days of audio
    days = sorted((p for p in (ROOT / adir).iterdir()
                   if p.is_dir() and re.fullmatch(r"\d{4}-\d\d-\d\d", p.name)), reverse=True)
    for p in days[KEEP_DAYS:]:
        run(["git", "-C", ROOT, "rm", "-r", "-q", "--ignore-unmatch", str(p.relative_to(ROOT))])
        shutil.rmtree(p, ignore_errors=True)
        log(f"removed old audio {p.name}")
    for f in WORK.iterdir():                          # work cache: 2 days
        if f.name != "build.lock" and time.time() - f.stat().st_mtime > 2 * 86400:
            f.unlink()

    # 5. episode.json, feed.xml
    total = sum(i["seconds"] for i in items)
    ep = {"date": date, "sig": sig, "complete": not failed, "overview": overview,
          "generated": dt.datetime.now(ET).isoformat(timespec="seconds"),
          "totalSeconds": round(total, 1), "subjects": subjects, "skipped": failed, "items": items}
    ep_path.write_text(json.dumps(ep, indent=1, ensure_ascii=False) + "\n")
    write_feed(ep)
    log(f"episode {date}: {len(items)} items, {total / 60:.1f} min"
        + (f"; skipped {len(failed)}: {', '.join(failed)}" if failed else ""))

    # 6. publish
    pushed = publish(f"LATS Radio {date}")
    if pushed and not wait_live(sig):
        log("WARNING: pushed, but the live site has not updated yet")
    log("SUCCESS" + (f" (with {len(failed)} skipped)" if failed else ""))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"FAILED: {e}")
        sys.exit(1)
