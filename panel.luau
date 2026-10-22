--!nonstrict
-- Terminal panel: a persistent PTY session rendered as a scrollback + command
-- line.  Sessions live in the Python backend, so closing the panel does not
-- stop the shell.

local backend = require("./lib/backend.luau")

local tr = noctalia.tr
local trim = noctalia.string.trim

local FONT_FAMILY = noctalia.getConfig("font_family")
if not FONT_FAMILY or FONT_FAMILY == "" then
  FONT_FAMILY = "DejaVu Sans Mono"
end
local FONT_SIZE = tonumber(noctalia.getConfig("font_size")) or 12
local SCROLLBACK = tonumber(noctalia.getConfig("scrollback_chars")) or 16000
local MAX_SESSIONS = tonumber(noctalia.getConfig("max_sessions")) or 12
local GUARD = noctalia.getConfig("block_destructive") ~= false
local STARTUP = noctalia.getConfig("startup_command") or ""
local SHELL = noctalia.getConfig("shell") or "bash"
local DEFAULT_SESSION = "main"
-- The daemon publishes an atomically replaced snapshot for each session.
-- A tiny runStream helper notifies the panel when that file changes, so a
-- normal frame does not start a Python process or poll the filesystem.
local SNAPSHOT_POLL_MS = 50
local SNAPSHOT_FALLBACK_MS = 500
local STREAM_STALE_MS = 3000
-- A label with the whole 16k scrollback is expensive to re-layout in software
-- rendering.  Keep the full text in memory (copy/history), but draw a tail
-- window that is large enough for normal terminal use.
local UI_VISIBLE_CHARS = math.min(SCROLLBACK, 4000)

local SAFETY_KEYS = {
  rm_protected = "safety.rm_protected",
  rm_no_preserve = "safety.rm_no_preserve",
  mv_protected = "safety.mv_protected",
  chmod_protected = "safety.chmod_protected",
  dd_device = "safety.dd_device",
  format_device = "safety.format_device",
  wipe_device = "safety.wipe_device",
  destroy_protected = "safety.destroy_protected",
  find_protected = "safety.find_protected",
  write_device = "safety.write_device",
  write_protected = "safety.write_protected",
  forkbomb = "safety.forkbomb",
  invalid = "safety.invalid",
}

local isOpen = false
local ready = false
local sessions = {}
local current = nil
local output = ""
local outputRev = 0
local statusText = nil
local statusIsError = false
local statusSource = nil
local mode = ""
local draft = ""
local formDraft = ""
local inputGen = 0
local formGen = 0
local capturePending = false
local lastCaptureError = nil
local lastSnapshotFallback = 0
local snapshotIgnoreBefore = 0
local liveMode = "slow"
local streamStarted = false
local streamControlPath = nil
local lastStreamEvent = 0
local lastFramePoll = 0
local history = {}
local historyCursor = 0
local passwordMode = false
local passwordReveal = false

local PASSWORD_COMMANDS = {
  sudo = true,
  sudoedit = true,
  doas = true,
  pkexec = true,
  su = true,
  passwd = true,
}

local SHELL_COMMANDS = {
  bash = true,
  busybox = true,
  csh = true,
  dash = true,
  elvish = true,
  fish = true,
  ksh = true,
  nu = true,
  sh = true,
  tcsh = true,
  xonsh = true,
  zsh = true,
}

local PASSWORD_WORDS = {
  "password",
  "passphrase",
  "pass phrase",
  "passcode",
  "verification code",
  "one-time",
  "2fa",
  "парол",
  "код подтверждения",
  "одноразовый код",
  "passwort",
  "mot de passe",
  "contraseña",
  "senha",
}

local render
local captureNow
local refreshSessions
local createSession
local updateLiveSession

local function setStatus(text, isError, source)
  statusText = text
  statusIsError = isError == true
  statusSource = source
end

local function safetyReason(res)
  local key = SAFETY_KEYS[res.code or ""]
  if key then
    return tr(key, { detail = tostring(res.detail or ""):sub(1, 300) })
  end
  return res.reason or res.error or tr("safety.blocked")
end

local function findSession(name)
  for _, session in ipairs(sessions) do
    if session.name == name then
      return session
    end
  end
  return nil
end

local function selectedIndex()
  if not current then
    return 0
  end
  for index, session in ipairs(sessions) do
    if session.name == current.name then
      return index - 1
    end
  end
  return 0
end

local function lastSessionPath()
  if backend.dataDir then
    return backend.dataDir .. "/last_session"
  end
  return nil
end

local function snapshotPathFor(session)
  if backend.uiDir and session and session.id then
    return backend.uiDir .. "/" .. session.id .. ".json"
  end
  return nil
end

local function saveLastSession(name)
  local path = lastSessionPath()
  if path and name and name ~= "" then
    noctalia.writeFile(path, name)
  end
end

local function loadLastSession()
  local path = lastSessionPath()
  if not path then
    return nil
  end
  local text = noctalia.readFile(path)
  if not text then
    return nil
  end
  text = trim(text)
  if text == "" then
    return nil
  end
  return text
end

local function applyInfo(session, info)
  session.id = info.id or session.id
  session.state = info.state or session.state
  session.cwd = info.cwd or session.cwd
  session.running_command = info.running_command
  session.alive = info.alive
  session.exit_code = info.exit_code
end

local function stateLabel(session)
  if not session then
    return tr("panel.state_idle")
  end
  if session.state == "running" then
    return tr("panel.state_running", { command = session.running_command or "?" })
  elseif session.state == "sleeping" then
    return tr("panel.state_sleeping")
  elseif session.state == "exited" then
    return tr("panel.state_exited")
  end
  return tr("panel.state_idle")
end

local function stateColor(session)
  if not session then
    return "on_surface_variant"
  end
  if session.state == "running" then
    return "primary"
  elseif session.state == "sleeping" or session.state == "exited" then
    return "error"
  end
  return "on_surface_variant"
end

local function passwordPromptVisible(text)
  local last = ""
  for line in text:gmatch("[^\n]+") do
    last = line
  end
  last = trim(last)
  if last == "" then
    return false
  end
  if #last > 400 then
    last = last:sub(-400)
  end

  local lower = string.lower(last)
  local tail = lower:sub(-1)
  if tail ~= ":" and tail ~= "?" and tail ~= ">" then
    return false
  end
  for _, word in ipairs(PASSWORD_WORDS) do
    if lower:find(word, 1, true) then
      return true
    end
  end
  return false
end

local function isPasswordCommand(command)
  if not command or command == "" then
    return false
  end
  return PASSWORD_COMMANDS[string.lower(command)] == true
end

local function isShellCommand(command)
  if not command or command == "" then
    return false
  end
  return SHELL_COMMANDS[string.lower(command)] == true
end

local function refreshPasswordMode()
  local active = false
  if current and current.state == "running" and not isShellCommand(current.running_command) then
    active = passwordPromptVisible(output)
    -- Clearing the screen must not turn a live password prompt into a
    -- visible password field.  Keep masking while sudo/su/passwd waits.
    if not active and passwordMode and isPasswordCommand(current.running_command) then
      active = true
    end
  end

  if active ~= passwordMode then
    passwordMode = active
    passwordReveal = false
    if passwordMode then
      -- Recreate the input so it is focused and freshly masked.
      inputGen += 1
    end
    return true
  end
  return false
end

local function resetPasswordInput()
  passwordMode = false
  passwordReveal = false
end

refreshSessions = function(done)
  backend.list(function(res)
    if not isOpen then
      return
    end
    if not res.ok then
      setStatus(res.error or tr("panel.backend_error"), true, "capture")
      render()
      return
    end
    local wanted = current and current.name or nil
    sessions = res.sessions or {}
    current = nil
    if wanted then
      for _, session in ipairs(sessions) do
        if session.name == wanted then
          current = session
          break
        end
      end
    end
    updateLiveSession()
    render()
    if done then
      done()
    end
  end, SCROLLBACK)
end

local function applySessionData(res)
  if not isOpen or not current then
    return
  end
  if res.id and current.id and res.id ~= current.id then
    return
  end
  if res.name and res.name ~= current.name then
    return
  end
  if res.ok == false then
    local message = res.error or tr("panel.backend_error")
    if message ~= lastCaptureError then
      lastCaptureError = message
      setStatus(message, true, "capture")
      render()
    end
    return
  end
  lastCaptureError = nil
  if statusSource == "capture" then
    statusText = nil
    statusIsError = false
    statusSource = nil
  end

  local changed = false
  local text = type(res.text) == "string" and res.text or ""
  if text ~= output then
    output = text
    outputRev += 1
    changed = true
  end

  local oldState = current.state
  local oldCwd = current.cwd
  local oldCommand = current.running_command
  applyInfo(current, res)
  if current.state ~= oldState or current.cwd ~= oldCwd or current.running_command ~= oldCommand then
    changed = true
  end
  if refreshPasswordMode() then
    changed = true
  end
  if changed then
    render()
  end
end

captureNow = function()
  if not isOpen or not ready or not current or capturePending then
    return
  end
  local targetName = current.name
  capturePending = true
  backend.capture(targetName, SCROLLBACK, function(res)
    capturePending = false
    if not isOpen or not current or current.name ~= targetName then
      return
    end
    applySessionData(res)
  end)
end

local function pollSnapshot()
  if not isOpen or not ready or not current then
    return
  end
  local path = snapshotPathFor(current)
  if path then
    local raw = noctalia.readFile(path)
    if type(raw) == "string" and raw ~= "" then
      local res = noctalia.json.decode(raw)
      if type(res) == "table" and res.ok ~= false then
        if noctalia.nowMs() >= snapshotIgnoreBefore or (res.text or "") == "" then
          applySessionData(res)
          return
        end
        return
      end
    end
  end

  -- A daemon from before the snapshot feature (or a race during startup) has
  -- no UI file yet.  Fall back to the regular request path, but at a low rate:
  -- never spawn a Python process for every poll tick.
  local now = noctalia.nowMs()
  if now - lastSnapshotFallback >= SNAPSHOT_FALLBACK_MS and not capturePending then
    lastSnapshotFallback = now
    captureNow()
  end
end

local function shellQuote(value)
  return "'" .. tostring(value or ""):gsub("'", "'\\''") .. "'"
end

local function enableFrameFallback()
  if liveMode == "frames" then
    return
  end
  liveMode = "frames"
  panel.setNeedsFrameTick(true)
end

updateLiveSession = function()
  if not ready or not backend.dataDir then
    return
  end
  if not streamControlPath then
    streamControlPath = backend.dataDir .. "/ui/follow-current"
  end

  local selected = current and current.id or ""
  noctalia.writeFile(streamControlPath, selected)
  lastStreamEvent = noctalia.nowMs()

  if selected == "" or streamStarted or liveMode == "frames" then
    return
  end

  local cmd = "python3 -S "
    .. shellQuote(backend.pluginDir .. "/backend/follow.py")
    .. " --data "
    .. shellQuote(backend.dataDir)
    .. " --control "
    .. shellQuote(streamControlPath)
  local started, result = pcall(noctalia.runStream, cmd, function(line)
    lastStreamEvent = noctalia.nowMs()
    if line == "hb" or not isOpen or not current then
      return
    end
    if line == current.id then
      pollSnapshot()
    end
  end)
  local ok = started and result == true
  if ok then
    streamStarted = true
    liveMode = "stream"
    panel.setNeedsFrameTick(false)
  else
    enableFrameFallback()
  end
end

createSession = function(name, automatic)
  local sessionName = trim(name or "")
  if sessionName == "" then
    if not automatic then
      setStatus(tr("panel.name_required"), true, "action")
      render()
    end
    return
  end
  if #sessionName > 40 or sessionName:find("[/\\]") then
    setStatus(tr("panel.name_invalid"), true, "action")
    render()
    return
  end

  backend.create(sessionName, STARTUP, MAX_SESSIONS, GUARD, SHELL, function(res)
    if not isOpen then
      return
    end
    if not res.ok then
      if res.code == "exists" then
        mode = ""
        refreshSessions(function()
          local session = findSession(sessionName)
          if session then
            current = session
            output = ""
            outputRev += 1
            resetPasswordInput()
            saveLastSession(sessionName)
            updateLiveSession()
            render()
            pollSnapshot()
          end
        end)
        return
      end
      mode = ""
      if res.blocked then
        setStatus(tr("safety.blocked") .. " — " .. safetyReason(res), true, "action")
      else
        setStatus(res.error or tr("panel.create_failed"), true, "action")
      end
      render()
      return
    end

    mode = ""
    statusText = nil
    statusSource = nil
    refreshSessions(function()
      local session = findSession(sessionName)
      if session then
        current = session
        output = ""
        outputRev += 1
        resetPasswordInput()
        saveLastSession(sessionName)
        updateLiveSession()
        render()
        pollSnapshot()
      end
    end)
  end)
end

local function bootstrap(desired)
  backend.list(function(res)
    if not isOpen then
      return
    end
    if not res.ok then
      ready = false
      setStatus(res.error or tr("panel.backend_error"), true, "action")
      render()
      return
    end

    sessions = res.sessions or {}
    if #sessions == 0 then
      createSession(loadLastSession() or DEFAULT_SESSION, true)
      return
    end

    local picked = nil
    if desired and desired ~= "" then
      for _, session in ipairs(sessions) do
        if session.name == desired then
          picked = session
          break
        end
      end
    end
    if not picked then
      local last = loadLastSession()
      if last then
        for _, session in ipairs(sessions) do
          if session.name == last then
            picked = session
            break
          end
        end
      end
    end
    current = picked or sessions[1]
    saveLastSession(current.name)
    updateLiveSession()
    render()
    pollSnapshot()
  end, SCROLLBACK)
end

render = function()
  if not isOpen then
    return
  end

  local header = {
    ui.glyph({ name = "terminal-2", size = 16, color = "primary" }),
    ui.label({
      text = tr("title"),
      fontSize = 15,
      fontWeight = "bold",
      color = "on_surface",
      flexGrow = 1,
    }),
  }
  if current then
    table.insert(header, ui.glyph({ name = "circle-filled", size = 8, color = stateColor(current) }))
    table.insert(header, ui.label({
      text = stateLabel(current),
      fontSize = 11,
      color = "on_surface_variant",
      maxLines = 1,
    }))
  end
  table.insert(header, ui.button({
    glyph = "x",
    variant = "ghost",
    tooltip = tr("panel.close"),
    onClick = "onClose",
  }))

  local sessionRow
  if #sessions > 0 then
    local options = {}
    for _, session in ipairs(sessions) do
      table.insert(options, session.name)
    end
    sessionRow = ui.row({ align = "center", gap = 6 }, {
      ui.select({
        options = options,
        selectedIndex = selectedIndex(),
        onChange = "onSessionSelect",
        flexGrow = 1,
      }),
      ui.button({
        glyph = "plus",
        variant = "secondary",
        tooltip = tr("panel.new_tooltip"),
        onClick = "onNewSession",
      }),
      ui.button({
        glyph = "pencil",
        variant = "ghost",
        enabled = current ~= nil,
        tooltip = tr("panel.rename"),
        onClick = "onRenameSession",
      }),
      ui.button({
        glyph = "trash",
        variant = "ghost",
        enabled = current ~= nil,
        tooltip = tr("panel.delete"),
        onClick = "onDeleteSession",
      }),
    })
  else
    sessionRow = ui.row({ align = "center", gap = 8 }, {
      ui.label({
        text = ready and tr("panel.no_sessions") or tr("panel.connecting"),
        color = "on_surface_variant",
        flexGrow = 1,
      }),
    })
  end

  local form = nil
  if mode == "new" or mode == "rename" then
    form = ui.column({ fill = "surface_variant/0.3", radius = 8, padding = 10, gap = 8 }, {
      ui.label({
        text = mode == "new" and tr("panel.new") or tr("panel.rename"),
        fontWeight = "bold",
        color = "primary",
      }),
      ui.input({
        key = "form-" .. tostring(formGen),
        value = formDraft,
        placeholder = tr("panel.name_placeholder"),
        focus = true,
        submitOnEnter = true,
        onChange = "onFormChange",
        onSubmit = "onFormSubmit",
      }),
      ui.row({ justify = "end", gap = 8 }, {
        ui.button({ text = tr("panel.cancel"), variant = "ghost", onClick = "onFormCancel" }),
        ui.button({
          text = mode == "new" and tr("panel.create") or tr("panel.rename_action"),
          variant = "primary",
          onClick = "onFormConfirm",
        }),
      }),
    })
  elseif mode == "delete" and current then
    form = ui.column({ fill = "error/0.12", radius = 8, padding = 10, gap = 8 }, {
      ui.label({
        text = tr("panel.delete_confirm", { name = current.name }),
        color = "on_surface",
        maxLines = 2,
      }),
      ui.label({ text = tr("panel.delete_warning"), fontSize = 10, color = "error" }),
      ui.row({ justify = "end", gap = 8 }, {
        ui.button({ text = tr("panel.cancel"), variant = "ghost", onClick = "onFormCancel" }),
        ui.button({ text = tr("panel.delete_action"), variant = "destructive", onClick = "onDeleteConfirm" }),
      }),
    })
  end

  local statusNode = nil
  if statusText then
    statusNode = ui.row({
      align = "center",
      gap = 8,
      fill = statusIsError and "error/0.12" or "surface_variant/0.35",
      radius = 8,
      padding = 8,
    }, {
      ui.glyph({
        name = statusIsError and "alert-triangle" or "info-circle",
        size = 14,
        color = statusIsError and "error" or "primary",
      }),
      ui.label({
        text = statusText,
        color = statusIsError and "error" or "on_surface",
        fontSize = 11,
        flexGrow = 1,
        maxLines = 3,
      }),
    })
  end

  local toolbar = ui.row({ align = "center", gap = 6 }, {
    ui.button({
      glyph = "eraser",
      variant = "ghost",
      enabled = current ~= nil and not passwordMode,
      tooltip = tr("panel.clear"),
      onClick = "onClear",
    }),
    ui.button({
      glyph = "copy",
      variant = "ghost",
      enabled = current ~= nil and output ~= "",
      tooltip = tr("panel.copy"),
      onClick = "onCopy",
    }),
    ui.button({
      glyph = "refresh",
      variant = "ghost",
      enabled = current ~= nil,
      tooltip = tr("panel.restart"),
      onClick = "onRestart",
    }),
    ui.label({
      text = current and (current.cwd or "") or "",
      fontSize = 10,
      color = "on_surface_variant",
      maxLines = 1,
      textAlign = "end",
      flexGrow = 1,
    }),
  })

  local shownOutput = output
  if #shownOutput > UI_VISIBLE_CHARS then
    local start = #shownOutput - UI_VISIBLE_CHARS
    -- Do not start in the middle of a UTF-8 sequence.
    while start < #shownOutput do
      local byte = shownOutput:byte(start)
      if not byte or byte < 0x80 or byte >= 0xC0 then
        break
      end
      start += 1
    end
    local nextLine = shownOutput:find("\n", start, true)
    if nextLine then
      start = nextLine + 1
    end
    shownOutput = shownOutput:sub(start)
  end
  if shownOutput == "" then
    shownOutput = ready and tr("panel.empty") or tr("panel.connecting")
  end

  local outputNode = ui.scroll({
    key = "output-scroll",
    flexGrow = 1,
    fill = "surface_variant/0.25",
    radius = 8,
    padding = 10,
    stickToBottom = true,
    scrollToBottomRev = outputRev,
  }, {
    ui.label({
      key = "output-text",
      text = shownOutput,
      fontFamily = FONT_FAMILY,
      fontSize = FONT_SIZE,
      color = output == "" and "on_surface_variant" or "on_surface",
      textAlign = "start",
    }),
  })

  local inputRow = nil
  if mode == "" then
    local inputPrompt
    if passwordMode then
      inputPrompt = ui.glyph({ name = "lock", size = 14, color = "primary" })
    else
      inputPrompt = ui.label({
        text = "$",
        fontFamily = FONT_FAMILY,
        fontSize = FONT_SIZE + 1,
        fontWeight = "bold",
        color = "primary",
      })
    end

    local placeholder = tr("panel.command_placeholder")
    if passwordMode then
      placeholder = passwordReveal
        and tr("panel.password_visible_placeholder")
        or tr("panel.password_placeholder")
    end

    local inputChildren = {
      inputPrompt,
      ui.input({
        key = "cmd-" .. tostring(inputGen),
        value = draft,
        placeholder = placeholder,
        password = passwordMode and not passwordReveal,
        focus = true,
        submitOnEnter = true,
        onChange = "onInputChange",
        onSubmit = "onCommandSubmit",
        flexGrow = 1,
      }),
      ui.button({
        glyph = "history",
        variant = "ghost",
        enabled = not passwordMode and #history > 0,
        tooltip = tr("panel.history"),
        onClick = "onHistoryPrev",
      }),
      ui.button({
        text = "Ctrl+C",
        variant = "secondary",
        controlSize = "sm",
        onClick = "onCtrlC",
      }),
      ui.button({
        text = "Tab",
        variant = "ghost",
        controlSize = "sm",
        onClick = "onTab",
      }),
      ui.button({
        text = "Esc",
        variant = "ghost",
        controlSize = "sm",
        onClick = "onEscape",
      }),
    }

    if passwordMode then
      table.insert(inputChildren, 3, ui.button({
        glyph = passwordReveal and "eye-off" or "eye",
        variant = "ghost",
        tooltip = passwordReveal and tr("panel.password_hide") or tr("panel.password_reveal"),
        onClick = "onTogglePasswordReveal",
      }))
    end

    inputRow = ui.row({ align = "center", gap = 6 }, inputChildren)
  end

  local footer = ui.row({ align = "center", gap = 8 }, {
    ui.label({
      text = passwordMode and tr("panel.password_hint") or tr("panel.hint"),
      fontSize = 10,
      color = passwordMode and "primary" or "on_surface_variant",
      flexGrow = 1,
      maxLines = 1,
    }),
    ui.label({
      text = string.format("%d/%d", #sessions, MAX_SESSIONS),
      fontSize = 10,
      color = "on_surface_variant",
    }),
  })

  local children = {
    ui.row({ align = "center", gap = 8 }, header),
    sessionRow,
  }
  if form then
    table.insert(children, form)
  end
  if statusNode then
    table.insert(children, statusNode)
  end
  table.insert(children, toolbar)
  table.insert(children, outputNode)
  if inputRow then
    table.insert(children, inputRow)
  end
  table.insert(children, footer)

  panel.render(ui.column({ flexGrow = 1, gap = 10 }, children))
end

function onOpen(context)
  isOpen = true
  ready = false
  sessions = {}
  current = nil
  output = ""
  outputRev += 1
  statusText = nil
  statusIsError = false
  statusSource = nil
  mode = ""
  draft = ""
  formDraft = ""
  resetPasswordInput()
  inputGen += 1
  formGen += 1
  capturePending = false
  lastCaptureError = nil
  lastSnapshotFallback = 0
  snapshotIgnoreBefore = 0
  lastStreamEvent = noctalia.nowMs()
  lastFramePoll = 0
  panel.setWantsSecondTicks(true)
  panel.setNeedsFrameTick(liveMode == "frames")
  render()

  local initError = backend.init()
  if initError then
    setStatus(initError, true, "action")
    render()
    return
  end
  ready = true
  local desired = nil
  if type(context) == "string" and trim(context) ~= "" then
    desired = trim(context)
  end
  bootstrap(desired)
end

function onClose()
  isOpen = false
  capturePending = false
  panel.setNeedsFrameTick(false)
  if streamControlPath then
    noctalia.writeFile(streamControlPath, "")
  end
  if current then
    saveLastSession(current.name)
  end
end

function update()
  if not isOpen then
    return
  end
  -- 1 Hz safety net: the stream normally pushes changes, but if its helper
  -- process died the panel must still notice new output and switch to frames.
  if liveMode == "stream" and noctalia.nowMs() - lastStreamEvent > STREAM_STALE_MS then
    enableFrameFallback()
  end
  pollSnapshot()
end

function onFrameTick(_deltaMs)
  if not isOpen or liveMode ~= "frames" then
    return
  end
  local now = noctalia.nowMs()
  if now - lastFramePoll < SNAPSHOT_POLL_MS then
    return
  end
  lastFramePoll = now
  pollSnapshot()
end

function onCommandSubmit(value)
  if mode ~= "" then
    return
  end
  local command = value or ""
  local isPassword = passwordMode
  if trim(command) == "" and not isPassword then
    return
  end
  if not ready or not current then
    setStatus(tr("panel.backend_error"), true, "action")
    render()
    return
  end

  local targetName = current.name
  draft = ""
  inputGen += 1
  passwordReveal = false
  statusText = nil
  statusIsError = false
  statusSource = nil
  render()

  if not isPassword then
    table.insert(history, command)
    if #history > 100 then
      table.remove(history, 1)
    end
    historyCursor = #history
  end

  -- A password is not a shell command: it may contain quotes or other syntax
  -- that the safety parser would reject.  The daemon only accepts this path
  -- while a non-shell foreground process is waiting for input.
  local handleSendResult = function(res)
    if not isOpen then
      return
    end
    if res.ok then
      updateLiveSession()
      pollSnapshot()
      return
    end

    -- Never put a failed password back into the input or into history.
    if not isPassword then
      draft = command
    elseif res.code == "password_not_waiting" or res.code == "password_shell" then
      passwordMode = false
      passwordReveal = false
    end
    inputGen += 1
    if res.blocked then
      local reason = safetyReason(res)
      setStatus(tr("safety.blocked") .. " — " .. reason, true, "action")
      noctalia.notifyError(tr("title"), reason)
    else
      local message = res.error or tr("panel.send_failed")
      if res.code == "password_not_waiting" then
        message = tr("panel.password_not_waiting")
      elseif res.code == "password_shell" then
        message = tr("panel.password_shell")
      end
      setStatus(message, true, "action")
    end
    render()
  end

  if isPassword then
    backend.sendPassword(targetName, command, handleSendResult)
  else
    backend.send(targetName, command, GUARD, handleSendResult)
  end
end

function onInputChange(value)
  draft = value or ""
end

function onHistoryPrev()
  if passwordMode or #history == 0 then
    return
  end
  if historyCursor < 1 or historyCursor > #history then
    historyCursor = #history
  end
  draft = history[historyCursor]
  historyCursor -= 1
  inputGen += 1
  render()
end

function onTogglePasswordReveal()
  if not passwordMode then
    return
  end
  passwordReveal = not passwordReveal
  inputGen += 1
  render()
end

function onSessionSelect(index)
  local position = (tonumber(index) or 0) + 1
  local session = sessions[position]
  if not session or (current and session.name == current.name) then
    return
  end
  current = session
  output = ""
  outputRev += 1
  resetPasswordInput()
  statusText = nil
  statusSource = nil
  saveLastSession(session.name)
  updateLiveSession()
  render()
  pollSnapshot()
end

function onNewSession()
  mode = "new"
  formDraft = ""
  formGen += 1
  statusText = nil
  statusSource = nil
  render()
end

function onRenameSession()
  if not current then
    return
  end
  mode = "rename"
  formDraft = current.name
  formGen += 1
  statusText = nil
  statusSource = nil
  render()
end

function onDeleteSession()
  if not current then
    return
  end
  mode = "delete"
  statusText = nil
  statusSource = nil
  render()
end

function onFormCancel()
  mode = ""
  formDraft = ""
  render()
end

function onFormChange(value)
  formDraft = value or ""
end

function onFormConfirm()
  onFormSubmit(formDraft)
end

function onFormSubmit(value)
  if mode == "new" then
    createSession(value or formDraft, false)
    return
  end

  if mode ~= "rename" or not current then
    return
  end
  local newName = trim(value or formDraft)
  if newName == "" then
    setStatus(tr("panel.name_required"), true, "action")
    render()
    return
  end
  if newName == current.name then
    mode = ""
    render()
    return
  end

  local oldName = current.name
  backend.rename(oldName, newName, function(res)
    if not isOpen then
      return
    end
    if not res.ok then
      setStatus(res.error or tr("panel.rename_failed"), true, "action")
      render()
      return
    end
    mode = ""
    statusText = nil
    statusSource = nil
    refreshSessions(function()
      local session = findSession(newName)
      if session then
        current = session
        saveLastSession(newName)
      end
      updateLiveSession()
      render()
      pollSnapshot()
    end)
  end)
end

function onDeleteConfirm()
  if not current then
    return
  end
  local name = current.name
  backend.remove(name, function(res)
    if not isOpen then
      return
    end
    if not res.ok then
      setStatus(res.error or tr("panel.remove_failed"), true, "action")
      render()
      return
    end
    mode = ""
    current = nil
    output = ""
    outputRev += 1
    resetPasswordInput()
    statusText = nil
    statusSource = nil
    render()
    refreshSessions(function()
      if #sessions == 0 then
        createSession(DEFAULT_SESSION, true)
        return
      end
      current = sessions[1]
      resetPasswordInput()
      saveLastSession(current.name)
      updateLiveSession()
      render()
      pollSnapshot()
    end)
  end)
end

function onClear()
  if not current or passwordMode then
    return
  end
  local name = current.name
  backend.clear(name, function(res)
    if not isOpen then
      return
    end
    if not res.ok then
      setStatus(res.error or tr("panel.backend_error"), true, "action")
      render()
      return
    end
    output = ""
    outputRev += 1
    snapshotIgnoreBefore = noctalia.nowMs() + 150
    render()
  end)
end

function onCopy()
  if output == "" then
    noctalia.notify(tr("title"), tr("panel.copy_empty"))
    return
  end
  noctalia.copyToClipboard(output, "text/plain")
  noctalia.notify(tr("title"), tr("panel.copied"))
end

function onRestart()
  if not current then
    return
  end
  local name = current.name
  backend.restart(name, function(res)
    if not isOpen then
      return
    end
    if not res.ok then
      setStatus(res.error or tr("panel.backend_error"), true, "action")
      render()
      return
    end
    output = ""
    outputRev += 1
    resetPasswordInput()
    statusText = nil
    statusSource = nil
    snapshotIgnoreBefore = noctalia.nowMs() + 150
    updateLiveSession()
    render()
    pollSnapshot()
  end)
end

function onCtrlC()
  if not current then
    return
  end
  backend.sendKey(current.name, "ctrl-c", function(res)
    if isOpen and not res.ok then
      setStatus(res.error or tr("panel.send_failed"), true, "action")
      render()
    end
  end)
end

function onTab()
  if not current then
    return
  end
  backend.sendKey(current.name, "tab", function(res)
    if isOpen and not res.ok then
      setStatus(res.error or tr("panel.send_failed"), true, "action")
      render()
    end
  end)
end

function onEscape()
  if not current then
    return
  end
  backend.sendKey(current.name, "escape", function(res)
    if isOpen and not res.ok then
      setStatus(res.error or tr("panel.send_failed"), true, "action")
      render()
    end
  end)
end

function onIpc(event, payload)
  if not isOpen then
    return
  end

  if event == "run" and type(payload) == "string" and trim(payload) ~= "" and current then
    local targetName = current.name
    statusText = nil
    statusIsError = false
    statusSource = nil
    render()
    backend.send(targetName, payload, GUARD, function(res)
      if not isOpen then
        return
      end
      if res.ok then
        updateLiveSession()
        pollSnapshot()
      else
        local reason = res.blocked and safetyReason(res) or (res.error or tr("panel.send_failed"))
        setStatus(reason, true, "action")
        noctalia.notifyError(tr("title"), reason)
        render()
      end
    end)
  elseif event == "clear" then
    onClear()
  elseif event == "restart" then
    onRestart()
  elseif event == "new" then
    local name = type(payload) == "string" and trim(payload) or ""
    createSession(name ~= "" and name or DEFAULT_SESSION, true)
  elseif event == "session" and type(payload) == "string" then
    local name = trim(payload)
    local session = findSession(name)
    if session then
      current = session
      output = ""
      outputRev += 1
      resetPasswordInput()
      saveLastSession(name)
      updateLiveSession()
      render()
      pollSnapshot()
    end
  end
end
