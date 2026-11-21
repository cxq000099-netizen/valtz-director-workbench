"""Installation paths. A configured external drive is resolved by UUID on each use."""
import os
import plistlib
import subprocess
from pathlib import Path

APP = Path(os.environ.get("VALTZ_APP", "/Applications/Valtz.app")).expanduser()
CTL = str(APP / "Contents/Helpers/valtzctl")
PLUGIN = str(APP / "Contents/PlugIns/valtz-vpipe.so")


def mounted(uuid):
    raw = subprocess.run(["/usr/sbin/diskutil", "info", "-plist", uuid],
                         capture_output=True, check=False, timeout=15)
    if raw.returncode == 0:
        info = plistlib.loads(raw.stdout)
        if info.get("VolumeUUID", "").casefold() == uuid.casefold() and info.get("MountPoint"):
            return Path(info["MountPoint"])
    raise ValueError("所需的外接盘未挂载，请连接后再继续。")


def configured_path(kind, default, relative_default):
    prefix = f"VALTZ_DIRECTOR_{kind}"
    uuid = os.environ.get(prefix + "_UUID")
    if uuid:
        relative = Path(os.environ.get(prefix + "_SUBDIR", relative_default))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("外接盘的子路径必须是盘内相对路径。")
        return mounted(uuid) / relative
    return Path(os.environ.get(prefix + "_PATH", default)).expanduser().resolve()


def data_root():
    return configured_path("DATA", "~/Library/Application Support/Valtz Director Workbench",
                           "AI/projects/Valtz Director Workbench")


def model_root():
    if os.environ.get("VALTZ_DIRECTOR_MODELS_UUID") or os.environ.get("VALTZ_DIRECTOR_MODELS_PATH"):
        return configured_path("MODELS", "", "AI/models/valtz")
    result = subprocess.run([CTL, "paths"], capture_output=True, text=True, check=True, timeout=15)
    for line in result.stdout.splitlines():
        label, _, value = line.partition(" ")
        if label == "models" and value.strip().startswith("/"):
            return Path(value.strip())
    raise ValueError("Valtz 未返回模型位置。请设置 VALTZ_DIRECTOR_MODELS_PATH。")


def valtz_env():
    return dict(os.environ, VALTZ_VPIPE_PLUGIN=PLUGIN)


def app_version():
    with (APP / "Contents/Info.plist").open("rb") as file:
        info = plistlib.load(file)
    return {"version": str(info["CFBundleShortVersionString"]), "build": str(info["CFBundleVersion"])}
