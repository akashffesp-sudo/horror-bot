#!/usr/bin/env python3
"""
Daily YouTube Shorts bot - 100% free stack.

trend analysis (YouTube API) -> story (Groq) -> images (Pollinations)
-> voice (edge-tts) -> animated vertical video (ffmpeg) -> upload (YouTube API)

Usage:
    python bot.py hi      # Hindi short
    python bot.py en      # English short

Env vars:
    GROQ_API_KEY                                  (required)
    YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN   (required for trends + upload)
    DRY_RUN=1        build the video but do not upload
    PRIVACY=private  private | unlisted | public
    STORY_GENRE      e.g. "moral story", "horror story", "motivational story"
"""
import asyncio
import datetime
import json
import os
import pathlib
import random
import re
import subprocess
import sys
import time
import urllib.parse

import requests
from PIL import Image, ImageDraw, ImageOps

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

LANGS = {
    "hi": dict(name="Hindi", region="IN", voice="hi-IN-MadhurNeural", font="Noto Sans Devanagari"),
    "en": dict(name="English", region="US", voice="en-US-AndrewNeural", font="Noto Sans"),
}

LANG = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("LANG_CODE", "hi")).lower()
if LANG not in LANGS:
    raise SystemExit("Language must be 'hi' or 'en'")
L = LANGS[LANG]

DRY_RUN = os.getenv("DRY_RUN", "0") == "1"
PRIVACY = os.getenv("PRIVACY", "private")
MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GENRE = os.getenv("STORY_GENRE", "short scary horror story with a twist ending (spooky, no gore, no violence)")
FPS = 30
TODAY = datetime.date.today().isoformat()

WORK = pathlib.Path("work") / LANG
OUT = pathlib.Path("output")


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-3000:])
        raise SystemExit(f"command failed: {cmd[0]}")
    return r.stdout


# ---------------------------------------------------------------- YouTube
def yt_client():
    need = ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN")
    if not all(os.getenv(k) for k in need):
        print("YouTube credentials missing -> skipping trends/upload")
        return None
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        scopes=SCOPES,
    )
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def get_trends(yt, region):
    """Return (trending titles, most common trending tags)."""
    if yt is None:
        return [], []
    try:
        r = yt.videos().list(
            part="snippet", chart="mostPopular", regionCode=region, maxResults=25
        ).execute()
        titles, counts = [], {}
        for it in r.get("items", []):
            sn = it["snippet"]
            titles.append(sn["title"])
            for t in sn.get("tags", [])[:6]:
                counts[t.lower()] = counts.get(t.lower(), 0) + 1
        tags = [t for t, _ in sorted(counts.items(), key=lambda x: -x[1])[:20]]
        return titles[:15], tags
    except Exception as e:  # keep going even if trends fail
        print("trend fetch failed:", e)
        return [], []


def upload(yt, video, story):
    from googleapiclient.http import MediaFileUpload

    tags = story["hashtags"]
    title = (story["title"][:88] + " #Shorts")[:100]
    desc = f'{story["description"]}\n\n{" ".join(tags)} #Shorts\n\nAI-assisted story.'
    body = {
        "snippet": {
            "title": title,
            "description": desc,
            "tags": [t.lstrip("#") for t in tags] + ["Shorts"],
            "categoryId": "24",
            "defaultLanguage": LANG,
            "defaultAudioLanguage": LANG,
        },
        "status": {
            "privacyStatus": PRIVACY,
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True,
        },
    }
    media = MediaFileUpload(str(video), mimetype="video/mp4", resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    print("Uploaded: https://youtube.com/shorts/" + resp["id"])


# ---------------------------------------------------------------- Story (Groq)
def clean_tags(tags):
    out = []
    for t in tags:
        t = "#" + re.sub(r"[\s#.,!?'\"-]", "", str(t))
        if len(t) > 1 and t not in out:
            out.append(t)
    return out[:10]


def make_story(titles, tags):
    from groq import Groq

    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    prompt = (
        f"You run a YouTube Shorts channel. Language of the video: {L['name']}.\n"
        f"Today's trending YouTube titles ({L['region']}): {json.dumps(titles, ensure_ascii=False)}\n"
        f"Trending tags: {json.dumps(tags, ensure_ascii=False)}\n\n"
        f"1. Find the common theme or mood in today's trends.\n"
        f"2. Write an ORIGINAL {GENRE} inspired by that theme. Do not copy any video, "
        f"song, movie or real person. Family friendly.\n"
        f"3. Return ONLY a JSON object with these keys:\n"
        f'   "topic": short topic in English,\n'
        f'   "title": catchy title in {L["name"]}, max 70 characters, no hashtags,\n'
        f'   "description": 2-3 lines in {L["name"]},\n'
        f'   "hashtags": list of 6-8 hashtags mixing trending tags and story keywords,\n'
        f'   "scenes": list of exactly 7 objects. Each has "narration" (1-2 short sentences in '
        f'{L["name"]}; the whole story must take 35-45 seconds when spoken) and "image_prompt" '
        f"(English visual description of that scene, same characters described the same way in "
        f"every scene, no text in the image).\n"
    )
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.9,
                response_format={"type": "json_object"},
            )
            data = json.loads(resp.choices[0].message.content)
            assert len(data["scenes"]) >= 4
            data["hashtags"] = clean_tags(data.get("hashtags", []))
            return data
        except Exception as e:
            print(f"Groq attempt {attempt + 1} failed:", e)
            time.sleep(4)
    raise SystemExit("Could not get a story from Groq")


# ---------------------------------------------------------------- Images (free)
STYLE = (
    "vertical 9:16, dark spooky 3D cartoon animation style, moody cinematic lighting, "
    "highly detailed, no text, no watermark"
)


def placeholder(path, seed):
    rnd = random.Random(seed)
    top = tuple(rnd.randint(20, 120) for _ in range(3))
    bot = tuple(rnd.randint(80, 220) for _ in range(3))
    img = Image.new("RGB", (1080, 1920))
    d = ImageDraw.Draw(img)
    for y in range(1920):
        k = y / 1919
        d.line([(0, y), (1080, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * k) for i in range(3)))
    img.save(path, "JPEG", quality=90)


def make_image(prompt, path, seed):
    url = (
        "https://image.pollinations.ai/prompt/"
        + urllib.parse.quote(f"{prompt}, {STYLE}")
        + f"?width=1080&height=1920&nologo=true&seed={seed}"
    )
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=150)
            if r.ok and r.headers.get("content-type", "").startswith("image"):
                path.write_bytes(r.content)
                img = ImageOps.fit(Image.open(path).convert("RGB"), (1080, 1920))
                img.save(path, "JPEG", quality=92)
                return
            print("image HTTP", r.status_code)
        except Exception as e:
            print("image error:", e)
        time.sleep(6 * (attempt + 1))
    print("Using placeholder background for", path.name)
    placeholder(path, seed)


# ---------------------------------------------------------------- Voice (edge-tts)
async def _tts(text, path):
    import edge_tts

    await edge_tts.Communicate(text, L["voice"]).save(str(path))


def duration(path):
    out = run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(path),
    ])
    return float(out.strip())


# ---------------------------------------------------------------- Video (ffmpeg)
def make_clip(img, audio, dur, out, idx):
    total = dur + 0.4
    frames = int(total * FPS) + 1
    if idx % 2 == 0:
        z = "min(zoom+0.0012,1.3)"
    else:
        z = "if(lte(on,1),1.3,max(1.0,zoom-0.0012))"
    vf = (
        f"scale=1620:2880,zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d={frames}:s=1080x1920:fps={FPS},format=yuv420p"
    )
    run([
        "ffmpeg", "-y", "-i", str(img), "-i", str(audio),
        "-vf", vf, "-af", "apad", "-t", f"{total:.2f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "160k", str(out),
    ])
    return total


def ass_time(t):
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


def build_video(clips, texts, lengths, final):
    lst = WORK / "list.txt"
    lst.write_text("".join(f"file '{c.resolve()}'\n" for c in clips), encoding="utf-8")
    joined = WORK / "joined.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(joined)])

    # Captions as an ASS file with an explicit 1080x1920 canvas (SRT would be scaled wrongly).
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{L['font']},64,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,"
        "-1,0,0,0,100,100,0,0,1,5,2,2,80,80,330,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines, t = [], 0.0
    for txt, ln in zip(texts, lengths):
        clean = txt.replace("\n", " ").replace("{", "(").replace("}", ")")
        lines.append(f"Dialogue: 0,{ass_time(t)},{ass_time(t + ln - 0.05)},Default,,0,0,0,,{clean}")
        t += ln
    ass_path = WORK / "subs.ass"
    ass_path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    subs = f"ass={ass_path.as_posix()}"

    music = sorted(pathlib.Path("music").glob("*.mp3")) if pathlib.Path("music").exists() else []
    cmd = ["ffmpeg", "-y", "-i", str(joined)]
    if music:
        cmd += ["-stream_loop", "-1", "-i", str(random.choice(music))]
        fc = (
            f"[0:v]{subs}[v];[0:a]volume=1.0[a0];[1:a]volume=0.12[a1];"
            "[a0][a1]amix=inputs=2:duration=first,volume=2[a]"
        )
        cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "[a]"]
    else:
        cmd += ["-vf", subs]
    cmd += [
        "-t", f"{t:.2f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(final),
    ]
    run(cmd)
    return t


# ---------------------------------------------------------------- Main
def main():
    WORK.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(exist_ok=True)

    yt = yt_client()
    titles, tags = get_trends(yt, L["region"])
    story = make_story(titles, tags)

    report = {"date": TODAY, "lang": LANG, "trend_titles": titles, "trend_tags": tags, "story": story}
    (OUT / f"report_{TODAY}_{LANG}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("Title:", story["title"])

    clips, texts, lengths = [], [], []
    for i, sc in enumerate(story["scenes"][:8]):
        img = WORK / f"img{i}.jpg"
        aud = WORK / f"voice{i}.mp3"
        clip = WORK / f"clip{i}.mp4"
        make_image(sc["image_prompt"], img, seed=random.randint(1, 10**6))
        asyncio.run(_tts(sc["narration"], aud))
        total = make_clip(img, aud, duration(aud), clip, i)
        clips.append(clip)
        texts.append(sc["narration"])
        lengths.append(total)
        print(f"scene {i + 1} ready ({total:.1f}s)")

    final = OUT / f"short_{TODAY}_{LANG}.mp4"
    total = build_video(clips, texts, lengths, final)
    print(f"Video ready: {final} ({total:.1f}s)")
    if total > 59:
        print("WARNING: longer than 59s - may not count as a Short")

    if DRY_RUN or yt is None:
        print("Dry run / no YouTube credentials: not uploading.")
        return
    upload(yt, final, story)


if __name__ == "__main__":
    main()
