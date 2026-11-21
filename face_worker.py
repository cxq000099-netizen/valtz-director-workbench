"""Isolated CPU worker. Reads a private job file created by the workbench."""

import contextlib
import json
import math
import secrets
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import cv2
import insightface
import numpy as np
import onnxruntime


THRESHOLD = 0.49
MARGIN = 0.06


def progress(stage, index, total):
    print(f"[PROGRESS] {min(99, 100 * index // max(total, 1))}% of '{stage}'", flush=True)


def setup(model_root, swapping):
    providers = ["CPUExecutionProvider"]
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = 4
    options.inter_op_num_threads = 1
    cv2.setNumThreads(2)
    o = {"providers": providers, "sess_options": options}
    detector = insightface.model_zoo.get_model(str(model_root / "buffalo_l/det_10g.onnx"), **o)
    recognizer = insightface.model_zoo.get_model(str(model_root / "buffalo_l/w600k_r50.onnx"), **o)
    if not detector or not recognizer:
        raise RuntimeError("本地人脸检测或识别模型未能载入。")
    detector.prepare(ctx_id=-1, input_size=(640, 640), det_thresh=0.55)
    recognizer.prepare(ctx_id=-1)
    swapper = insightface.model_zoo.get_model(str(model_root / "inswapper_128.onnx"), **o) if swapping else None
    if swapping and swapper is None:
        raise RuntimeError("本地换脸模型未能载入。")
    return detector, recognizer, swapper


def detect(frame, detector, recognizer):
    boxes, points = detector.detect(frame, max_num=0)
    found = []
    for bound, kps in zip(boxes, points):
        x0, y0, x1, y1 = bound[:4]
        if min(x1 - x0, y1 - y0) < 26:
            continue
        face = insightface.app.common.Face(bbox=bound[:4], kps=kps, det_score=bound[4])
        recognizer.get(frame, face)
        if face.normed_embedding is not None:
            found.append(face)
    return found


def score(face, group):
    return max(float(np.dot(face.normed_embedding, vector)) for vector in group["templates"])


def matches(faces, groups, threshold=THRESHOLD):
    """Match on original frames. Reject ambiguous and conflicting assignments."""
    ranked = []
    for index, face in enumerate(faces):
        options = sorted(((score(face, group), key) for key, group in groups.items()), reverse=True)
        if not options or options[0][0] < threshold:
            continue
        best, key = options[0]
        if len(options) > 1 and best - options[1][0] < MARGIN:
            continue
        ranked.append((best, index, key))
    assigned = {}
    used = set()
    for _, index, key in sorted(ranked, reverse=True):
        if key not in used:
            assigned[index] = key
            used.add(key)
    return assigned


def face_crop(frame, face):
    x1, y1, x2, y2 = face.bbox
    radius = max(x2 - x1, y2 - y1) * 0.67
    cx, cy = (x1+x2)/2, (y1+y2)/2
    h, w = frame.shape[:2]
    crop = frame[max(0, int(cy-radius)):min(h, int(cy+radius)),
                 max(0, int(cx-radius)):min(w, int(cx+radius))]
    return cv2.resize(crop, (224, 224), interpolation=cv2.INTER_AREA)


def stream_frames(config, fps, width, height, start=0, duration=None):
    args = [config["ffmpeg"], "-nostdin", "-v", "error", "-threads", "2", "-protocol_whitelist", "file",
            "-ss", f"{start:.6f}", "-i", config["source"], "-an", "-sn",
            "-map", "0:V:0", "-vf", f"fps={fps},scale={width}:{height}", "-pix_fmt", "bgr24"]
    if duration is not None:
        args += ["-t", f"{duration:.6f}"]
    args += ["-f", "rawvideo", "-threads", "2", "pipe:1"]
    proc = subprocess.Popen(args, stdout=subprocess.PIPE)
    frame_size = width * height * 3
    try:
        while True:
            raw = proc.stdout.read(frame_size)
            if not raw:
                break
            if len(raw) != frame_size:
                raise RuntimeError("视频解码遇到不完整的视频帧。")
            yield np.frombuffer(raw, dtype=np.uint8).reshape((height,width,3))
        if proc.wait():
            raise RuntimeError("源视频解码失败。")
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            proc.terminate()
            proc.wait()


def scan(config, detector, recognizer):
    info = config["video"]
    rate = min(3.0, 2000 / max(info["duration"], 1.0))
    scale = min(1.0, 960 / max(info["width"], info["height"]))
    w, h = round(info["width"] * scale), round(info["height"] * scale)
    groups = {}
    frame_count = 0
    expected = math.ceil(info["duration"] * rate)
    for index, frame in enumerate(stream_frames(config, str(rate), w, h)):
        frame_count += 1
        faces = detect(frame, detector, recognizer)
        assigned = matches(faces, groups, threshold=0.45)
        for fidx, face in enumerate(faces):
            key = assigned.get(fidx)
            quality = float(face.det_score * np.prod(face.bbox[2:]-face.bbox[:2]))
            if key is None:
                # Close calls are safer to skip than to seed a conflicting identity.
                if len(groups) >= 60 or any(score(face, g) > THRESHOLD and g["id"] not in assigned.values()
                                           for g in groups.values()):
                    continue
                key = secrets.token_hex(12)
                groups[key] = {"id": key, "templates": [], "quality": 0, "samples": 0, "first_time": index / rate}
            group = groups[key]
            group["samples"] += 1
            if len(group["templates"]) == 0 or (len(group["templates"]) < 6 and score(face, group) < 0.86):
                group["templates"].append(face.normed_embedding.copy())
            if quality > group["quality"]:
                group.update(crop=face_crop(frame, face), quality=quality, time=round(index/rate, 3))
        if index % 4 == 0:
            progress("face scan", index, expected)
    if not frame_count:
        raise RuntimeError("视频太短或没有解码到画面。")
    output = []
    for face in sorted(groups.values(), key=lambda item: (-item["samples"], item["first_time"])):
        thumb = Path(config["assets"]) / (secrets.token_hex(12) + ".jpg")
        if not cv2.imwrite(str(thumb), face.pop("crop")):
            raise RuntimeError("人脸缩略图无法保存。")
        face["thumbnail"] = thumb.name
        face["templates"] = [template.tolist() for template in face["templates"]]
        face.pop("quality")
        output.append(face)
    return {"faces": output, "samples": frame_count}


def source_faces(config, detector, recognizer):
    refs = {}
    for key, filename in config["mappings"].items():
        reference = cv2.imread(filename)
        if reference is None:
            raise RuntimeError("有一张参考图无法读取，请使用 JPG 或 PNG。")
        if max(reference.shape[:2]) > 1600:
            scale = 1600 / max(reference.shape[:2])
            reference = cv2.resize(reference, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        faces = detect(reference, detector, recognizer)
        if len(faces) != 1:
            raise RuntimeError("参考图须有一张可清晰识别的人脸。请用清楚的单人照片。")
        refs[key] = faces[0]
    return refs


def replace(config, detector, recognizer, swapper):
    manifest = json.loads(Path(config["scan"]).read_text(encoding="utf-8"))
    groups = {group["id"]: group for group in manifest["faces"]}
    for group in groups.values():
        group["templates"] = [np.array(vector, dtype=np.float32) for vector in group["templates"]]
    refs = source_faces(config, detector, recognizer)
    if not refs or any(key not in groups for key in refs):
        raise RuntimeError("请先确认要替换的人脸及参考图。")
    info = config["video"]
    fps = Fraction(info["fps"])
    start = max(0, min(groups[key]["first_time"] for key in refs) - 0.15) if config["preview"] else 0
    duration = min(5.0, info["duration"] - start) if config["preview"] else info["duration"]
    w, h = info["width"], info["height"]
    target = Path(config["assets"]) / (config["id"] + ".mp4")
    video = target.with_suffix(".partial.mp4")
    feed = [config["ffmpeg"], "-nostdin", "-v", "error", "-n", "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-video_size", f"{w}x{h}", "-framerate", str(fps), "-i", "pipe:0", "-an",
            "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-c:v", "libx264", "-threads", "4",
            "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", str(video)]
    counts = {key: 0 for key in refs}
    frames = 0
    expected = math.ceil(duration * float(fps))
    try:
        with contextlib.closing(stream_frames(config, str(fps), w, h, start, duration)) as source:
            process = subprocess.Popen(feed, stdin=subprocess.PIPE)
            try:
                for frame in source:
                    faces = detect(frame, detector, recognizer)
                    assigned = matches(faces, groups)
                    edited = frame
                    for idx, key in assigned.items():
                        if key not in refs:
                            continue
                        edited = swapper.get(edited, faces[idx], refs[key], paste_back=True)
                        counts[key] += 1
                    process.stdin.write(edited.tobytes())
                    frames += 1
                    if frames % max(1, int(float(fps))) == 1:
                        progress("face replacement", frames, expected)
                process.stdin.close()
                if process.wait() or not frames:
                    raise RuntimeError("换脸视频编码失败。")
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait()
                if not process.stdin.closed:
                    process.stdin.close()
        if not any(counts.values()) or (not config["preview"] and any(value == 0 for value in counts.values())):
            raise RuntimeError("有指定人物在处理范围内未匹配到，未输出可能错误的换脸视频。")
        progress("copy source audio", 0, 1)
        command = [config["ffmpeg"], "-nostdin", "-v", "error", "-n", "-i", str(video),
                   "-protocol_whitelist", "file", "-ss", f"{start:.6f}", "-i", config["source"],
                   "-map", "0:v:0", "-map", "1:a?", "-t", f"{frames / float(fps):.6f}",
                   "-c:v", "copy"]
        for codec in ("copy", "aac"):
            result = subprocess.run([*command, "-c:a", codec, "-movflags", "+faststart", str(target)])
            if not result.returncode:
                break
            target.unlink(missing_ok=True)
        else:
            raise RuntimeError("无法合入原视频音轨。")
    finally:
        video.unlink(missing_ok=True)
    return {"counts": counts, "start": start, "frames": frames, "target_duration": duration}


def main():
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    output = Path(config["output"])
    try:
        swap = config["action"] == "replace"
        detector, recognizer, swapper = setup(Path(config["weights"]), swap)
        result = replace(config, detector, recognizer, swapper) if swap else scan(config, detector, recognizer)
        output.write_text(json.dumps(result), encoding="utf-8")
    except (RuntimeError, ValueError) as error:
        output.with_suffix(".error").write_text(str(error), encoding="utf-8")
        raise


if __name__ == "__main__":
    main()
