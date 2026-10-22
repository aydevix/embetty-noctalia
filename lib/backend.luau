--!nonstrict
-- Small Luau client for the Python PTY daemon.  Every call writes one JSON
-- request to the plugin data directory, runs backend/termctl.py, and decodes
-- the single JSON line it prints.  All calls are asynchronous.

local M = {}

M.ready = false
M.error = nil
M.dataDir = nil
M.pluginDir = nil
M.requestsDir = nil
M.uiDir = nil
M.socketPath = nil

local initialized = false
local sequence = 0

math.randomseed(noctalia.nowMs())

local function fail(message)
  return { ok = false, error = message }
end

function M.init()
  if initialized then
    return M.error
  end
  initialized = true

  local pluginDir = noctalia.pluginDir()
  if not pluginDir or pluginDir == "" then
    M.error = "plugin directory is unavailable"
    return M.error
  end
  M.pluginDir = pluginDir

  local dataDir, dataErr = noctalia.pluginDataDir()
  if not dataDir or dataDir == "" then
    M.error = "plugin data directory is unavailable: " .. tostring(dataErr or "unknown error")
    return M.error
  end
  M.dataDir = dataDir
  M.socketPath = dataDir .. "/termd.sock"
  M.requestsDir = dataDir .. "/requests"
  M.uiDir = dataDir .. "/ui"

  local ok, mkErr = noctalia.mkdirAll(M.requestsDir)
  if not ok then
    M.error = "could not create request directory: " .. tostring(mkErr or "unknown error")
    return M.error
  end

  if not noctalia.commandExists("python3") then
    M.error = "python3 is required but was not found on PATH"
    return M.error
  end

  local shell = noctalia.getConfig("shell")
  if shell and shell ~= "" then
    local found = noctalia.commandExists(shell)
    if not found and shell:find("/") then
      found = noctalia.fileExists(shell)
    end
    if not found then
      M.error = "configured shell was not found: " .. tostring(shell)
      return M.error
    end
  end

  M.ready = true
  return nil
end

function M.request(payload, callback)
  if not M.ready then
    callback(fail(M.error or "terminal backend is not initialized"))
    return false
  end

  sequence += 1
  local name = string.format(
    "%d-%d-%d.json",
    noctalia.nowMs(),
    math.random(1, 1000000000),
    sequence
  )
  local path = M.requestsDir .. "/" .. name
  local encoded = noctalia.json.encode(payload)
  if not encoded then
    callback(fail("could not encode backend request"))
    return false
  end

  local written, writeErr = noctalia.writeFile(path, encoded)
  if not written then
    callback(fail("could not write backend request: " .. tostring(writeErr or "unknown error")))
    return false
  end

  local argv = {
    "python3",
    "-S",
    M.pluginDir .. "/backend/termctl.py",
    "--data",
    M.dataDir,
    "--request",
    path,
  }

  local accepted = noctalia.runAsync(argv, function(result)
    noctalia.removeFile(path)
    if not result then
      callback(fail("terminal backend returned no result"))
      return
    end
    local stdout = result.stdout or ""
    local decoded = noctalia.json.decode(stdout)
    if type(decoded) == "table" then
      callback(decoded)
      return
    end
    local detail = result.stderr
    if not detail or detail == "" then
      detail = stdout
    end
    if not detail or detail == "" then
      detail = "exit code " .. tostring(result.exitCode or "unknown")
    end
    callback(fail("terminal backend error: " .. tostring(detail):sub(1, 800)))
  end)

  if not accepted then
    noctalia.removeFile(path)
    callback(fail("Noctalia refused to start the terminal backend"))
    return false
  end
  return true
end

function M.ping(callback)
  return M.request({ cmd = "ping" }, callback)
end

function M.list(callback, maxChars)
  local payload = { cmd = "list" }
  if maxChars then
    payload.max_chars = maxChars
  end
  return M.request(payload, callback)
end

function M.create(name, startup, maxSessions, guard, shell, callback)
  return M.request({
    cmd = "create",
    name = name,
    startup = startup or "",
    max_sessions = maxSessions or 16,
    guard = guard ~= false,
    shell = shell or noctalia.getConfig("shell") or "bash",
  }, callback)
end

function M.send(name, text, guard, callback)
  return M.request({
    cmd = "send",
    name = name,
    text = text,
    guard = guard ~= false,
  }, callback)
end

-- Raw input for a foreground password prompt.  The daemon verifies that a
-- non-shell process is actually in the foreground, so this cannot be used to
-- bypass the command guard at a shell prompt.
function M.sendPassword(name, text, callback)
  return M.request({
    cmd = "send",
    name = name,
    text = text,
    guard = false,
    password = true,
  }, callback)
end

function M.sendKey(name, key, callback)
  return M.request({ cmd = "send_key", name = name, key = key }, callback)
end

function M.capture(name, maxChars, callback)
  return M.request({
    cmd = "capture",
    name = name,
    max_chars = maxChars or 16000,
  }, callback)
end

function M.clear(name, callback)
  return M.request({ cmd = "clear", name = name }, callback)
end

function M.rename(from, to, callback)
  return M.request({ cmd = "rename", from = from, to = to }, callback)
end

function M.remove(name, callback)
  return M.request({ cmd = "remove", name = name }, callback)
end

function M.restart(name, callback)
  return M.request({ cmd = "restart", name = name }, callback)
end

function M.shutdown(callback)
  return M.request({ cmd = "shutdown" }, callback or function() end)
end

return M
