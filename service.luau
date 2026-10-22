--!nonstrict
-- Headless service: warms up the PTY daemon when the plugin is enabled and
-- shuts it down on uninstall.  Disabling the plugin keeps the daemon (and its
-- sessions) alive on purpose; re-enabling reconnects to the same sessions.

local backend = require("./lib/backend.luau")

local initError = backend.init()
if initError then
  noctalia.log("terminal backend init failed: " .. tostring(initError))
  noctalia.state.set("terminal.backend_ok", false)
else
  local function ping()
    backend.ping(function(res)
      if res.ok then
        noctalia.state.set("terminal.backend_ok", true)
      else
        noctalia.state.set("terminal.backend_ok", false)
        noctalia.log("terminal daemon ping failed: " .. tostring(res.error))
      end
    end)
  end
  ping()

  function onEnable()
    ping()
  end

  function onIpc(event)
    if event == "shutdown" then
      backend.shutdown()
    elseif event == "ping" then
      ping()
    end
  end

  function onExit(_signal, reason)
    if reason ~= "uninstall" then
      return
    end
    -- The plugin directory may disappear immediately after this callback, so
    -- use a self-contained client instead of backend/termctl.py.
    local socketPath = backend.socketPath
    if not socketPath then
      return
    end
    local code = [[
import socket, sys
s = socket.socket(socket.AF_UNIX)
s.settimeout(2)
try:
    s.connect(sys.argv[1])
    s.sendall(b'{"cmd":"shutdown"}\n')
    s.recv(1024)
except Exception:
    pass
finally:
    s.close()
]]
    noctalia.runAsync({ "python3", "-c", code, socketPath }, function() end)
  end
end
