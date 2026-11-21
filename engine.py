"""Local Splash and Valtz adapters for the Director Workbench."""
import contextlib
import fcntl
import json
import os
import re
import secrets
import shutil
import subprocess
import urllib.request
from pathlib import Path

import providers
import storage
from storage import CTL

HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SKILL = Path(os.environ.get("VALTZ_DIRECTOR_H3_SKILL", str(Path.home() / ".dsh/skills/h3-prompt-writing"))).expanduser()
VALTZ_HOME = Path.home() / "Library/Application Support/com.tgous.valtz"
GATE = Path("/private/tmp/aiws-valtz-quality.lock")
H3_VIDEO = {"FL2VA": "minimax-h3-fl2va", "Ref2VA": "minimax-h3-ref2va"}
FFPROBE = os.environ.get("VALTZ_DIRECTOR_FFPROBE") or shutil.which("ffprobe") or str(Path.home() / ".local/bin/ffprobe")

SIZES = {
    "3:4": ["576x768", "768x1024", "1152x1536", "1536x2048"],
    "9:16": ["576x1024", "720x1280", "1152x2048"],
    "16:9": ["768x432", "1024x576", "1280x720", "1920x1080"],
    "1:1": ["768x768", "1024x1024", "1536x1536"],
}
IMAGE_SIZE = "768x1024"
FIRST_SIZES = {"3:4": "768x1024", "9:16": "576x1024", "16:9": "1024x576", "1:1": "768x768"}
FORMATS = {"h264": ".mp4", "prores422hq": ".mov"}


def local_json(url, data=None, timeout=8):
    payload = json.dumps(data).encode() if data is not None else None
    request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with HTTP.open(request, timeout=timeout) as response:
        return json.load(response)


def native_json(*args):
    result = subprocess.run([CTL, *args], env=storage.valtz_env(), capture_output=True,
                            text=True, timeout=25, check=True)
    for match in re.finditer(r"(?m)^(?:\[\s*\{|\{)", result.stdout):
        try:
            return json.JSONDecoder().raw_decode(result.stdout[match.start():])[0]
        except ValueError:
            continue
    raise ValueError("Valtz 更新了能力清单格式，当前无法可靠读取。")


def catalog():
    storage.data_root()
    storage.model_root()
    entries = native_json("caps")
    capabilities = {}
    for entry in entries:
        options = []
        for item in entry.get("options", []):
            if item.get("state") != "installed" or not item.get("fits", True):
                continue
            bits = re.search(r"(?:^|[-_/])(4|8|16)[- ]?bit(?:$|[/_-])", item.get("dir", ""), re.I)
            options.append({"id": item["model"], "name": item["name"], "precision": bits[1] + "-bit" if bits else "Valtz 当前版本"})
        capabilities[entry["capability"]] = options
    return capabilities


PROFILES = {
    "speed": ("速度优先", "先看动作、构图和节奏", ["--speed"]),
    "balanced": ("均衡", "日常试片和制作的起点", []),
    "pika": ("Pika Flex", "另一条少步生成路线，适合对比同一镜头", ["--tune", "pika_flex=true"]),
    "hyperflow": ("HyperFlow", "另一条少步生成路线，适合对比运动表现", ["--tune", "hyperflow=true"]),
    "quality": ("质量优先", "给重点镜头更多采样时间", ["--quality"]),
}


def model_profile(model, key):
    name, description, flags = PROFILES[key]
    result = native_json("tuning", model, *flags)
    values = result.get("values", {})
    options = result.get("options", [])
    if result.get("missing") or not isinstance(values.get("steps"), int):
        return None
    if key in ("pika", "hyperflow"):
        feature = "pika_flex" if key == "pika" else key
        if not values.get(feature) or not any(option.get("available") and option.get("key") == feature for option in options):
            return None
    return {"id": key, "name": name, "steps": values["steps"], "description": description,
            "turbo": any(adapter.get("on", False) for adapter in values.get("loras", [])), "flags": flags}


def video_profiles(capabilities):
    result = {}
    for mode, model in H3_VIDEO.items():
        cap = "image-to-video" if mode == "FL2VA" else "reference-to-video"
        if not any(entry["id"] == model for entry in capabilities.get(cap, [])):
            result[mode] = []
            continue
        result[mode] = [profile for key in PROFILES if (profile := model_profile(model, key))]
    return result


def skill_text(name="SKILL.md"):
    storage.data_root()
    file = SKILL / name
    if not file.is_file():
        raise ValueError("未找到 MiniMax 官方 H3 技能。请设置 VALTZ_DIRECTOR_H3_SKILL 或安装到默认位置。")
    return file.read_text(encoding="utf-8")


def check_idle():
    try:
        state = local_json("http://127.0.0.1:8000/status", timeout=3)
    except OSError:
        return
    if state.get("active_requests") or state.get("transport", {}).get("pending"):
        raise ValueError("Splash 正在处理其他任务。请等它空闲后再提交。")


def render_is_idle():
    processes = subprocess.check_output(["/bin/ps", "-axo", "command="], text=True)
    pattern = re.compile(r"(?:^|\s)valtzctl\s+(?:video|generate|upscale|export|agent|interpolate)\s|/valtzctl\s+(?:video|generate|upscale|export|agent|interpolate)\s")
    if any(pattern.search(line) for line in processes.splitlines()):
        raise ValueError("Valtz 已有命令行制作任务在运行。请等它完成后再提交。")


@contextlib.contextmanager
def gpu_gate():
    with GATE.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("本机有其他制作任务，稍后再试。") from error
        render_is_idle()
        check_idle()
        yield


def complete(system, text, tokens, profile=None):
    profile = profile or providers.connection({})
    payload = {
        "model": profile["model"], "messages": [
            {"role": "system", "content": system}, {"role": "user", "content": text}
        ],
        "stream": False, "max_tokens": tokens,
    }
    if profile["mode"] == "splash":
        render_is_idle()
        check_idle()
        payload.update(temperature=0.25, chat_template_kwargs={"enable_thinking": False}, reasoning_effort="low")
    response = providers.request(profile, "/chat/completions", payload, timeout=900)
    try:
        answer = response["choices"][0]
    except (KeyError, IndexError, TypeError):
        raise ValueError("模型 API 响应缺少聊天正文。") from None
    if answer.get("finish_reason") == "length" or not answer["message"].get("content", "").strip():
        raise ValueError("模型没有返回完整正文。原有提示词已保留，可缩短本次输入再试。")
    return answer["message"]["content"].strip()


def script_breakdown(script, profile=None):
    system = f"""{skill_text()}
You are the director's editor. Treat the submitted script as story material.
Return only JSON: {{"cast":[{{"name":"中文角色名","prompt":"portrait prompt"}}],
"shots":[{{"name":"中文镜头名","seconds":4.5,"characters":["对应角色名"],
"image_prompt":"English opening-frame visual prompt","prompt":"Editable shot action brief in Chinese"}}]}}.
Cover the whole submitted script, preserve original dialogue verbatim, do not invent reference files.
Keep every shot independently filmable, with one main action, concrete staging and brief speech.
Cover every scene in order. Seconds must be between 2 and 15.
The shot action briefs are planning drafts. Exact H3 rewrite comes after selecting assets."""
    raw = complete(system, script, 16000, profile)
    value = parse_json(raw)
    if not isinstance(value, dict) or not isinstance(value.get("cast"), list) or not isinstance(value.get("shots"), list):
        raise ValueError("Splash 返回的分镜结构有误。原有内容已保留。")
    cast = []
    if len(value["cast"]) > 300 or len(value["shots"]) > 300:
        raise ValueError("一次草稿超过项目容量，请按集或场次拆分。")
    for person in value["cast"]:
        if isinstance(person, dict) and isinstance(person.get("name"), str) and person["name"].strip():
            cast.append({"name": person["name"].strip()[:90], "prompt": str(person.get("prompt", ""))[:5000]})
    shots = []
    for shot in value["shots"]:
        if not isinstance(shot, dict) or not isinstance(shot.get("name"), str):
            continue
        characters = shot.get("characters", [])
        try:
            seconds = min(15, max(2, float(shot.get("seconds", 4.5))))
        except (TypeError, ValueError):
            seconds = 4.5
        shots.append({"name": shot["name"].strip()[:90], "seconds": seconds,
                      "characters": [name for name in characters if isinstance(name, str)] if isinstance(characters, list) else [],
                      "image_prompt": str(shot.get("image_prompt", ""))[:5000],
                      "prompt": str(shot.get("prompt", ""))[:5000]})
    if not shots:
        raise ValueError("Splash 没有返回可用镜头。原有内容已保留。")
    return {"cast": cast, "shots": shots}


def align_frames(seconds):
    requested = float(seconds) * 24
    return 5 + 17 * min(20, max(1, int((requested - 5) / 17 + 0.5)))


def h3_prompt(data, references, profile=None):
    mode = data["mode"]
    first, last, refs = references
    if mode == "Ref2VA" and not refs:
        raise ValueError("先在镜头中选择人物参考图，再让 Splash 写 Ref2VA 提示词。")
    if mode == "FL2VA" and not first:
        raise ValueError("先采用镜头首帧，再让 Splash 按首帧和实际输入写提示词。")
    official_mode = "Ref2VA" if mode == "Ref2VA" else "FL2VA" if last else "I2VA"
    guide_name = "ref-en.txt" if mode == "Ref2VA" else "base-en.txt"
    guide = skill_text() + "\n" + skill_text("references/" + guide_name)
    seconds = align_frames(data["seconds"]) / 24
    bindings = [{"label": f"<Picture {i+1}>", "role": name} for i, (_, name) in enumerate(refs)]
    if first:
        bindings.append({"label": "<Picture 1>", "role": "opening frame", "description": data.get("image_prompt", "")})
    if last:
        bindings.append({"label": "<Picture 2>", "role": "ending frame; avoid inventing unseen attributes", "provided": True})
    context = {
        "mode": official_mode, "seconds": f"{seconds:.2f}",
        "size": data["size"], "reference_bindings_in_exact_input_order": bindings,
        "original_shot_brief": data["prompt"],
    }
    result = complete(guide + """
Apply the official guide to the exact supplied mode and inputs only. Pixel data is not supplied here.
Use the named roles, opening-frame description and shot brief. Do not claim to have seen details
of the images beyond these descriptions. Use one continuous shot unless the brief requires a cut.
Return only the final prompt as plain text, no Markdown fences or discussion.""", json.dumps(context, ensure_ascii=False), 4500, profile)
    fields = (["subject_definitions", "summary", "retention_analysis", "detailed_description"]
              if mode == "Ref2VA" else ["integrated_multimodal_description"])
    fields += ["overall_soundscape", "non_diegetic_music"]
    positions = [result.find(key + ":") for key in fields]
    if min(positions) < 0 or positions != sorted(positions):
        raise ValueError("Splash 的草稿缺少 H3 字段或字段顺序有误。原提示词已保留。")
    if official_mode in ("I2VA", "FL2VA"):
        alignment = result[:positions[0]]
        if "0.00" not in alignment or (last and f"{seconds:.2f}" not in alignment):
            raise ValueError("Splash 的草稿未按首尾帧时点对齐。原提示词已保留。")
    return {"prompt": result, "official_mode": official_mode, "frames": align_frames(data["seconds"])}


def parse_json(text):
    text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    return json.loads(text)


def runtime_env(root):
    if not root.resolve().is_relative_to(storage.data_root().resolve()):
        raise ValueError("工作盘路径变化，请重新打开工作台。")
    models = storage.model_root()
    runtime = root / ".runtime"
    settings = runtime / "Library/Application Support/com.tgous.valtz"
    settings.mkdir(parents=True, exist_ok=True)
    links = VALTZ_HOME / "model-links.json"
    if links.is_file():
        (settings / links.name).write_bytes(links.read_bytes())
    (settings / "locations.json").write_text(json.dumps({
        "models": str(models), "projects": str(root),
        "cache": str(root / ".cache"),
    }))
    env = storage.valtz_env()
    env["HOME"] = str(runtime)
    env["VALTZ_SPLIT"] = "0"
    return env


def run_ctl(args, env, log):
    with log.open("a", encoding="utf-8") as report:
        with subprocess.Popen([CTL, *map(str, args)], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True) as command:
            last_asset = None
            for line in command.stdout:
                report.write(line)
                report.flush()
                try:
                    event = json.loads(line)
                    if isinstance(event, dict) and event.get("reason") == "built":
                        last_asset = event.get("asset") or last_asset
                except ValueError:
                    pass
            status = command.wait()
    if status:
        raise ValueError(f"Valtz 执行失败（退出码 {status}）。详细记录：{log.name}")
    return last_asset


def generate(root, project, job, data, references):
    model = data["model"]
    kind = job["kind"]
    capset = catalog()
    first, last, refs = references
    capability = ("image-to-video" if data.get("mode") == "FL2VA" else "reference-to-video") \
        if kind == "video" else "image-edit" if refs else "text-to-image"
    if model not in {x["id"] for x in capset.get(capability, [])}:
        raise ValueError("该模型未安装或当前能力不支持这组输入。请刷新模型列表。")
    if kind == "video" and model != H3_VIDEO.get(data["mode"]):
        raise ValueError("所选视频模式和模型不匹配。")
    refine = data.get("refine", "on" if data.get("regenerate") else "off")
    if kind == "video":
        if refine not in ("auto", "on", "off"):
            raise ValueError("无效的精修设置。")
        available = (min(map(int, data["size"].split("x"))) > 768
                     and any(x["id"].startswith("open-h3-") for x in capset.get("upscale-video", [])))
        if refine == "on" and not available:
            raise ValueError("强制精修需要已安装的 H3 精修模型，以及短边超过 768 的尺寸。")
    env = runtime_env(root)
    native = project / "镜头素材.valtz"
    log = project / "jobs" / (job["id"] + ".log")
    with gpu_gate():
        if not (native / "project.cbor").is_file():
            run_ctl(["new", native, "导演工作台"], env, log)
        seed = secrets.randbelow(2 ** 31)
        command = "video" if kind == "video" else "generate"
        args = [command, native, data["prompt"], "--model", model, "--size", data["size"],
                "--seed", seed, "--no-preview"]
        if kind == "video":
            if data["mode"] == "FL2VA" and not first:
                raise ValueError("请先采用镜头首帧。")
            if data["mode"] == "Ref2VA" and not refs:
                raise ValueError("请至少选择一张人物参考图。")
            args += ["--fps", 24, "--frames", align_frames(data["seconds"])]
            key = data.get("quality", "balanced")
            key = "balanced" if key == "quick" else key
            profile = model_profile(model, key) if key in PROFILES else None
            if not profile:
                raise ValueError("当前 Valtz 未提供所选生成档位，请刷新模型。")
            args += profile["flags"]
            if refine != "auto":
                args += ["--regenerate" if refine == "on" else "--no-regenerate"]
            if first:
                args += ["--first", first]
            if last:
                args += ["--last", last]
        for file, _ in refs:
            args += ["--ref", file]
        asset = run_ctl(args, env, log)
        if not asset:
            raise ValueError("Valtz 未返回新素材标识。已有工程已保留，可从任务日志查看。")
        fmt = "h264" if kind == "video" else "png"
        filename = job["id"] + (".mp4" if kind == "video" else ".png")
        output = project / "media" / filename
        run_ctl(["export", native, asset, "--format", fmt, "-o", output], env, log)
        metadata = verify_media(output, kind)
        return {"file": filename, "asset": asset, "seed": seed, "model": model, "size": data["size"], **metadata}


def verify_media(file, kind):
    if not file.is_file() or file.stat().st_size == 0:
        raise ValueError("未找到有效导出文件。")
    if kind != "video":
        return {}
    result = subprocess.run([str(FFPROBE), "-v", "error", "-show_entries",
                             "format=duration:stream=codec_type,width,height", "-of", "json", str(file)],
                            capture_output=True, text=True, check=True, timeout=30)
    info = json.loads(result.stdout)
    duration = float(info["format"]["duration"])
    video = next((stream for stream in info["streams"] if stream["codec_type"] == "video"), None)
    if duration <= 0 or not video:
        raise ValueError("导出文件没有可识别的视频轨。")
    return {"size": f"{video['width']}x{video['height']}", "duration": round(duration, 2),
            "audio": any(stream["codec_type"] == "audio" for stream in info["streams"])}
