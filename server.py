"""Loopback-only Valtz Director Workbench."""
import argparse
import contextlib
import datetime
import fcntl
import http.server
import io
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import urllib.parse
import webbrowser
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import engine
import face_swap
import mtv
import providers
import storage

BASE = Path(__file__).resolve().parent
ID = re.compile(r"[a-f0-9]{24}")
MEDIA = re.compile(r"[a-f0-9]{24}\.(?:png|jpg|mp4)")
DOCUMENT_LIMIT = 12 * 1024 * 1024
APP_TITLE = "Valtz 导演操作台"
PROJECT_MODES = ("drama", "mtv", "swap")
FACE_JOBS = ("face-scan", "face-replace", "face-export")
TRACKED_JOBS = ("breakdown", "mtv-plan", "rewrite", "cast-image", "first-image", "video", "export", "auto-shots") + FACE_JOBS
EXPORT_SHORT_EDGES = (0, 720, 1080, 1440)


def project_mode(doc):
    if doc.get("mode") in PROJECT_MODES:
        return doc["mode"]
    if ((doc.get("mtv") or {}).get("track_id") and not doc.get("script")
            and all(shot.get("mtv") for shot in doc.get("shots", []))):
        return "mtv"
    return "drama"


def uid():
    return secrets.token_hex(12)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path, data):
    temp = path.with_name(path.name + "." + uid() + ".tmp")
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def text(value, name, maximum=10000, required=False):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()) or "\0" in value:
        raise ValueError(f"请检查{name}是否填写完整或过长。")
    return value


def seconds(value):
    try:
        duration = float(value)
    except (TypeError, ValueError):
        raise ValueError("请选择 2–15 秒。")
    if not 2 <= duration <= 15:
        raise ValueError("请选择 2–15 秒。")
    return duration


def safe_name(value):
    return re.sub(r'[/:\\<>|"*?\x00-\x1f]', "_", value).strip(" .")[:70] or "未命名"


def shot_pair(doc, owner):
    for index, shot in enumerate(doc["shots"]):
        if shot["id"] == owner:
            return shot, doc["shots"][index - 1] if index else None
    raise ValueError("找不到该镜头。")


def ingest_script(data, filename):
    if filename.lower().endswith(".docx"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                part = archive.getinfo("word/document.xml")
                if part.file_size > 4_000_000:
                    raise ValueError("Word 正文过长。")
                document = ET.fromstring(archive.read(part))
        except (KeyError, zipfile.BadZipFile, ET.ParseError) as error:
            raise ValueError("无法读取此 Word 文档。") from error
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        value = "\n".join("".join(n.text or "" for n in p.findall(".//w:t", ns))
                          for p in document.findall(".//w:p", ns))
    else:
        value = data.decode("utf-8-sig")
    return text(value, "剧本", 70000, required=True)


class Workbench:
    def __init__(self, root):
        self.root = root
        self.lock = threading.RLock()
        self.busy = threading.Lock()
        self.token = secrets.token_urlsafe(32)
        self.profile = providers.connection({})
        self.custom = None
        self.last_job = None

    def check_drive(self):
        if self.root != storage.data_root():
            raise ValueError("工作盘挂载路径已变化，请重新打开操作台。")

    def project(self, key):
        self.check_drive()
        if not ID.fullmatch(key):
            raise ValueError("无效的项目编号。")
        path = self.root / key
        if not (path / "project.json").is_file():
            raise ValueError("找不到此项目。")
        return path

    def list_projects(self):
        self.check_drive()
        projects = [read_json(p) for p in self.root.glob("*/project.json")]
        return [{"id": p["id"], "name": p["name"], "mode": project_mode(p)}
                for p in reversed(sorted(projects, key=lambda p: p["updated"]))]

    def create(self, title, mode="drama"):
        self.check_drive()
        title = text(title, "项目名", 90, required=True)
        if mode not in PROJECT_MODES:
            raise ValueError("请选择一个制作模式。")
        key = uid()
        folder = self.root / key
        folder.mkdir()
        (folder / "jobs").mkdir()
        (folder / "media").mkdir()
        doc = {"id": key, "name": title, "mode": mode, "script": "", "cast": [], "shots": [],
               "updated": self.now(), "revision": 0}
        save_json(folder / "project.json", doc)
        return doc

    @staticmethod
    def now():
        return datetime.datetime.now().isoformat(timespec="milliseconds")

    def save(self, path, body):
        with self.lock:
            current = read_json(path / "project.json")
            if body.get("revision") != current["revision"]:
                raise ValueError("此项目已在另一窗口修改，请重新打开项目后编辑。")
            cleaned = {
                "id": current["id"], "name": text(body["name"], "项目名", 90, True),
                "mode": project_mode(current),
                "script": text(body["script"], "剧本", 70000),
                "cast": [], "shots": [], "updated": self.now(), "revision": current["revision"] + 1,
                "breakdown": text(body.get("breakdown") or "", "草稿编号", 24),
            }
            for collection in ("cast", "shots"):
                if not isinstance(body[collection], list) or len(body[collection]) > 300:
                    raise ValueError("人物或镜头列表过长。")
                for item in body[collection]:
                    if not isinstance(item, dict) or not ID.fullmatch(item.get("id", "")):
                        raise ValueError("无效的分镜编号。")
                    self.validate_selection(path, item)
                    if collection == "shots":
                        if "mtv" in item:
                            self.mtv_shot(path, item["mtv"])
                        self.validate_look_lock(path, item.get("lookLock"))
                    cleaned[collection].append(item)
            if body.get("mtv") is not None:
                cleaned["mtv"] = self.mtv_settings(path, body["mtv"], cleaned["cast"])
            if cleaned["mode"] == "swap":
                cleaned["swap"] = self.swap_settings(path, body.get("swap") or {})
            save_json(path / "project.json", cleaned)
            return cleaned

    def validate_selection(self, path, item):
        for key in ("adopted", "first", "last", "selected"):
            value = item.get(key, "")
            if value and (not isinstance(value, str) or not MEDIA.fullmatch(value)
                          or not (path / "media" / value).is_file()):
                raise ValueError("选中的图片或视频不在当前项目内。")

    def media(self, path, name):
        if not MEDIA.fullmatch(name):
            raise ValueError("无效的素材编号。")
        file = path / "media" / name
        if not file.is_file():
            raise ValueError("找不到该素材。")
        return file

    def image(self, path, name):
        file = self.media(path, name)
        if file.suffix not in (".jpg", ".png"):
            raise ValueError("此位置仅接受图片。")
        return file

    def validate_look_lock(self, path, lock):
        if lock is None:
            return
        if (not isinstance(lock, dict) or not isinstance(lock.get("previous"), str)
                or not ID.fullmatch(lock["previous"])):
            raise ValueError("人物造型锁定记录无效。")
        for key in ("frame", "first", "last"):
            value = lock.get(key, "")
            if not isinstance(value, str) or (key == "frame" and not value):
                raise ValueError("请为人物造型锁定选择参考帧。")
            if value:
                self.image(path, value)

    def grab_look_frame(self, path, owner, body):
        doc = read_json(path / "project.json")
        _, previous = shot_pair(doc, owner)
        if not previous or not previous.get("selected") or previous["selected"] != body.get("video"):
            raise ValueError("上一镜的已选视频已变化，请重新选择参考画面。")
        source = self.media(path, previous["selected"])
        if source.suffix != ".mp4":
            raise ValueError("上一镜没有已采用的视频。")
        duration = engine.verify_media(source, "video")["duration"]
        time = body.get("time", duration / 2)
        if type(time) not in (int, float) or not 0 <= time <= duration + 0.1:
            raise ValueError("请在上一镜视频内选择参考画面。")
        time = min(time, max(0, duration - 1 / 24))
        filename = uid() + ".jpg"
        output = path / "media" / filename
        try:
            subprocess.run([mtv.transcoder(), "-nostdin", "-v", "error", "-protocol_whitelist", "file",
                            "-ss", f"{time:.6f}", "-i", str(source), "-map", "0:v:0", "-frames:v", "1",
                            "-q:v", "2", "-threads", "1", str(output)], check=True,
                           capture_output=True, timeout=30)
            engine.verify_media(output, "image")
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            output.unlink(missing_ok=True)
            raise ValueError("无法截取该画面，请拖到上一镜中人物清晰的一帧再试。") from error
        return {"file": filename}

    def apply_look_lock(self, path, doc, job, data, first, last, refs):
        shot, previous = shot_pair(doc, job["owner"])
        lock = shot.get("lookLock")
        if not lock:
            return
        self.validate_look_lock(path, lock)
        if not previous or lock["previous"] != previous["id"]:
            raise ValueError("上一镜已变化，请重新选择延续造型的参考帧。")
        shared = set(shot.get("refs", [])) & set(previous.get("refs", []))
        people = [person["name"] for person in doc["cast"] if person["id"] in shared]
        if not people:
            raise ValueError("两镜至少要勾选一个相同的人物，才能延续造型。")
        names = "、".join(people)
        if job["kind"] == "first-image" or data.get("mode") == "Ref2VA":
            if len(refs) >= 6:
                raise ValueError("延续造型需要占用 1 张参考图，请将定妆参考减至 5 张以内。")
            label = f"图 {len(refs) + 1}" if job["kind"] == "first-image" else f"<Picture {len(refs) + 1}>"
            refs.append((self.image(path, lock["frame"]), ("上一镜造型：" + names)[:100]))
            directive = (
                f"{names}的服装款式、颜色、图案、发型与配饰沿用{label}中各自对应的人物。"
                f"服装、发型与配饰以{label}为准，即使镜头草稿对造型另有描述。人物身份仍匹配各自的定妆图。"
                "其余人物使用自己的定妆参考。构图、背景、姿势与动作按本镜要求。"
            )
        else:
            if job["kind"] == "video" and (not first or lock.get("first") != first.name
                                           or (last and lock.get("last") != last.name)):
                raise ValueError("请先在构图与参考中确认并采用与上一镜造型一致的首帧及已选尾帧。")
            directive = f"{names}在整个镜头中保持已提供首帧里的服装、发型和配饰。"
        prompt = data["prompt"].rstrip()
        # Keep guidance inside the visual fields when the draft follows the H3 schema.
        marker = re.search(r"(?m)^\s*detailed_description:" if data.get("mode") == "Ref2VA"
                           else r"(?m)^\s*overall_soundscape:", prompt) if job["kind"] == "video" else None
        data["prompt"] = (prompt[:marker.start()].rstrip() + "\n" + directive + "\n\n" + prompt[marker.start():].lstrip()
                          if marker else prompt + "\n\n" + directive)

    def upload_image(self, path, owner, data, purpose):
        doc = read_json(path / "project.json")
        keys = {"cast": {"cast-source"}, "shots": {"first-upload", "last-upload"}}
        if not any(item["id"] == owner and purpose in allowed
                   for group, allowed in keys.items() for item in doc[group]):
            raise ValueError("先添加并保存人物或镜头。")
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            suffix = ".png"
        elif data.startswith(b"\xff\xd8\xff"):
            suffix = ".jpg"
        else:
            raise ValueError("请使用 PNG 或 JPEG 图片。")
        key = uid()
        filename = key + suffix
        (path / "media" / filename).write_bytes(data)
        job = {"id": key, "owner": owner, "kind": purpose, "status": "done",
               "created": self.now(), "result": {"file": filename}}
        save_json(path / "jobs" / (key + ".json"), job)
        return job

    def jobs(self, path):
        return sorted((read_json(p) for p in (path / "jobs").glob("*.json")), key=lambda job: (job["created"], job["id"]))

    def swap_source(self, path, key):
        if not ID.fullmatch(key or ""):
            raise ValueError("请先上传换脸视频。")
        record = path / "face-media" / (key + ".json")
        if not record.is_file():
            raise ValueError("未找到原视频。")
        return read_json(record)

    def upload_swap_video(self, path, name, stream, length):
        self.require_mode(path, "swap")
        key = uid()
        folder = path / "face-media"
        record = face_swap.import_video(folder, key, name, stream, length)
        self.check_drive()
        save_json(folder / (key + ".json"), record)
        return record

    def require_mode(self, path, mode):
        if project_mode(read_json(path / "project.json")) != mode:
            raise ValueError("请先打开对应制作模式的项目。")

    def job_record(self, path, job_id, kind):
        if not ID.fullmatch(job_id or ""):
            raise ValueError("任务编号无效。")
        file = path / "jobs" / (job_id + ".json")
        if not file.is_file():
            raise ValueError("找不到相关任务。")
        result = read_json(file)
        if result["kind"] != kind or result["status"] != "done":
            raise ValueError("相关任务还没有成功完成。")
        return result

    def swap_settings(self, path, settings):
        video = text(settings.get("video", ""), "视频编号", 24)
        scan = text(settings.get("scan", ""), "识别任务编号", 24)
        mappings = settings.get("mappings", {})
        if video:
            self.swap_source(path, video)
        if scan:
            result = self.job_record(path, scan, "face-scan")["result"]
            if result["video_id"] != video:
                raise ValueError("识别结果与当前视频不一致。")
            faces = {face["id"] for face in result["faces"]}
            if not isinstance(mappings, dict) or not set(mappings).issubset(faces):
                raise ValueError("参考图必须对应到当前视频的人脸。")
            for file in mappings.values():
                self.image(path, file)
        elif mappings:
            raise ValueError("请先识别人脸。")
        return {"video": video, "scan": scan, "mappings": mappings}

    def upload_swap_reference(self, path, data):
        self.require_mode(path, "swap")
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            suffix = ".png"
        elif data.startswith(b"\xff\xd8\xff"):
            suffix = ".jpg"
        else:
            raise ValueError("请上传单人 PNG 或 JPEG 参考图。")
        name = uid() + suffix
        (path / "media" / name).write_bytes(data)
        return {"file": name}

    def track(self, path, key):
        if not mtv.TRACK_ID.fullmatch(key or ""):
            raise ValueError("音轨编号无效。")
        metadata = path / "tracks" / (key + ".json")
        if not metadata.is_file():
            raise ValueError("找不到所选音轨，请重新导入。")
        return read_json(metadata)

    def upload_track(self, path, name, stream, length):
        key = uid()
        folder = path / "tracks"
        record = mtv.import_audio(folder, key, name, stream, length)
        self.check_drive()
        save_json(folder / (key + ".json"), record)
        return record

    def mtv_settings(self, path, settings, cast):
        if not isinstance(settings, dict):
            raise ValueError("MTV 项目设置无效。")
        style = settings.get("style", "cinematic")
        treatment = settings.get("treatment", "narrative")
        if style not in mtv.STYLE or treatment not in mtv.TREATMENTS:
            raise ValueError("请选择已有的 MTV 形式和风格。")
        count = settings.get("count", 1)
        ids = settings.get("cast_ids", [])
        available = {person["id"] for person in cast}
        if (type(count) is not int or not 0 <= count <= 5 or not isinstance(ids, list) or len(ids) > count
                or any(not isinstance(key, str) or key not in available for key in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError("请确认 MTV 人物名单，人数为 0–5 人。")
        track_id = text(settings.get("track_id", ""), "音轨编号", 24)
        if track_id:
            self.track(path, track_id)
        return {"style": style, "treatment": treatment, "count": count, "cast_ids": ids,
                "track_id": track_id, "lyrics": text(settings.get("lyrics", ""), "歌词", 30000),
                "direction": text(settings.get("direction", ""), "导演要求", 3000),
                "cues": text(settings.get("cues", ""), "时间锚点", 10000),
                "adopted_plan": text(settings.get("adopted_plan", ""), "草稿编号", 24)}

    def mtv_shot(self, path, data):
        track = self.track(path, data["track_id"])
        start, stop = data["start_ms"], data["end_ms"]
        if (type(start) is not int or type(stop) is not int
                or not 0 <= start < stop <= round(track["duration"] * 1000)):
            raise ValueError("MTV 镜头的原音时间超出音轨范围。")
        return track

    def start(self, path, body):
        if not self.busy.acquire(blocking=False):
            raise ValueError("请等待工作台当前任务完成。")
        try:
            kind = body.get("kind")
            if kind not in TRACKED_JOBS:
                raise ValueError("无效任务。")
            doc = read_json(path / "project.json")
            mode = project_mode(doc)
            if (kind in FACE_JOBS) != (mode == "swap") or (kind == "mtv-plan" and mode != "mtv") or (kind == "breakdown" and mode != "drama"):
                raise ValueError("当前项目模式不支持这个任务。")
            group = "cast" if kind == "cast-image" else "shots"
            owner = body.get("owner", "")
            if kind == "auto-shots":
                keys = body.get("shots")
                if (not isinstance(keys, list) or not keys or not all(isinstance(key, str) for key in keys)
                        or len(keys) != len(set(keys)) or not set(keys) <= {shot["id"] for shot in doc["shots"]}):
                    raise ValueError("请选择当前项目内要自动生成的镜头。")
            if kind not in ("breakdown", "mtv-plan", "export", "auto-shots", *FACE_JOBS) and not any(x["id"] == owner for x in doc[group]):
                raise ValueError("找不到本次任务对应的人物或镜头。")
            if kind in FACE_JOBS:
                owner = doc.get("swap", {}).get("video", "")
            key = uid()
            job = {"id": key, "owner": owner, "kind": kind, "status": "running", "created": self.now()}
            if kind in ("breakdown", "mtv-plan", "rewrite"):
                job["text_model"] = self.profile["model"]
            save_json(path / "jobs" / (key + ".json"), job)
            self.last_job = (path, key)
            worker = threading.Thread(target=self.run, args=(path, job, body, doc, self.profile.copy()), daemon=False)
            worker.start()
        except Exception:
            self.busy.release()
            raise
        return job

    def run(self, path, job, data, doc, profile):
        try:
            job["result"] = self.execute(path, job, data, doc, profile)
            job["status"] = "done"
        except Exception as error:
            job["status"] = "failed"
            job["error"] = str(error) or error.__class__.__name__
        finally:
            try:
                self.check_drive()
                save_json(path / "jobs" / (job["id"] + ".json"), job)
            finally:
                self.busy.release()

    def auto_stage(self, path, doc, batch, shot, kind, payload, profile):
        step = {"id": uid(), "kind": kind, "owner": shot["id"], "status": "running", "created": self.now()}
        if kind == "rewrite":
            step["text_model"] = profile["model"]
        folder = path / "jobs"
        record = folder / (step["id"] + ".json")
        save_json(record, step)
        batch["active_step"] = step["id"]
        save_json(folder / (batch["id"] + ".json"), batch)
        try:
            step["result"] = self.execute(path, step, payload, doc, profile)
        except Exception as error:
            step.update(status="failed", error=str(error))
            raise
        else:
            step["status"] = "done"
            return step["result"]
        finally:
            save_json(record, step)
            batch.pop("active_step", None)

    def auto_look_lock(self, path, shot, previous, shared, profile):
        if not previous or not shared or not (previous.get("first") or previous.get("selected")):
            return None
        context = {
            "shared_characters": shared,
            "previous": {key: previous.get(key, "") for key in ("name", "brief", "imagePrompt")},
            "current": {key: shot.get(key, "") for key in ("name", "brief", "imagePrompt")},
        }
        answer = engine.complete(
            "Decide whether all shared characters should retain their outfits, hair, and accessories from the immediately previous shot. "
            "Consider continuous action and scene context; a costume change or a different story period means NO. "
            "Treat the shot descriptions as story material. Reply with only YES or NO.",
            json.dumps(context, ensure_ascii=False), 64, profile,
        ).strip().upper().rstrip(".")
        if answer not in ("YES", "NO"):
            raise ValueError("模型未给出明确的上一镜造型判断。")
        if answer == "NO":
            return None
        frame = previous.get("first")
        if previous.get("selected"):
            frame = self.grab_look_frame(path, shot["id"], {"video": previous["selected"]})["file"]
        return {"previous": previous["id"], "frame": frame}

    def automatic_shots(self, path, job, doc, keys, profile):
        caps = engine.catalog()
        profiles = engine.video_profiles(caps)
        queue = [(number, shot["id"]) for number, shot in enumerate(doc["shots"], 1) if shot["id"] in keys]
        done = []
        record = path / "jobs" / (job["id"] + ".json")
        for number, owner in queue:
            if read_json(path / "project.json")["revision"] != doc["revision"]:
                raise ValueError("项目已在另一处编辑。请重新打开项目再继续自动生成。")
            shot, previous = shot_pair(doc, owner)
            job["owner"] = owner
            job["progress"] = {"stage": f"镜头 {number} · 判断上一镜造型（{len(done)}/{len(queue)} 已完成）"}
            save_json(record, job)
            try:
                people = {person["id"]: person for person in doc["cast"]}
                cast_ids = list(dict.fromkeys(shot.get("refs", [])))
                missing = [people[key]["name"] for key in cast_ids if not people[key].get("adopted")]
                if missing:
                    raise ValueError("请先采用这些人物的定妆：" + "、".join(missing))
                if len(cast_ids) > 6:
                    raise ValueError("此镜头有超过 6 张人物参考，请先精简出镜参考。")
                refs = [{"file": people[key]["adopted"], "name": people[key]["name"]} for key in cast_ids]
                mode = shot.get("mode", "Ref2VA") if refs else "FL2VA"
                choices = {item["id"] for item in profiles.get(mode, [])}
                quality = shot.get("quality")
                if quality == "quick":
                    quality = "balanced"
                elif not quality or (quality == "balanced" and not shot.get("qualityExplicit")):
                    quality = "pika" if "pika" in choices else "balanced"
                if quality not in choices:
                    raise ValueError("该镜头的 H3 视频模型或生成档位不可用。")
                shared = [people[key]["name"] for key in cast_ids if previous and key in previous.get("refs", [])]
                look = self.auto_look_lock(path, shot, previous, shared, profile)
                if look and len(refs) == 6:
                    raise ValueError("延续造型需要额外 1 张参考图，请将定妆图减到 5 张。")
                if look and mode == "FL2VA" and shot.get("last"):
                    raise ValueError("该镜头有尾帧，请手动确认尾帧造型后制作。")
                shot["lookLock"] = look
                shot["mode"] = mode
                image_prompt = shot.get("imagePrompt") or shot.get("brief") or shot.get("prompt")
                brief = shot.get("brief") or shot.get("prompt") or image_prompt
                use_ref = bool(refs or look)
                image_options = caps.get("image-edit" if use_ref else "text-to-image", [])
                model_key = shot.get("editModel" if use_ref else "imageModel")
                model = next((item["id"] for item in image_options if item["id"] == model_key),
                             image_options[0]["id"] if image_options else None)
                if not model:
                    raise ValueError("缺少当前镜头所需的生图模型。")
                inputs = {"refs": refs, "ratio": shot["ratio"]}
                job["progress"]["stage"] = f"镜头 {number} · 生成首帧"
                result = self.auto_stage(path, doc, job, shot, "first-image",
                                         {**inputs, "prompt": image_prompt, "model": model}, profile)
                shot["first"] = result["file"]
                if look:
                    look["first"] = shot["first"]
                doc = self.save(path, doc)
                shot, _ = shot_pair(doc, owner)
                short_edge = min(map(int, shot["size"].split("x")))
                video_refs = refs
                if mode == "Ref2VA" and len(refs) + bool(look) < 6:
                    video_refs = [*refs, {"file": shot["first"], "name": "本镜首帧构图参考"}]
                request = {**inputs, "refs": video_refs,
                           "mode": mode, "first": shot["first"], "last": shot.get("last"),
                           "seconds": shot["seconds"], "size": shot["size"], "quality": quality,
                           "image_prompt": image_prompt, "model": engine.H3_VIDEO[mode],
                           "refine": shot.get("refine", "auto") if short_edge > 768 else "off"}
                job["progress"]["stage"] = f"镜头 {number} · 起草 H3 提示词"
                draft = self.auto_stage(path, doc, job, shot, "rewrite", {**request, "prompt": brief}, profile)
                shot["prompt"] = draft["prompt"]
                doc = self.save(path, doc)
                job["progress"]["stage"] = f"镜头 {number} · 生成视频候选"
                result = self.auto_stage(path, doc, job, shot, "video", {**request, "prompt": draft["prompt"]}, profile)
                done.append({"shot": owner, "file": result["file"]})
            except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
                raise ValueError(f"镜头 {number}《{shot['name']}》暂停，已完成 {len(done)}/{len(queue)} 镜。{error}") from error
        return {"shots": done}

    def execute(self, path, job, data, doc, profile):
        self.check_drive()
        kind = job["kind"]
        if kind == "auto-shots":
            return self.automatic_shots(path, job, doc, data["shots"], profile)
        if kind in FACE_JOBS:
            options = self.swap_settings(path, doc.get("swap") or {})
            video = self.swap_source(path, options["video"])
            if kind == "face-export":
                result = self.job_record(path, data.get("result_id"), "face-replace")["result"]
                if result["preview"] or result["video_id"] != video["id"]:
                    raise ValueError("请先完成当前视频的整片换脸。")
                src = self.media(path, result["file"])
                folder = Path.home() / "Downloads" / "Valtz 导演操作台" / safe_name(doc["name"])
                folder.mkdir(parents=True, exist_ok=True)
                destination = folder / (safe_name(Path(video["name"]).stem) + "_换脸_" + job["id"][:8] + ".mp4")
                with src.open("rb") as incoming, destination.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
                return {"file": str(destination)}
            source = path / "face-media" / video["file"]
            if kind == "face-scan":
                return face_swap.run(self.root, path, job, source, video)
            if not options["mappings"]:
                raise ValueError("请为至少一个识别出的人脸选择参考图。")
            refs = {key: self.image(path, value) for key, value in options["mappings"].items()}
            return face_swap.run(self.root, path, job, source, video, options["scan"], refs, bool(data.get("preview")))
        if kind == "breakdown":
            return engine.script_breakdown(text(doc["script"], "剧本", 70000, True), profile)
        if kind == "mtv-plan":
            settings = self.mtv_settings(path, doc.get("mtv", {}), doc["cast"])
            track = self.track(path, settings["track_id"])
            cast = [person for person in doc["cast"] if person["id"] in settings["cast_ids"]]
            if len(cast) != settings["count"] or any(not person.get("adopted") for person in cast):
                raise ValueError("先为选定人数采用定妆图，或选择 0 人的意象 MTV。")
            return mtv.draft(settings, track, cast, profile)
        if kind == "export":
            return self.export(path, job, doc, data.get("format"), data.get("short_edge", 0))
        data["prompt"] = text(data.get("prompt"), "提示词", required=True)
        first = last = None
        if kind in ("rewrite", "video"):
            mode = data.get("mode")
            if mode not in engine.H3_VIDEO:
                raise ValueError("请选择 FL2VA 或 Ref2VA。")
            data["seconds"] = seconds(data.get("seconds"))
            if data.get("size") not in engine.SIZES.get(data.get("ratio"), []):
                raise ValueError("请从当前比例的尺寸列表选择。")
            if mode == "FL2VA":
                first = self.image(path, data["first"]) if data.get("first") else None
                last = self.image(path, data["last"]) if data.get("last") else None
        elif kind == "first-image":
            data["size"] = engine.FIRST_SIZES.get(data.get("ratio"), engine.IMAGE_SIZE)
        else:
            data["size"] = engine.IMAGE_SIZE
        refs = []
        if kind in ("cast-image", "first-image") or data.get("mode") == "Ref2VA":
            for ref in data.get("refs", [])[:6]:
                refs.append((self.image(path, ref["file"]), text(ref["name"], "参考图名称", 100, True)))
        if kind in ("first-image", "rewrite", "video"):
            self.apply_look_lock(path, doc, job, data, first, last, refs)
        if kind == "rewrite":
            return engine.h3_prompt(data, (first, last, refs), profile)
        if data["prompt"].startswith("@"):
            raise ValueError("提示词开头请使用文字，避免被 Valtz 当成素材编号。")
        return engine.generate(self.root, path, job, data, (first, last, refs))

    def export(self, path, job, doc, fmt, short_edge=0):
        if fmt not in engine.FORMATS:
            raise ValueError("请选择导出格式。")
        if type(short_edge) is not int or short_edge not in EXPORT_SHORT_EDGES:
            raise ValueError("请选择有效的导出分辨率。")
        chosen = [(index, shot) for index, shot in enumerate(doc["shots"], 1) if shot.get("selected")]
        if not chosen:
            raise ValueError("请先选中至少一条视频。")
        assets = {j.get("result", {}).get("file"): j["result"]["asset"] for j in self.jobs(path)
                  if j["kind"] == "video" and j["status"] == "done"}
        if any(shot["selected"] not in assets for _, shot in chosen):
            raise ValueError("有选片缺少对应的 Valtz 素材。")
        tracks = {}
        for _, shot in chosen:
            if shot.get("mtv"):
                track = self.mtv_shot(path, shot["mtv"])
                tracks[track["id"]] = track
        output_root = Path.home() / "Downloads" / "Valtz 导演操作台" / safe_name(doc["name"])
        output = output_root / (datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + job["id"][:6])
        output_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".export-", dir=output_root) as folder:
            files = self.render_export(path, job, doc, fmt, short_edge, chosen, assets, tracks, Path(folder))
            Path(folder).rename(output)
        return {"directory": str(output), "files": files}

    def render_export(self, path, job, doc, fmt, short_edge, chosen, assets, tracks, output):
        ffmpeg = mtv.transcoder() if short_edge else None
        env = engine.runtime_env(self.root) if fmt != "h264" else None
        log = path / "jobs" / (job["id"] + ".log")
        files = []
        placements = []
        masters = {}
        dimensions = {}
        count = len(chosen)
        with (engine.gpu_gate() if fmt != "h264" else contextlib.nullcontext()):
            for completed, (index, shot) in enumerate(chosen):
                selected = shot["selected"]
                filename = f"{index:02d}_{safe_name(shot['name'])}{engine.FORMATS[fmt]}"
                file = output / filename
                source = self.media(path, selected)
                size = engine.export_size(engine.verify_media(source, "video")["size"], short_edge) if short_edge else None
                native = output / ".source.mov" if size and fmt != "h264" else file
                if fmt == "h264":
                    if not size:
                        shutil.copyfile(source, file)
                else:
                    engine.run_ctl(["export", path / "镜头素材.valtz", assets[selected], "--format", fmt, "-o", native], env, log)
                    source = native
                if size:
                    with log.open("a", encoding="utf-8") as report:
                        report.write(f"[PROGRESS] {completed * 100 // count}% of 'export resolution'\n")
                    engine.scale_export(ffmpeg, source, file, fmt, size, log)
                    if native != file:
                        native.unlink()
                checked = engine.verify_media(file, "video")
                if size and checked["size"] != f"{size[0]}x{size[1]}":
                    raise ValueError(f"{filename} 的导出尺寸与设定不符。")
                dimensions[filename] = checked["size"]
                files.append(filename)
                if short_edge:
                    with log.open("a", encoding="utf-8") as report:
                        report.write(f"[PROGRESS] {len(files) * 100 // count}% of 'export resolution'\n")
                if shot.get("mtv"):
                    placement = shot["mtv"]
                    placements.append({"file": filename, "song": placement["track_id"],
                                       "start_ms": placement["start_ms"], "end_ms": placement["end_ms"]})
        for number, (key, track) in enumerate(tracks.items(), 1):
            name = f"歌曲原音_{number:02d}_{safe_name(Path(track['name']).stem)}.wav"
            shutil.copyfile(path / "tracks" / track["preview"], output / name)
            masters[key] = name
        manifest = {"project": doc["name"], "files": files}
        if short_edge:
            manifest.update(target_short_edge=short_edge, resolutions=dimensions)
        if placements:
            manifest.update(mtv_placements=placements, song_audio=masters)
        save_json(output / "镜头清单.json", manifest)
        return files

    def recover(self):
        files = sorted(self.root.glob("*/jobs/*.json"), key=lambda file: file.stat().st_mtime)
        for file in files:
            job = read_json(file)
            if job["status"] == "running":
                job["status"] = "failed"
                job["error"] = "上次服务已关闭。请看任务记录或确认现有素材，系统没有自动重做。"
                save_json(file, job)
            if job["kind"] in TRACKED_JOBS:
                self.last_job = (file.parent.parent, job["id"])

    def task_status(self):
        self.check_drive()
        tracked = self.last_job
        if not tracked:
            return None
        project, key = tracked
        job = read_json(project / "jobs" / (key + ".json"))
        document = read_json(project / "project.json")
        job["project"] = {"id": document["id"], "name": document["name"]}
        owner = next((item["name"] for item in document["cast"] + document["shots"] if item["id"] == job["owner"]), "")
        job["owner_name"] = owner
        if job["status"] == "running":
            step = job.get("active_step", key)
            measured = native_progress(project / "jobs" / (step + ".log"))
            progress = job.get("progress")
            if progress and measured:
                progress = {**measured, "stage": f"{progress['stage']} · {measured['stage']}"}
            job["progress"] = progress or measured
        # Results can contain entire prompts. The floating monitor only needs status.
        job.pop("result", None)
        return job


def native_progress(log):
    if not log.is_file():
        return None
    with log.open("rb") as stream:
        stream.seek(max(0, log.stat().st_size - 24_000))
        tail = stream.read(24_000).decode("utf-8", "replace")
    for line in reversed(tail.splitlines()):
        match = re.search(r"\[PROGRESS\] (\d+)% of '([^']+)'", line)
        if match:
            return {"stage": match[2][:65], "percent": min(100, int(match[1]))}
        match = re.search(r"\[PROGRESS\] '([^']+)' ended", line)
        if match:
            return {"stage": match[1][:65], "percent": 100}
    return None


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def json(self, data, status=200):
        self.reply(json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def reply(self, data, mime, status=200):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(data)

    def handle_request(self, writing=False):
        try:
            self.dispatch(writing)
        except (BrokenPipeError, ConnectionResetError):
            return
        except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
            self.json({"error": str(error) or error.__class__.__name__}, 400)
        except Exception as error:
            self.json({"error": f"本机操作失败：{error}"}, 500)

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request(writing=True)

    def dispatch(self, writing):
        app = self.server.app
        if self.headers.get("Host") != self.server.host_header:
            self.json({"error": "此工作台仅接受本机地址。"}, 403)
            return
        if writing:
            key = self.headers.get("X-Director-Key", "")
            key_valid = secrets.compare_digest(key, app.token)
            # In a tab kept open across a local restart, the old key is stale.
            # Permit the browser's verified same-origin POST, still requiring
            # the workbench header. Cross-origin and opaque origins stay blocked.
            same_tab = (
                re.fullmatch(r"[A-Za-z0-9_-]{43}", key)
                and self.headers.get("Sec-Fetch-Site") == "same-origin"
                and self.headers.get("Origin") == "http://" + self.server.host_header
            )
            if not key_valid and not same_tab:
                self.json({"error": "请刷新工作台页面。"}, 403)
                return
        url = urllib.parse.urlsplit(self.path)
        parts = url.path.strip("/").split("/")
        if not writing and url.path in ("/", "/app.js", "/mtv.js", "/face.js", "/app.css"):
            file = BASE / ("index.html" if url.path == "/" else url.path[1:])
            data = file.read_bytes()
            if url.path == "/":
                data = data.replace(b"__DIRECTOR_KEY__", app.token.encode("ascii"))
            self.reply(data, (mimetypes.guess_type(file.name)[0] or "text/html") + "; charset=utf-8")
            return
        if parts == ["api", "capabilities"] and not writing:
            caps, native, profiles, valtz_error = {}, None, {}, ""
            try:
                caps = engine.catalog()
                native = storage.app_version()
                profiles = engine.video_profiles(caps)
            except (OSError, ValueError, subprocess.SubprocessError):
                valtz_error = "Valtz 或其模型盘未就绪。"
            try:
                default = providers.connection({})
                splash = default["model"] in providers.model_ids(default)
            except (OSError, ValueError, KeyError, AttributeError):
                splash = False
            self.json({"valtz": caps, "native": native, "profiles": profiles, "valtz_error": valtz_error,
                       "sizes": engine.SIZES, "splash": splash, "modes": list(PROJECT_MODES),
                       "face_swap": face_swap.available(app.root), "look_lock": True,
                       "export_short_edges": list(EXPORT_SHORT_EDGES), "auto_shots": True})
            return
        if parts == ["api", "task-status"] and not writing:
            self.json(app.task_status())
            return
        if parts == ["api", "model"]:
            if writing:
                settings = providers.connection(self.body(), app.custom)
                if settings["mode"] == "custom":
                    app.custom = settings
                elif settings["mode"] == "codex":
                    providers.check_codex_login()
                app.profile = settings
            self.json(providers.public_details(app.profile, app.custom))
            return
        if parts == ["api", "model", "test"] and writing:
            settings = providers.connection(self.body(), app.custom)
            if settings["mode"] == "codex":
                providers.check_codex_login()
                self.json({"mode": "codex", "matched": True})
                return
            ids = providers.model_ids(settings)
            self.json({"count": len(ids), "matched": settings["model"] in ids})
            return
        if parts == ["api", "projects"]:
            if writing:
                request = self.body()
                self.json(app.create(request["name"], request.get("mode", "drama")))
            else:
                self.json(app.list_projects())
            return
        if len(parts) >= 3 and parts[:2] == ["api", "projects"]:
            project = app.project(parts[2])
            if len(parts) == 3:
                if writing:
                    self.json(app.save(project, self.body()))
                else:
                    doc = read_json(project / "project.json")
                    self.json({**doc, "mode": project_mode(doc)})
                return
            action = parts[3]
            if action == "mtv-audio":
                app.require_mode(project, "mtv")
                if writing and len(parts) == 4:
                    name = urllib.parse.parse_qs(url.query).get("filename", [""])[0]
                    self.json(app.upload_track(project, name, self.rfile, int(self.headers.get("Content-Length", "0"))))
                    return
                if not writing and len(parts) in (5, 6):
                    track = app.track(project, parts[4])
                    if len(parts) == 5:
                        self.json(track)
                        return
                    if parts[5] == "preview":
                        self.send_media(project / "tracks" / track["preview"])
                        return
            if action == "swap-video":
                app.require_mode(project, "swap")
                if writing and len(parts) == 4:
                    name = urllib.parse.parse_qs(url.query).get("filename", [""])[0]
                    self.json(app.upload_swap_video(project, name, self.rfile, int(self.headers.get("Content-Length", "0"))))
                    return
                if not writing and len(parts) in (5, 6):
                    source = app.swap_source(project, parts[4])
                    if len(parts) == 5:
                        self.json(source)
                        return
                    if parts[5] == "preview":
                        self.send_media(project / "face-media" / source["file"])
                        return
            if action == "swap-reference" and writing and len(parts) == 4:
                self.json(app.upload_swap_reference(project, self.raw_body()))
                return
            if action == "jobs" and len(parts) == 4:
                self.json(app.start(project, self.body()) if writing else app.jobs(project))
                return
            if action == "look-frame" and len(parts) == 5 and writing:
                self.json(app.grab_look_frame(project, parts[4], self.body()))
                return
            if action == "media" and len(parts) == 5 and not writing:
                self.send_media(app.media(project, parts[4]))
                return
            if action == "log" and len(parts) == 5 and not writing and ID.fullmatch(parts[4]):
                log = project / "jobs" / (parts[4] + ".log")
                with log.open("rb") as stream:
                    stream.seek(max(0, log.stat().st_size - 60_000))
                    self.reply(stream.read(), "text/plain; charset=utf-8")
                return
            if action == "script" and len(parts) == 4 and writing:
                query = urllib.parse.parse_qs(url.query)
                self.json({"script": ingest_script(self.raw_body(), query["filename"][0])})
                return
            if action == "upload" and len(parts) == 5 and writing:
                query = urllib.parse.parse_qs(url.query)
                self.json(app.upload_image(project, parts[4], self.raw_body(), query["purpose"][0]))
                return
        self.json({"error": "页面不存在。"}, 404)

    def raw_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= DOCUMENT_LIMIT:
            raise ValueError("上传内容为空或超过 12 MB。")
        return self.rfile.read(length)

    def body(self):
        return json.loads(self.raw_body())

    def send_media(self, file):
        size = file.stat().st_size
        start, end = 0, size - 1
        header = self.headers.get("Range")
        if header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
            if not match or (not match[1] and not match[2]):
                self.send_error(416)
                return
            if match[1]:
                start = int(match[1])
                end = min(end, int(match[2])) if match[2] else end
            else:
                start = max(0, size - int(match[2]))
            if not 0 <= start <= end:
                self.send_error(416)
                return
        self.send_response(206 if header else 200)
        self.send_header("Content-Type", mimetypes.guess_type(file.name)[0])
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("X-Content-Type-Options", "nosniff")
        if header:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with file.open("rb") as source:
            source.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = source.read(min(remaining, 1024 * 1024))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8776)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    root = storage.data_root()
    root.mkdir(parents=True, exist_ok=True)
    address = f"http://127.0.0.1:{args.port}/"
    with (root / ".service.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not args.no_open:
                webbrowser.open(address)
            return
        app = Workbench(root)
        app.recover()
        with http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as service:
            service.app = app
            service.host_header = f"127.0.0.1:{args.port}"
            print(f"{APP_TITLE} V0.4  {address}", flush=True)
            if not args.no_open:
                webbrowser.open(address)
            with contextlib.suppress(KeyboardInterrupt):
                service.serve_forever()


if __name__ == "__main__":
    main()
