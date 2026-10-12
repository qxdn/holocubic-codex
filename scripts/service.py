from __future__ import annotations

import time

from app import StateServer, TaskMonitor, TaskSnapshot, UsageSnapshot, fetch_usage, load_config


def device_state(usage: UsageSnapshot, task, event: str) -> dict:
    by_label = {item.label: item for item in usage.windows}
    five_hour = by_label.get("5 小时")
    seven_day = by_label.get("7 天")
    return {
        "updated_at": int(time.time()),
        "quota_5h_pct": five_hour.remaining if five_hour else None,
        "quota_7d_pct": seven_day.remaining if seven_day else None,
        "quota_5h_reset": five_hour.reset_at if five_hour else None,
        "quota_7d_reset": seven_day.reset_at if seven_day else None,
        "plan": usage.plan,
        "task_state": task.state,
        "event": event,
        "completion_key": task.completion_key,
        "error": usage.error,
    }


def main() -> None:
    config = load_config()
    monitor = TaskMonitor()
    monitor.initialize()
    server = StateServer()
    try:
        server.start()
    except OSError:
        return

    usage = UsageSnapshot((), None, "等待余量刷新")
    next_usage_refresh = 0.0
    completion_until = 0.0
    try:
        while True:
            now = time.time()
            if now >= next_usage_refresh:
                try:
                    usage = fetch_usage()
                except Exception as exc:
                    usage = UsageSnapshot((), None, f"余量刷新失败：{exc.__class__.__name__}")
                next_usage_refresh = now + config["poll_seconds"]

            try:
                task = monitor.scan()
            except Exception:
                task = TaskSnapshot("空闲", "会话读取失败", None)
            if monitor.completion_is_new(task):
                completion_until = time.time() + 60

            event = "complete" if time.time() < completion_until else "running" if task.state == "处理中" else "idle"
            server.update(device_state(usage, task, event))
            time.sleep(2)
    finally:
        server.stop()


if __name__ == "__main__":
    main()
