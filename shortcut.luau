--!nonstrict
-- Control-center tile: opens the terminal panel.

shortcut.setLabel(noctalia.tr("title"))
shortcut.setIcon("terminal-2")
shortcut.setActive(false)

function onClick()
  noctalia.togglePanel("ayone/embetty-noctalia:terminal")
end
