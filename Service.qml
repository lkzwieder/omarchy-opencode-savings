import QtQuick
import Quickshell
import Quickshell.Io

// The whole plugin is one collector on a timer.
//
// Omarchy's agents panel draws whatever JSON records it finds in
// ~/.local/state/omarchy/agents/usage/ and does not care who wrote them. So
// this service never touches the panel: it just runs the opencode collector
// and drops the record in that directory, and the panel grows an "Opencode"
// tab on its own.
Item {
  id: root
  visible: false

  property var settings: ({})

  readonly property string home: Quickshell.env("HOME") || ""
  readonly property string stateDir:
    (Quickshell.env("XDG_STATE_HOME") || home + "/.local/state") + "/omarchy/agents/usage"

  // A third-party plugin lives wherever the user installed it, so resolve the
  // collector relative to this file instead of guessing a path.
  readonly property string pluginDir: {
    var url = String(Qt.resolvedUrl("."))
    var path = url.indexOf("file://") === 0 ? url.slice(7) : url
    return path.charAt(path.length - 1) === "/" ? path.slice(0, -1) : path
  }

  readonly property int refreshIntervalSec:
    Math.max(60, Number(setting("refreshIntervalSec", 900)))

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  Process {
    id: collector
    running: false
    command: ["sh", "-c",
      "mkdir -p '" + root.stateDir + "' && " +
      "record=$('" + root.pluginDir + "/bin/opencode-usage-record') && " +
      "[ -n \"$record\" ] && " +
      "tmp=$(mktemp '" + root.stateDir + "/.opencode.XXXXXX') && " +
      "printf '%s\\n' \"$record\" > \"$tmp\" && " +
      "mv \"$tmp\" '" + root.stateDir + "/opencode.json'"]

    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: if (text.trim() !== "") console.warn("lkzwieder.opencode-usage", text.trim())
    }
  }

  Timer {
    interval: root.refreshIntervalSec * 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: if (!collector.running) collector.running = true
  }

  // `omarchy-shell lkzwieder.opencode-usage refresh`, handy after a long session.
  IpcHandler {
    target: "lkzwieder.opencode-usage"

    function refresh(): string {
      if (!collector.running) collector.running = true
      return "refreshing"
    }
  }
}
