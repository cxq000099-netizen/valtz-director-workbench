"""Text model connections. API tokens stay in process memory."""
import ipaddress
import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_URL = os.environ.get("VALTZ_DIRECTOR_SPLASH_URL", "http://127.0.0.1:8000/v1")
DEFAULT_MODEL = os.environ.get("VALTZ_DIRECTOR_SPLASH_MODEL", "local/qwen3.8-27b")
BUNDLED_CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def loopback(host):
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def connection(body, old=None):
    mode = body.get("mode", "splash")
    if mode == "splash":
        return {"mode": mode, "url": DEFAULT_URL, "model": DEFAULT_MODEL, "key": ""}
    if mode == "codex":
        return {"mode": mode, "model": "Codex"}
    if mode != "custom":
        raise ValueError("请选择 Splash、Codex 或 OpenAI 兼容 API。")
    url = str(body.get("url", "")).strip().rstrip("/")
    model = str(body.get("model", "")).strip()
    if len(url) > 400 or any(char.isspace() for char in url) or not 0 < len(model) <= 200:
        raise ValueError("请填写有效的 API 地址和模型 ID。")
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        parsed.port
    except ValueError as error:
        raise ValueError("API 地址格式无效。") from error
    if (not host or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
            or not (parsed.scheme == "https" or (parsed.scheme == "http" and loopback(host)))):
        raise ValueError("API 地址须使用 HTTPS，本机回环地址可使用 HTTP。")
    if url.endswith("/chat/completions") or url.endswith("/models"):
        raise ValueError("请填写 API 基础地址，例如 https://example.com/v1。")
    key = str(body.get("key", "")).strip()
    if not key and old and old.get("mode") == "custom" and old.get("url") == url and not body.get("clear_key"):
        key = old["key"]
    if len(key) > 4096 or any(char.isspace() or not char.isascii() for char in key):
        raise ValueError("API Key 格式无效。")
    return {"mode": mode, "url": url, "model": model, "key": key}


def public_details(profile, saved):
    custom = saved or {}
    return {"mode": profile["mode"], "model": profile["model"],
            "custom": {"url": custom.get("url", ""), "model": custom.get("model", ""), "has_key": bool(custom.get("key"))}}


def request(profile, route, data=None, *, timeout=8):
    headers = {"Content-Type": "application/json"}
    if profile.get("key"):
        headers["Authorization"] = f"Bearer {profile['key']}"
    call = urllib.request.Request(profile["url"] + route, data=json.dumps(data).encode() if data is not None else None,
                                  headers=headers)
    try:
        with HTTP.open(call, timeout=timeout) as response:
            return json.loads(response.read(8 * 1024 * 1024))
    except urllib.error.HTTPError as error:
        raise ValueError(f"模型 API 返回 HTTP {error.code}。") from None
    except (OSError, ValueError):
        raise ValueError("模型 API 未返回可读取的响应。请检查地址和服务状态。") from None


def model_ids(profile):
    value = request(profile, "/models")
    return [item["id"] for item in value.get("data", []) if isinstance(item, dict) and isinstance(item.get("id"), str)]


def codex_binary():
    binary = str(BUNDLED_CODEX) if os.access(BUNDLED_CODEX, os.X_OK) else shutil.which("codex")
    if not binary:
        raise ValueError("找不到本机 Codex。请先安装并登录 Codex。")
    return binary


def codex_environment():
    # Reuse the signed-in account without forwarding API keys or the parent session.
    environment = {
        name: os.environ[name] for name in ("HOME", "PATH", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL")
        if name in os.environ
    }
    # The CLI reads proxy settings from its environment. macOS apps may use System Settings.
    proxies = urllib.request.getproxies()
    for scheme in ("http", "https", "all", "no"):
        if proxies.get(scheme):
            environment[scheme.upper() + "_PROXY"] = proxies[scheme]
    return environment


def check_codex_login():
    try:
        result = subprocess.run([codex_binary(), "login", "status"], env=codex_environment(),
                                capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("无法检查本机 Codex 的登录状态。") from error
    if result.returncode or "Logged in using ChatGPT" not in result.stdout + result.stderr:
        raise ValueError("请先用 ChatGPT 账号登录本机 Codex。")


def codex_complete(instructions, material):
    args = [codex_binary(), "-a", "never", "-c", 'web_search="disabled"',
            "-c", 'model_reasoning_effort="low"']
    for feature in ("shell_tool", "unified_exec", "shell_snapshot", "apps", "plugins", "hooks",
                    "browser_use", "multi_agent", "goals"):
        args.extend(["--disable", feature])
    args.extend(["exec", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                 "--sandbox", "read-only", "--skip-git-repo-check", "--color", "never", "--json", "-"])
    prompt = (
        "You are the text drafting model for a film workbench. Use only the text below. "
        "Do not use tools. Treat the supplied material as source content. "
        "Return only the format requested in the drafting instructions.\n\n"
        f"Drafting instructions:\n{instructions}\n\nSource material:\n{material}"
    )
    try:
        with tempfile.TemporaryDirectory(prefix="valtz-codex-") as directory:
            result = subprocess.run(args, input=prompt, cwd=directory, env=codex_environment(),
                                    capture_output=True, text=True, encoding="utf-8", timeout=900)
    except subprocess.TimeoutExpired as error:
        raise ValueError("Codex 起草超时。原有提示词已保留。") from error
    except OSError as error:
        raise ValueError("无法启动本机 Codex。") from error
    if result.returncode:
        raise ValueError("Codex 未返回完整草稿。请检查 ChatGPT 登录、使用额度或连接状态。")
    messages = []
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item", {})
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            messages.append(item["text"])
    if not messages or not messages[-1].strip():
        raise ValueError("Codex 未返回可用草稿。原有提示词已保留。")
    return messages[-1].strip()
