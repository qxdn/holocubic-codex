from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import tkinter as tk
from tkinter import messagebox
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "config.json"
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
TAIL_BYTES = 2 * 1024 * 1024

DEFAULT_CONFIG = {
    "poll_seconds": 30,
    "notify_on_completion": True,
    "notify_on_low_quota": True,
    "low_quota_threshold": 10,
}


@dataclass(frozen=True)
class UsageWindow:
    label: str
    remaining: float | None
    reset_at: str | None


@dataclass(frozen=True)
class UsageSnapshot:
    windows: tuple[UsageWindow, ...]
    plan: str | None
    error: str | None = None


@dataclass(frozen=True)
class TaskSnapshot:
    state: str
    detail: str
    session: str | None
    completion_key: str | None = None


def load_config() -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    try:
        user_config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(user_config, dict):
            config.update({key: value for key, value in user_config.items() if key in config})
    except FileNotFoundError:
        CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        pass

    config["poll_seconds"] = max(10, min(int(config["poll_seconds"]), 3600))
    config["low_quota_threshold"] = max(1, min(int(config["low_quota_threshold"]), 100))
    config["notify_on_completion"] = bool(config["notify_on_completion"])
    config["notify_on_low_quota"] = bool(config["notify_on_low_quota"])
    return config


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def read_access_token() -> str:
    auth_path = codex_home() / "auth.json"
    try:
        raw = auth_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        token = data.get("tokens", {}).get("access_token")
    except FileNotFoundError as exc:
        raise RuntimeError("未找到 Codex 登录信息，请先登录 Codex。") from exc
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        raise RuntimeError("无法读取 Codex 登录信息。") from exc
    if not isinstance(token, str) or not token:
        raise RuntimeError("Codex 登录令牌缺失，请重新登录。")
    return token


def parse_reset(value: Any) -> str | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value)).strftime("%m-%d %H:%M")
    if isinstance(value, str) and value:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(normalized)
            return parsed.astimezone().strftime("%m-%d %H:%M") if parsed.tzinfo else parsed.strftime("%m-%d %H:%M")
        except ValueError:
            return None
    return None


def fetch_usage() -> UsageSnapshot:
    try:
        token = read_access_token()
        payload = _fetch_usage_payload(token)
    except RuntimeError as exc:
        return UsageSnapshot((), None, str(exc))
    except HTTPError as exc:
        message = "登录已失效，请重新登录 Codex。" if exc.code in (401, 403) else f"用量接口返回 HTTP {exc.code}。"
        return UsageSnapshot((), None, message)
    except (URLError, OSError, json.JSONDecodeError) as exc:
        return UsageSnapshot((), None, f"无法获取用量：{exc.__class__.__name__}")

    rate_limit = payload.get("rate_limit") if isinstance(payload, dict) else None
    if not isinstance(rate_limit, dict):
        return UsageSnapshot((), None, "用量接口返回了无法识别的数据。")

    windows: list[UsageWindow] = []
    # Window order varies by plan; primary_window can itself be the weekly limit.
    labels_by_seconds = {5 * 60 * 60: "5 小时", 7 * 24 * 60 * 60: "7 天"}
    for key in ("primary_window", "secondary_window"):
        raw = rate_limit.get(key)
        if not isinstance(raw, dict):
            continue
        label = labels_by_seconds.get(raw.get("limit_window_seconds"))
        if label is None:
            continue
        try:
            remaining = max(0.0, min(100.0, 100.0 - float(raw.get("used_percent"))))
        except (TypeError, ValueError):
            remaining = None
        windows.append(UsageWindow(label, remaining, parse_reset(raw.get("reset_at"))))

    if not windows:
        return UsageSnapshot((), None, "用量接口未返回可用的额度窗口。")
    plan = payload.get("plan_type") if isinstance(payload.get("plan_type"), str) else None
    return UsageSnapshot(tuple(windows), plan)


def _fetch_usage_payload(token: str) -> dict[str, Any]:
    request = Request(
        USAGE_URL,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "Codex Windows Monitor",
        },
    )
    try:
        with urlopen(request, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise json.JSONDecodeError("response is not an object", "", 0)
            return payload
    except URLError:
        return _fetch_usage_with_node(token)


def _find_node() -> str | None:
    configured = os.environ.get("CODEX_NODE")
    candidates = [configured, shutil.which("node")]
    candidates.extend(str(path) for path in (Path.home() / ".cache" / "codex-runtimes").glob("*/dependencies/node/bin/node.exe"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def _fetch_usage_with_node(token: str) -> dict[str, Any]:
    node = _find_node()
    if not node:
        raise URLError("Python network request failed and Node.js is unavailable")
    script = """
const https = require('https');
const req = https.request(process.env.CODEX_MONITOR_USAGE_URL, {
  headers: { Accept: 'application/json', Authorization: `Bearer ${process.env.CODEX_MONITOR_TOKEN}`, 'User-Agent': 'Codex Windows Monitor' }
}, res => {
  let body = '';
  res.setEncoding('utf8');
  res.on('data', chunk => body += chunk);
  res.on('end', () => process.stdout.write(JSON.stringify({ status: res.statusCode, body })));
});
req.on('error', err => { process.stderr.write(err.message); process.exit(1); });
req.setTimeout(12000, () => req.destroy(new Error('timeout')));
req.end();
"""
    env = dict(os.environ)
    env["CODEX_MONITOR_TOKEN"] = token
    env["CODEX_MONITOR_USAGE_URL"] = USAGE_URL
    try:
        result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15, env=env, check=False)
        if result.returncode != 0:
            raise URLError("Node usage request failed")
        wrapper = json.loads(result.stdout)
        status = int(wrapper.get("status", 0))
        if status in (401, 403):
            raise HTTPError(USAGE_URL, status, "auth rejected", {}, None)
        if status != 200:
            raise HTTPError(USAGE_URL, status, "usage request failed", {}, None)
        payload = json.loads(wrapper.get("body", ""))
        if not isinstance(payload, dict):
            raise json.JSONDecodeError("response is not an object", "", 0)
        return payload
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise URLError("Node usage request failed") from exc


def parse_timestamp(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
            return datetime.fromisoformat(normalized).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def compact(value: Any, limit: int = 90) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def read_jsonl_tail(path: Path) -> list[dict[str, Any]]:
    size = path.stat().st_size
    with path.open("rb") as stream:
        if size > TAIL_BYTES:
            stream.seek(size - TAIL_BYTES)
            stream.readline()
        raw = stream.read().decode("utf-8", errors="replace")
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                events.append(item)
        except json.JSONDecodeError:
            continue
    return events


class TaskMonitor:
    def __init__(self) -> None:
        self.sessions_dir = codex_home() / "sessions"
        self.last_completion_key: str | None = None

    def initialize(self) -> None:
        snapshot = self.scan()
        self.last_completion_key = snapshot.completion_key

    def scan(self) -> TaskSnapshot:
        if not self.sessions_dir.exists():
            return TaskSnapshot("未检测到会话", "等待 Codex 创建会话", None)
        try:
            session = max(self.sessions_dir.rglob("*.jsonl"), key=lambda item: item.stat().st_mtime)
            events = read_jsonl_tail(session)
        except (OSError, ValueError):
            return TaskSnapshot("会话读取失败", "无法读取最新会话文件", None)

        active = False
        detail = "等待下一项任务"
        completion_key = None
        for event in events:
            payload = event.get("payload")
            if not isinstance(payload, dict) or event.get("type") != "event_msg":
                continue
            event_type = payload.get("type")
            timestamp = parse_timestamp(event.get("timestamp")) or session.stat().st_mtime
            if event_type == "task_started":
                active = True
                detail = "Codex 正在处理任务"
            elif event_type == "task_complete":
                active = False
                detail = "最近任务已完成"
                completion_key = f"{session}:{timestamp:.6f}"
            elif event_type == "turn_aborted":
                active = False
                detail = "最近任务被中断"
            elif event_type == "error":
                active = False
                detail = compact(payload.get("message") or "Codex 报错")
            elif event_type == "agent_message" and active:
                detail = compact(payload.get("message") or "Codex 正在处理任务")

        state = "处理中" if active else "空闲"
        return TaskSnapshot(state, detail, session.name, completion_key)

    def completion_is_new(self, snapshot: TaskSnapshot) -> bool:
        if not snapshot.completion_key or snapshot.completion_key == self.last_completion_key:
            return False
        self.last_completion_key = snapshot.completion_key
        return True


class ToastNotifier:
    def notify(self, title: str, body: str) -> None:
        script = APP_DIR / "notify.ps1"
        if not script.exists() or os.name != "nt":
            return
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Title", title, "-Body", body],
                creationflags=flags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass


class StateServer:
    def __init__(self, port: int = 8788) -> None:
        self.port = port
        self.lock = threading.Lock()
        self.state: dict[str, Any] = {
            "updated_at": 0,
            "quota_5h_pct": None,
            "quota_7d_pct": None,
            "task_state": "等待 Windows 端刷新",
            "task_detail": "",
            "event": "starting",
            "completion_key": None,
        }
        self.server: ThreadingHTTPServer | None = None

    def start(self) -> None:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def _send_json(self, status: int, payload: dict[str, Any]) -> None:
                raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                if path == "/health":
                    self._send_json(200, {"ok": True, "service": "codex-monitor"})
                elif path == "/state":
                    self._send_json(200, owner.snapshot())
                else:
                    self._send_json(404, {"ok": False, "error": "not found"})

        self.server = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def update(self, state: dict[str, Any]) -> None:
        with self.lock:
            self.state = dict(state)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.state)

    def url(self) -> str:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("8.8.8.8", 80))
                host = probe.getsockname()[0]
        except OSError:
            host = socket.gethostbyname(socket.gethostname())
        return f"http://{host}:{self.port}"


class DashboardApp:
    BG = "#111827"
    PANEL = "#1f2937"
    BORDER = "#334155"
    TEXT = "#f8fafc"
    SUB = "#94a3b8"
    GREEN = "#34d399"
    AMBER = "#fbbf24"
    RED = "#fb7185"
    BLUE = "#60a5fa"

    def __init__(self) -> None:
        self.config = load_config()
        self.root = tk.Tk()
        self.root.title("Codex Monitor")
        self.root.geometry("720x480")
        self.root.minsize(640, 440)
        self.root.configure(bg=self.BG)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

        self.task_monitor = TaskMonitor()
        self.task_monitor.initialize()
        self.notifier = ToastNotifier()
        self.state_server = StateServer()
        self.server_error: str | None = None
        try:
            self.state_server.start()
        except OSError:
            self.server_error = "端口 8788 已被占用，HoloCubic 无法连接"
        self.results: queue.Queue[tuple[UsageSnapshot, TaskSnapshot]] = queue.Queue()
        self.refreshing = False
        self.low_notified = False
        self.next_refresh = 0.0

        self.plan_var = tk.StringVar(value="正在读取用量")
        self.updated_var = tk.StringVar(value="未刷新")
        self.task_state_var = tk.StringVar(value="正在读取会话")
        self.task_detail_var = tk.StringVar(value="")
        self.session_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="准备就绪")
        self.bridge_var = tk.StringVar(value=self.server_error or f"HoloCubic 服务：{self.state_server.url()}/state")
        self.notify_var = tk.BooleanVar(value=bool(self.config["notify_on_completion"]))
        self.cards: dict[str, dict[str, Any]] = {}
        self._build_ui()
        self.refresh_now()
        self.root.after(250, self._poll_results)
        self.root.after(1000, self._tick)

    def _label(self, parent: tk.Widget, text: str, size: int, color: str, **kwargs: Any) -> tk.Label:
        return tk.Label(parent, text=text, font=("Microsoft YaHei UI", size), fg=color, bg=parent.cget("bg"), **kwargs)

    def _build_ui(self) -> None:
        outer = tk.Frame(self.root, bg=self.BG, padx=24, pady=22)
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=self.BG)
        header.pack(fill="x")
        self._label(header, "CODEX MONITOR", 20, self.TEXT).pack(side="left")
        self._label(header, "本机用量与任务提醒", 10, self.SUB).pack(side="left", padx=(12, 0), pady=(8, 0))
        tk.Button(header, text="立即刷新", command=self.refresh_now, font=("Microsoft YaHei UI", 10), fg=self.TEXT, bg="#2563eb", activebackground="#1d4ed8", activeforeground=self.TEXT, relief="flat", padx=12, pady=6, cursor="hand2").pack(side="right")

        self._label(outer, self.plan_var.get(), 11, self.SUB, textvariable=self.plan_var).pack(anchor="w", pady=(10, 14))

        metrics = tk.Frame(outer, bg=self.BG)
        metrics.pack(fill="x")
        self._metric_card(metrics, "5 小时余量", "primary")
        self._metric_card(metrics, "7 天余量", "secondary")

        task = tk.Frame(outer, bg=self.PANEL, highlightbackground=self.BORDER, highlightthickness=1, padx=16, pady=14)
        task.pack(fill="x", pady=(18, 0))
        self._label(task, "任务状态", 12, self.TEXT).pack(anchor="w")
        self._label(task, self.task_state_var.get(), 18, self.GREEN, textvariable=self.task_state_var).pack(anchor="w", pady=(6, 0))
        self._label(task, self.task_detail_var.get(), 10, self.SUB, textvariable=self.task_detail_var, wraplength=630, justify="left").pack(anchor="w", pady=(3, 0))
        self._label(task, self.session_var.get(), 9, self.SUB, textvariable=self.session_var).pack(anchor="w", pady=(6, 0))

        footer = tk.Frame(outer, bg=self.BG)
        footer.pack(fill="x", side="bottom", pady=(14, 0))
        tk.Checkbutton(footer, text="任务完成时通知", variable=self.notify_var, font=("Microsoft YaHei UI", 10), fg=self.SUB, bg=self.BG, activebackground=self.BG, activeforeground=self.TEXT, selectcolor=self.PANEL).pack(side="left")
        self._label(footer, self.status_var.get(), 10, self.SUB, textvariable=self.status_var).pack(side="right")
        self._label(outer, self.updated_var.get(), 9, self.SUB, textvariable=self.updated_var).pack(anchor="w", side="bottom", pady=(8, 0))
        self._label(outer, self.bridge_var.get(), 9, self.SUB, textvariable=self.bridge_var).pack(anchor="w", side="bottom", pady=(4, 0))

    def _metric_card(self, parent: tk.Widget, title: str, key: str) -> None:
        card = tk.Frame(parent, bg=self.PANEL, highlightbackground=self.BORDER, highlightthickness=1, padx=16, pady=14)
        card.pack(side="left", fill="both", expand=True, padx=(0, 9) if key == "primary" else (9, 0))
        self._label(card, title, 11, self.SUB).pack(anchor="w")
        value = tk.StringVar(value="--")
        reset = tk.StringVar(value="重置时间：--")
        color = tk.StringVar(value=self.SUB)
        number = self._label(card, value.get(), 30, self.GREEN, textvariable=value)
        number.pack(anchor="w", pady=(5, 0))
        self._label(card, reset.get(), 10, self.SUB, textvariable=reset).pack(anchor="w")
        bar = tk.Canvas(card, height=7, bg=self.PANEL, highlightthickness=0)
        bar.pack(fill="x", pady=(12, 0))
        bar.bind("<Configure>", lambda _event, card_key=key: self._redraw_bar(self.cards[card_key]))
        self.cards[key] = {"value": value, "reset": reset, "color": color, "number": number, "bar": bar}

    def refresh_now(self) -> None:
        if self.refreshing:
            return
        self.refreshing = True
        self.status_var.set("正在刷新…")
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self) -> None:
        try:
            self.results.put((fetch_usage(), self.task_monitor.scan()))
        except Exception as exc:
            self.results.put((UsageSnapshot((), None, f"刷新失败：{exc.__class__.__name__}"), TaskSnapshot("读取失败", "无法读取本机会话", None)))

    def _poll_results(self) -> None:
        try:
            while True:
                usage, task = self.results.get_nowait()
                self._apply_snapshot(usage, task)
        except queue.Empty:
            pass
        self.root.after(250, self._poll_results)

    def _apply_snapshot(self, usage: UsageSnapshot, task: TaskSnapshot) -> None:
        self.refreshing = False
        self.next_refresh = time.time() + self.config["poll_seconds"]
        self.updated_var.set("上次更新：" + datetime.now().strftime("%H:%M:%S"))
        self.task_state_var.set(task.state)
        self.task_detail_var.set(task.detail)
        self.session_var.set("会话：" + (task.session or "未找到"))

        completion_is_new = self.task_monitor.completion_is_new(task)
        self._update_device_state(usage, task, completion_is_new)

        if usage.error:
            self.plan_var.set(usage.error)
            self.status_var.set("刷新失败，将自动重试")
        else:
            plan = usage.plan or "Codex"
            self.plan_var.set(f"套餐：{plan}    自动刷新间隔：{self.config['poll_seconds']} 秒")
            self.status_var.set("监测中")
            for index, window in enumerate(usage.windows):
                key = "primary" if index == 0 else "secondary"
                self._set_card(key, window)
            self._check_low_quota(usage)

        if self.notify_var.get() and completion_is_new:
            self.notifier.notify("Codex 任务已完成", task.detail)
            self.status_var.set("已发送任务完成通知")

    def _update_device_state(self, usage: UsageSnapshot, task: TaskSnapshot, completion_is_new: bool) -> None:
        by_label = {item.label: item for item in usage.windows}
        five_hour = by_label.get("5 小时")
        seven_day = by_label.get("7 天")
        event = "complete" if completion_is_new else "running" if task.state == "处理中" else "idle"
        self.state_server.update(
            {
                "updated_at": int(time.time()),
                "quota_5h_pct": five_hour.remaining if five_hour else None,
                "quota_7d_pct": seven_day.remaining if seven_day else None,
                "quota_5h_reset": five_hour.reset_at if five_hour else None,
                "quota_7d_reset": seven_day.reset_at if seven_day else None,
                "plan": usage.plan,
                "task_state": task.state,
                "task_detail": task.detail,
                "event": event,
                "completion_key": task.completion_key,
                "error": usage.error,
            }
        )

    def _set_card(self, key: str, window: UsageWindow) -> None:
        card = self.cards[key]
        remaining = window.remaining
        text = "--" if remaining is None else f"{remaining:.0f}%"
        color = self.GREEN if remaining is None or remaining > 30 else self.AMBER if remaining > 10 else self.RED
        card["value"].set(text)
        card["reset"].set("重置时间：" + (window.reset_at or "--"))
        card["number"].configure(fg=color)
        canvas: tk.Canvas = card["bar"]
        canvas.delete("all")
        width = max(canvas.winfo_width(), 1)
        canvas.create_rectangle(0, 0, width, 7, fill="#334155", outline="")
        if remaining is not None:
            canvas.create_rectangle(0, 0, width * remaining / 100, 7, fill=color, outline="")

    def _check_low_quota(self, usage: UsageSnapshot) -> None:
        values = [item.remaining for item in usage.windows if item.remaining is not None]
        if not values:
            return
        lowest = min(values)
        threshold = self.config["low_quota_threshold"]
        if lowest <= threshold and self.config["notify_on_low_quota"] and not self.low_notified:
            self.notifier.notify("Codex 余量偏低", f"当前最低剩余 {lowest:.0f}%，请留意重置时间。")
            self.low_notified = True
        elif lowest > threshold:
            self.low_notified = False

    def _tick(self) -> None:
        if not self.refreshing and time.time() >= self.next_refresh:
            self.refresh_now()
        self.root.after(1000, self._tick)

    def _redraw_bar(self, card: dict[str, Any]) -> None:
        value = card["value"].get().rstrip("%")
        try:
            remaining = float(value)
        except ValueError:
            return
        canvas: tk.Canvas = card["bar"]
        color = card["number"].cget("fg")
        canvas.delete("all")
        width = max(canvas.winfo_width(), 1)
        canvas.create_rectangle(0, 0, width, 7, fill="#334155", outline="")
        canvas.create_rectangle(0, 0, width * remaining / 100, 7, fill=color, outline="")

    def _close(self) -> None:
        self.state_server.stop()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    try:
        DashboardApp().run()
    except tk.TclError as exc:
        messagebox.showerror("Codex Monitor", f"无法启动窗口：{exc}")
