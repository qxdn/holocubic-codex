# HoloCubic Codex 额度

320×240 Lua app，只显示 `5h`、`Weekly` 剩余额度百分比和各 20 格进度条，每格代表 5%。部分格通过亮度表示；低于等于 30% 为黄色，低于等于 10% 为红色。连接失败、额度不可用时显示 `--` 和空格。

无需设备即可预览：用浏览器打开根目录 `preview.html`，拖动滑块查看不同额度效果。

1. 在项目根目录运行 `python scripts/service.py` 启动服务，按 `Ctrl+C` 停止。
2. 修改 `app/config.json` 的 `service_url` 为电脑的局域网地址（端口 8788）。电脑和设备须在同一网络，设备须能访问该端口。
3. 将 `app/` 内文件复制到设备 `/sd/apps/codex_quota/`，在 Launcher 重扫应用并打开 Codex Quota。

默认每 15 秒读取 `/state`，使用 `quota_5h_pct`、`quota_7d_pct` 两个剩余额度字段。上键立即刷新，HOME 返回。电脑端额度刷新频率由现有服务配置决定。

电脑端服务文件统一放在 `scripts/`：`service.py` 为启动入口，`app.py` 提供额度读取和 HTTP 服务，`config.json` 设置电脑端刷新间隔（默认 30 秒）。需要 Python 3.10 或更新版本，并已登录 Codex；登录凭据仍从当前用户的 Codex 目录读取。

开发接口参考：[普通 app 开发参考](https://github.com/clocteck/holocubic-apps#普通-app-开发参考)、[Lua API](https://github.com/clocteck/holocubic-apps/blob/main/README_LUA.md)、[LVGL Lua API](https://github.com/clocteck/holocubic-apps/blob/main/README_LVGL.md)。
