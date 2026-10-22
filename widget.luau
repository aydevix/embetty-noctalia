--!nonstrict
-- Bar widget: click to open the terminal panel.

local glyph = noctalia.getConfig("glyph") or "terminal-2"
local showLabel = noctalia.getConfig("show_label") == true

local function render()
  barWidget.setGlyph(glyph)
  barWidget.setText(showLabel and noctalia.tr("title") or "")
  barWidget.setTooltip(noctalia.tr("widget.tooltip"))
end

render()
noctalia.setUpdateInterval(60000)

function update()
  render()
end

function onClick()
  noctalia.togglePanel("ayone/embetty-noctalia:terminal")
end

function onIpc(event)
  if event == "toggle" or event == "open" then
    noctalia.togglePanel("ayone/embetty-noctalia:terminal")
  end
end
