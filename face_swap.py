"""Workbench adapter for local face analysis and replacement."""

import json
import math
import os
import re
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import engine
import mtv

VIDEO_MAX = 512 * 1024 * 1024
SWAP_SUFFIXES = {".mp4", ".mov"}


def runtime(root):
    override = os.environ.get("VALTZ_DIRECTOR_FACE_PYTHON")
    if override:
        return Path(override).expanduser()
    # On an external installation, find the isolated worker relative to the mounted work root.
    return root.parent.parent / "agent-work/valtz-director-face-runtime/bin/python"


def weights():
    return Path(os.environ.get("VALTZ_DIRECTOR_FACE_MODELS", "~/.insightface/models")).expanduser()


def available(root):
    model = weights()
    python = runtime(root)
    if not python.is_file() or not all((model / name).is_file() for name in (
        "buffalo_l/det_10g.onnx", "buffalo_l/w600k_r50.onnx", "inswapper_128.onnx",
    )):
        return False
    return True


def video_info(file):
    probe = subprocess.run([str(engine.FFPROBE), "-v", "error", "-protocol_whitelist", "file", "-f", "mov",
                            "-show_streams", "-show_format", "-of", "json", str(file)], capture_output=True, timeout=40)
    if probe.returncode:
        raise ValueError("无法读取该视频，请使用可播放的 MP4 或 MOV。")
    data = json.loads(probe.stdout)
    video = next((stream for stream in data.get("streams", []) if stream["codec_type"] == "video"
                  and not stream.get("disposition", {}).get("attached_pic")), None)
    if not video:
        raise ValueError("文件没有可处理的视频轨。")
    try:
        duration = float(video.get("duration") or data["format"]["duration"])
    except (ValueError, KeyError, TypeError):
        duration = float(data.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("无法确认视频时长。")
    width, height = video["width"], video["height"]
    rotation = next((item.get("rotation", 0) for item in video.get("side_data_list", []) if "rotation" in item), 0)
    if round(abs(rotation)) % 180 == 90:
        width, height = height, width
    fps = Fraction(video.get("avg_frame_rate") or "0/1") if video.get("avg_frame_rate") != "0/0" else Fraction(0)
    if not 0 < fps <= 120:
        fps = Fraction(24)
    audio_tracks = sum(stream.get("codec_type") == "audio" for stream in data["streams"])
    return {"duration": duration, "width": width, "height": height, "fps": str(fps),
            "audio": bool(audio_tracks), "audio_tracks": audio_tracks}


def import_video(folder, key, name, stream, length):
    suffix = Path(name).suffix.lower()
    if suffix not in SWAP_SUFFIXES:
        raise ValueError("请选择 MP4 或 MOV 视频。")
    if not 0 < length <= VIDEO_MAX:
        raise ValueError("视频为空，或超过单文件 512 MB 上传上限。")
    folder.mkdir(exist_ok=True)
    target = folder / (key + suffix)
    try:
        with target.open("xb") as output:
            remaining = length
            while remaining:
                block = stream.read(min(remaining, 1024 * 1024))
                if not block:
                    raise ValueError("视频上传不完整。")
                output.write(block)
                remaining -= len(block)
        return {"id": key, "name": Path(name).name[:180], "file": target.name, **video_info(target)}
    except Exception:
        target.unlink(missing_ok=True)
        raise


def run(root, project, job, source, video, scan=None, mappings=None, preview=False):
    if not available(root):
        raise ValueError("本机换脸环境或模型未就绪。")
    job_id = job["id"]
    work = project / "face-work"
    work.mkdir(exist_ok=True)
    config = work / (job_id + ".input.json")
    output = work / (job_id + ".result.json")
    assets = project / "media"
    log = project / "jobs" / (job_id + ".log")
    action = "scan" if scan is None else "replace"
    config.write_text(json.dumps({
        "action": action, "weights": str(weights()), "source": str(source), "video": video,
        "assets": str(assets), "output": str(output), "id": job_id, "preview": preview,
        "ffmpeg": mtv.transcoder(), "scan": str(work / (scan + ".result.json")) if scan else None,
        "mappings": {key: str(value) for key, value in (mappings or {}).items()},
    }, ensure_ascii=False), encoding="utf-8")
    env = dict(os.environ, NO_ALBUMENTATIONS_UPDATE="1", OMP_NUM_THREADS="4", OPENBLAS_NUM_THREADS="4")
    try:
        with log.open("a", encoding="utf-8") as report:
            result = subprocess.run([str(runtime(root)), str(Path(__file__).with_name("face_worker.py")), str(config)],
                                    cwd=work, env=env, stdout=report, stderr=subprocess.STDOUT)
        if result.returncode:
            error_file = output.with_suffix(".error")
            if error_file.is_file():
                raise ValueError(error_file.read_text(encoding="utf-8"))
            raise ValueError("本地换脸任务失败。可在任务记录查看详细日志。")
        data = json.loads(output.read_text(encoding="utf-8"))
        if action == "scan":
            return {"video_id": video["id"], "faces": [
                {key: face[key] for key in ("id", "thumbnail", "time", "samples")} for face in data["faces"]
            ], "samples": data["samples"]}
        result_file = assets / (job_id + ".mp4")
        info = video_info(result_file)
        if info["audio_tracks"] != video.get("audio_tracks", int(video["audio"])) or abs(info["duration"] - data["target_duration"]) > 0.15:
            raise ValueError("输出时长或音轨校验失败，原视频已保留。")
        return {"video_id": video["id"], "file": result_file.name, "duration": info["duration"],
                "preview": preview, "counts": data["counts"], "start": data["start"], "frames": data["frames"]}
    finally:
        config.unlink(missing_ok=True)
