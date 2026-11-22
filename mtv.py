"""Local source-audio import and music-video shot planning."""

import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path

import engine

MAX_UPLOAD = 512 * 1024 * 1024
TRACK_ID = re.compile(r"[a-f0-9]{24}")
STYLE = {
    "cinematic": "电影写实：自然光与真实场景，明确的空间关系",
    "retro": "复古胶片：暖色、高光晕染、适量颗粒",
    "stage": "霓虹舞台：轮廓光、舞台装置与清晰的表演站位",
    "monochrome": "黑白高反差：光影主导，强调轮廓与质地",
    "dream": "中式梦核：熟悉空间、夸张物件与清晰的视觉反转",
    "animation": "二维动画：鲜明色块、可读的肢体和表情",
    "stop-motion": "手工定格：实体质感、微缩布景和逐格节奏",
    "minimal": "极简意象：克制色彩、少量物件与干净构图",
}
TREATMENTS = {
    "narrative": "剧情叙事，以人物行动推动情节",
    "performance": "舞台表演，以歌手、乐队或舞者为主",
    "hybrid": "剧情加表演，主歌推进剧情，副歌安排表演",
    "concept": "意象与节奏，以场景、动作和视觉母题呼应音乐",
}
TIME_TAG = re.compile(r"\[(\d{1,4}):(\d{2}(?:[.:]\d{1,3})?)\]")
CUE = re.compile(r"^\s*(?:(\d{1,4}):)?(\d{1,6}(?:\.\d{1,3})?)(?:\s+(.*))?\s*$")


def transcoder():
    path = os.environ.get("VALTZ_DIRECTOR_FFMPEG") or shutil.which("ffmpeg")
    if not path:
        path = Path.home() / ".local/bin/ffmpeg"
    if not Path(path).is_file():
        raise ValueError("此操作需要 FFmpeg。请安装后重新打开操作台。")
    return str(path)


def audio_info(file):
    demuxer = "wav" if file.suffix.lower() == ".wav" else "mov"
    result = subprocess.run([str(engine.FFPROBE), "-v", "error", "-protocol_whitelist", "file",
                             "-f", demuxer, "-select_streams", "a:0",
                             "-show_entries", "stream=duration,sample_rate,channels,codec_name:format=duration,format_name",
                             "-of", "json", str(file)], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError("无法读取这个音频文件。请检查文件是否损坏。")
    payload = json.loads(result.stdout)
    streams = payload.get("streams", [])
    if not streams:
        raise ValueError("文件没有可用的声音轨。")
    track = streams[0]
    try:
        duration = float(track.get("duration") or payload["format"]["duration"])
    except (KeyError, ValueError, TypeError):
        duration = float(payload.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("没有测到有效的音频时长。")
    return {"duration": round(duration, 3), "sample_rate": int(track["sample_rate"]),
            "channels": int(track["channels"]), "format": payload["format"]["format_name"]}


def import_audio(folder, key, name, stream, length):
    if not 0 < length <= MAX_UPLOAD:
        raise ValueError("声音文件为空，或超过单个文件 512 MB 的上传上限。")
    if Path(name).suffix.lower() not in (".wav", ".mp4"):
        raise ValueError("请选择 WAV 或带声音的 MP4。")
    folder.mkdir(exist_ok=True)
    suffix = Path(name).suffix.lower()
    original = folder / (key + suffix)
    preview = folder / (key + ".preview.wav")
    try:
        with original.open("xb") as output:
            remaining = length
            while remaining:
                part = stream.read(min(remaining, 1024 * 1024))
                if not part:
                    raise ValueError("音频上传不完整，请重试。")
                output.write(part)
                remaining -= len(part)
        source = audio_info(original)
        expected = {"wav"} if suffix == ".wav" else {"mp4"}
        if not expected.intersection(source["format"].split(",")):
            raise ValueError("实际文件格式与后缀不一致。请选择 WAV 或 MP4。")
        demuxer = "wav" if suffix == ".wav" else "mov"
        result = subprocess.run([transcoder(), "-nostdin", "-v", "error", "-n", "-protocol_whitelist", "file",
                                 "-f", demuxer, "-i", str(original),
                                 "-map", "0:a:0", "-vn", "-c:a", "pcm_s24le", "-fs", str(1024 ** 3), str(preview)],
                                capture_output=True, timeout=180)
        if result.returncode:
            raise ValueError("提取音轨失败，原项目未修改。")
        extracted = audio_info(preview)
        if abs(source["duration"] - extracted["duration"]) > 0.15:
            raise ValueError("提取后的音频长度与来源不符，未将其用于分镜。")
        return {"id": key, "name": Path(name).name[:180], "source": original.name,
                "preview": preview.name, **extracted}
    except Exception:
        original.unlink(missing_ok=True)
        preview.unlink(missing_ok=True)
        raise


def timeline(duration, cues):
    end = round(duration * 1000)
    if end < 2000:
        raise ValueError("声音至少需要 2 秒才能拆分镜头。")
    anchors = {0: "", end: ""}
    for line in cues.splitlines():
        if not line.strip():
            continue
        match = CUE.fullmatch(line)
        if not match or (match[1] and float(match[2]) >= 60):
            raise ValueError("时间点写法示例：00:24.500 副歌，或 24.5 副歌。")
        stamp = round((int(match[1] or 0) * 60 + float(match[2])) * 1000)
        if not 0 <= stamp <= end:
            raise ValueError("有一个时间点不在音频范围内，请试听后调整。")
        anchors[stamp] = (match[3] or "")[:100]
    boundaries = sorted(anchors)
    windows = []
    for left, right in zip(boundaries, boundaries[1:]):
        span = right - left
        if span < 2000:
            raise ValueError("相邻分镜锚点至少相隔 2 秒，包括片头和片尾。")
        count = max(1, math.ceil(span / 9000))
        cuts = [left + round(span * step / count) for step in range(count + 1)]
        for start, stop in zip(cuts, cuts[1:]):
            windows.append({"id": len(windows) + 1, "start_ms": start, "end_ms": stop,
                            "anchor": anchors.get(start, "")})
    if len(windows) > 150:
        raise ValueError("分镜较长，请先选一个歌曲段落制作，再继续其余段落。")
    return windows


def timed_lyrics(lyrics, duration):
    offset = re.search(r"^\[offset:\s*([+-]?\d+)\s*\]", lyrics, re.IGNORECASE | re.MULTILINE)
    shift = int(offset[1]) if offset else 0
    entries = []
    for line in lyrics.splitlines():
        tags = list(TIME_TAG.finditer(line))
        lyric = TIME_TAG.sub("", line).strip()
        if not lyric:
            continue
        for match in tags:
            stamp = round((int(match[1]) * 60 + float(match[2].replace(":", "."))) * 1000) + shift
            if 0 <= stamp < duration * 1000:
                entries.append({"time_ms": stamp, "line": lyric})
    return sorted(entries, key=lambda item: item["time_ms"])


def draft(settings, track, cast, profile):
    windows = timeline(track["duration"], settings["cues"])
    synced = timed_lyrics(settings["lyrics"], track["duration"])
    for window in windows:
        window["lyrics"] = [item for item in synced if window["start_ms"] <= item["time_ms"] < window["end_ms"]]
    package = {"treatment": TREATMENTS[settings["treatment"]], "look": STYLE[settings["style"]],
               "direction": settings["direction"], "duration_ms": round(track["duration"] * 1000),
               "lyrics": settings["lyrics"], "has_timed_lyrics": bool(synced),
               "approved_cast": [{"number": i + 1, "name": person["name"], "look": person.get("prompt", "")[:1000],
                                  "has_adopted_look": bool(person.get("adopted"))} for i, person in enumerate(cast)],
               "windows": windows}
    instructions = """Write a music-video treatment and per-shot storyboard in Chinese. Use supplied lyrics as source material.
You receive duration and a fixed timeline, not audio pixels or samples. Do not claim to have heard the song,
found its beat, identified the chorus from the recording, or inferred exact line times from unmarked lyrics.
Treat notes on anchor times and LRC times as supplied editorial marks. Link actions and edits to those only
when provided. Untimed lyrics are themes. Capture performance only as a visual plan; never promise lip sync.
Use exactly the approved cast numbers when showing characters and no added characters. A roster of zero means
use scenery, objects or graphics. Avoid complex simultaneous movement or extra dialogue.
Output a JSON object with script (150-450 Chinese characters) and shots, one per supplied window in order.
Shot fields: id (copy window id), name, kind ('narrative', 'performance', or 'insert'),
characters (array of approved integer numbers), action (a filmable shot including composition and movement),
image_prompt (opening-frame description), cue (short editing direction; say if approximate).
Use original lyrics unchanged if quoted. Do not write timestamps, durations, audio or media references into
the prompts; the application binds precise source windows. No Markdown."""
    raw = engine.complete(instructions, json.dumps(package, ensure_ascii=False), min(16000, 2500 + len(windows) * 260), profile)
    try:
        proposal = engine.parse_json(raw)
        shots = proposal["shots"]
        script = proposal["script"]
        if not isinstance(script, str) or not script.strip() or len(script) > 5000:
            raise ValueError
        if not isinstance(shots, list) or [item["id"] for item in shots] != [window["id"] for window in windows]:
            raise ValueError
        for item in shots:
            if item["kind"] not in ("narrative", "performance", "insert"):
                raise ValueError
            if not all(isinstance(item[key], str) and item[key].strip() and len(item[key]) <= 5000
                       for key in ("name", "action", "image_prompt", "cue")):
                raise ValueError
            if not isinstance(item["characters"], list) or not all(
                type(number) is int and 1 <= number <= len(cast) for number in item["characters"]
            ):
                raise ValueError
    except (ValueError, KeyError, TypeError):
        raise ValueError("模型未按原时间表返回完整剧本及分镜，已保留原项目。可减少分段后重试。") from None
    for item, window in zip(shots, windows):
        item.update(window)
        item["ref_ids"] = list(dict.fromkeys(cast[number - 1]["id"] for number in item["characters"]))
    return {"script": script, "shots": shots, "track_id": track["id"], "track_name": track["name"],
            "timing": "锚点和 LRC" if synced and settings["cues"].strip() else
                      "LRC 辅助" if synced else "手动锚点" if settings["cues"].strip() else "按时长均分",
            "duration": track["duration"]}
