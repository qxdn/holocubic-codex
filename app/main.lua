local APP_KEY = "HOLOCUBIC_CODEX_QUOTA"

-- 应用状态统一放在 APP，方便重载时找到并清理旧实例。
local APP = {
  timer = nil,     -- 额度刷新定时器，启动时创建，退出时释放
  stopped = false, -- 是否已退出
  busy = false,    -- 是否正在请求数据
  rows = {},       -- 两行额度的 UI 对象
}
local MAIN = LV_PART_MAIN | LV_STATE_DEFAULT
local root = nil
local BG, EMPTY, TEXT = 0x101820, 0x28343F, 0xF4F8FB
local config = { service_url = "http://192.168.124.19:8788", poll_seconds = 15 }
local function load_config()
  local directory = "/sd/apps/codex_quota"
  local current = app.current()
  if type(current) == "table" then
    local id = current.id or current.app_id
    if type(id) == "string" and id ~= "" then
      directory = "/sd/apps/" .. id
    end
  end

  local raw = file.getcontents(directory .. "/config.json")
  if not raw then return end
  local ok, data = pcall(sjson.decode, raw)
  if ok and type(data) == "table" then
    if type(data.service_url) == "string" and data.service_url:match("^https?://") then
      config.service_url = data.service_url:gsub("/+$", "")
    end
    local seconds = tonumber(data.poll_seconds)
    if seconds and seconds == seconds then
      config.poll_seconds = math.max(5, math.min(300, math.floor(seconds)))
    end
  end
end

local function style(obj, color)
  lv_obj_set_style_bg_color(obj, color, MAIN)
  lv_obj_set_style_bg_opa(obj, 255, MAIN)
  lv_obj_set_style_border_width(obj, 0, MAIN)
  lv_obj_set_style_radius(obj, 0, MAIN)
  lv_obj_set_style_pad_all(obj, 0, MAIN)
  lv_obj_clear_flag(obj, LV_OBJ_FLAG_SCROLLABLE)
end

local function label(text, x, y, width, align)
  local obj = lv_label_create(root)
  lv_label_set_text(obj, text)
  lv_obj_set_pos(obj, x, y)
  lv_obj_set_width(obj, width)
  lv_obj_set_style_text_font(obj, LV_FONT_MONTSERRAT_20, MAIN)
  lv_obj_set_style_text_color(obj, TEXT, MAIN)
  lv_obj_set_style_text_align(obj, align, MAIN)
  return obj
end

local function build_ui()
  lv_obj_clean(root)
  style(root, BG)
  for index, name in ipairs({ "5h", "Weekly" }) do
    local y = 42 + (index - 1) * 110
    label(name, 16, y, 120, LV_TEXT_ALIGN_LEFT)
    local row = { value = label("--", 224, y, 80, LV_TEXT_ALIGN_RIGHT), cells = {} }
    for i = 1, 20 do
      local cell = lv_obj_create(root)
      style(cell, EMPTY)
      lv_obj_set_pos(cell, 16 + math.floor((i - 1) * 276 / 19), y + 36)
      lv_obj_set_size(cell, 12, 26)
      row.cells[i] = cell
    end
    APP.rows[index] = row
  end
end

local function render(row, value)
  -- JSON null, missing fields and non-finite values represent unknown quota.
  local n = type(value) == "number" and value or nil
  if n and (n ~= n or n == math.huge or n == -math.huge) then n = nil end
  if n then n = math.max(0, math.min(100, n)) end
  local color = not n and EMPTY or n <= 10 and 0xFF7B7B or n <= 30 and 0xFFD166 or 0x67E5A5
  lv_label_set_text(row.value, n and string.format("%.0f%%", n) or "--")
  lv_obj_set_style_text_color(row.value, n and color or TEXT, MAIN)
  for i, cell in ipairs(row.cells) do
    -- Each cell represents 5%; opacity also represents a partly filled cell.
    local fraction = n and math.max(0, math.min(1, n / 5 - (i - 1))) or 0
    lv_obj_set_style_bg_color(cell, fraction > 0 and color or EMPTY, MAIN)
    lv_obj_set_style_bg_opa(cell, fraction > 0 and math.floor(80 + 175 * fraction) or 255, MAIN)
  end
end

local function update(data)
  render(APP.rows[1], data and data.quota_5h_pct)
  render(APP.rows[2], data and data.quota_7d_pct)
end

local function request_state()
  if APP.stopped or APP.busy or app.exiting() then return end
  APP.busy = true
  local ok = pcall(http.get, config.service_url .. "/state", {
    timeout = 5000, headers = { Accept = "application/json" }
  }, function(code, body)
    if APP.stopped or rawget(_G, APP_KEY) ~= APP or app.exiting() then return end
    APP.busy = false
    local decoded, data = false, nil
    if code == 200 and type(body) == "string" then decoded, data = pcall(sjson.decode, body) end
    -- sjson may decode JSON null to a truthy sentinel rather than Lua nil.
    local has_error = decoded and type(data) == "table"
        and type(data.error) == "string" and data.error ~= ""
    if decoded and type(data) == "table" and not has_error then update(data) else update(nil) end
  end)
  if not ok then
    APP.busy = false; update(nil)
  end
end

function APP.stop()
  if APP.stopped then return end
  APP.stopped = true

  -- 先停止刷新，再释放定时器引用。
  if APP.timer then
    pcall(function() APP.timer:stop() end)
    pcall(function() APP.timer:unregister() end)
    APP.timer = nil
  end
  pcall(key.off)
  if rawget(_G, APP_KEY) == APP then
    lv_obj_clean(root)
    _G[APP_KEY] = nil
  end
end

local function on_up(event)
  if event == key.SHORT then
    request_state()
  end
end

local function on_home(event)
  if event == key.SHORT then
    app.exit()
  end
end

-- 以下为启动逻辑，按执行顺序集中放在末尾。
local previous = rawget(_G, APP_KEY)
if previous and previous.stop then
  previous.stop()
end

_G[APP_KEY] = APP
APP.shutdown = APP.stop
root = lv_scr_act()
load_config()
build_ui()

APP.timer = tmr.create()
APP.timer:alarm(config.poll_seconds * 1000, tmr.ALARM_AUTO, request_state)

key.on(key.UP, on_up)
key.on(key.HOME, on_home)

-- 启动后立即刷新一次，不必等第一个定时周期。
request_state()
