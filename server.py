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
import threading
import urllib.parse
import webbrowser
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import engine
import providers
import storage

BASE = Path(__file__).resolve().parent
ID = re.compile(r"[a-f0-9]{24}")
MEDIA = re.compile(r"[a-f0-9]{24}\.(?:png|jpg|mp4)")
DOCUMENT_LIMIT = 12 * 1024 * 1024
APP_TITLE = "Valtz 导演操作台"


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
        return [{"id": p["id"], "name": p["name"]} for p in reversed(sorted(projects, key=lambda p: p["updated"]))]

    def create(self, title):
        self.check_drive()
        title = text(title, "项目名", 90, required=True)
        key = uid()
        folder = self.root / key
        folder.mkdir()
        (folder / "jobs").mkdir()
        (folder / "media").mkdir()
        doc = {"id": key, "name": title, "script": "", "cast": [], "shots": [], "updated": self.now(), "revision": 0}
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
                    cleaned[collection].append(item)
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

    def start(self, path, body):
        if not self.busy.acquire(blocking=False):
            raise ValueError("请等待工作台当前任务完成。")
        try:
            kind = body.get("kind")
            if kind not in ("breakdown", "rewrite", "cast-image", "first-image", "video", "export"):
                raise ValueError("无效任务。")
            doc = read_json(path / "project.json")
            group = "cast" if kind == "cast-image" else "shots"
            owner = body.get("owner", "")
            if kind not in ("breakdown", "export") and not any(x["id"] == owner for x in doc[group]):
                raise ValueError("找不到本次任务对应的人物或镜头。")
            key = uid()
            job = {"id": key, "owner": owner, "kind": kind, "status": "running", "created": self.now()}
            if kind in ("breakdown", "rewrite"):
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

    def execute(self, path, job, data, doc, profile):
        self.check_drive()
        kind = job["kind"]
        if kind == "breakdown":
            return engine.script_breakdown(text(doc["script"], "剧本", 70000, True), profile)
        if kind == "export":
            return self.export(path, job, doc, data.get("format"))
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
        if kind == "rewrite":
            return engine.h3_prompt(data, (first, last, refs), profile)
        if data["prompt"].startswith("@"):
            raise ValueError("提示词开头请使用文字，避免被 Valtz 当成素材编号。")
        return engine.generate(self.root, path, job, data, (first, last, refs))

    def export(self, path, job, doc, fmt):
        if fmt not in engine.FORMATS:
            raise ValueError("请选择导出格式。")
        chosen = [(index, shot) for index, shot in enumerate(doc["shots"], 1) if shot.get("selected")]
        if not chosen:
            raise ValueError("请先选中至少一条视频。")
        assets = {j.get("result", {}).get("file"): j["result"]["asset"] for j in self.jobs(path)
                  if j["kind"] == "video" and j["status"] == "done"}
        if any(shot["selected"] not in assets for _, shot in chosen):
            raise ValueError("有选片缺少对应的 Valtz 素材。")
        output_root = Path.home() / "Downloads" / "Valtz 导演操作台" / safe_name(doc["name"])
        output = output_root / (datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + job["id"][:6])
        output.mkdir(parents=True)
        env = engine.runtime_env(self.root) if fmt != "h264" else None
        files = []
        with (engine.gpu_gate() if fmt != "h264" else contextlib.nullcontext()):
            for index, shot in chosen:
                selected = shot["selected"]
                filename = f"{index:02d}_{safe_name(shot['name'])}{engine.FORMATS[fmt]}"
                file = output / filename
                if fmt == "h264":
                    shutil.copyfile(self.media(path, selected), file)
                else:
                    engine.run_ctl(["export", path / "镜头素材.valtz", assets[selected], "--format", fmt, "-o", file],
                                   env, path / "jobs" / (job["id"] + ".log"))
                engine.verify_media(file, "video")
                files.append(filename)
        save_json(output / "镜头清单.json", {"project": doc["name"], "files": files})
        return {"directory": str(output), "files": files}

    def recover(self):
        files = sorted(self.root.glob("*/jobs/*.json"), key=lambda file: file.stat().st_mtime)
        for file in files:
            job = read_json(file)
            if job["status"] == "running":
                job["status"] = "failed"
                job["error"] = "上次服务已关闭。请看任务记录或确认现有素材，系统没有自动重做。"
                save_json(file, job)
            if job["kind"] in ("breakdown", "rewrite", "cast-image", "first-image", "video", "export"):
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
            job["progress"] = native_progress(project / "jobs" / (key + ".log"))
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
        if writing and not secrets.compare_digest(self.headers.get("X-Director-Key", ""), app.token):
            self.json({"error": "请刷新工作台页面。"}, 403)
            return
        url = urllib.parse.urlsplit(self.path)
        parts = url.path.strip("/").split("/")
        if not writing and url.path in ("/", "/app.js", "/app.css"):
            file = BASE / ({"/": "index.html", "/app.js": "app.js", "/app.css": "app.css"}[url.path])
            data = file.read_bytes()
            if url.path == "/":
                data = data.replace(b"__DIRECTOR_KEY__", app.token.encode("ascii"))
            self.reply(data, (mimetypes.guess_type(file.name)[0] or "text/html") + "; charset=utf-8")
            return
        if parts == ["api", "capabilities"] and not writing:
            caps = engine.catalog()
            try:
                default = providers.connection({})
                splash = default["model"] in providers.model_ids(default)
            except (OSError, ValueError, KeyError, AttributeError):
                splash = False
            self.json({"valtz": caps, "native": storage.app_version(), "profiles": engine.video_profiles(caps),
                       "sizes": engine.SIZES, "splash": splash})
            return
        if parts == ["api", "task-status"] and not writing:
            self.json(app.task_status())
            return
        if parts == ["api", "model"]:
            if writing:
                settings = providers.connection(self.body(), app.custom)
                if settings["mode"] == "custom":
                    app.custom = settings
                app.profile = settings
            self.json(providers.public_details(app.profile, app.custom))
            return
        if parts == ["api", "model", "test"] and writing:
            settings = providers.connection(self.body(), app.custom)
            ids = providers.model_ids(settings)
            self.json({"count": len(ids), "matched": settings["model"] in ids})
            return
        if parts == ["api", "projects"]:
            self.json(app.create(self.body()["name"]) if writing else app.list_projects())
            return
        if len(parts) >= 3 and parts[:2] == ["api", "projects"]:
            project = app.project(parts[2])
            if len(parts) == 3:
                self.json(app.save(project, self.body()) if writing else read_json(project / "project.json"))
                return
            action = parts[3]
            if action == "jobs" and len(parts) == 4:
                self.json(app.start(project, self.body()) if writing else app.jobs(project))
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
    storage.model_root()
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
            print(f"{APP_TITLE} V0.2  {address}", flush=True)
            if not args.no_open:
                webbrowser.open(address)
            with contextlib.suppress(KeyboardInterrupt):
                service.serve_forever()


if __name__ == "__main__":
    main()
