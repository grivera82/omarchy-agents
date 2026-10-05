import QtQuick
import Quickshell
import Quickshell.Io

// Owns the single `agents daemon` process, which watches Claude Code and Codex
// session files, and mirrors its state. Bar widgets on every monitor share it.
Item {
  id: root

  property var shell: null
  property var manifest: null

  readonly property string cli: String(Qt.resolvedUrl("bin/agents")).replace(/^file:\/\//, "")

  property var state: ({ status: "starting" })
  property string lastError: ""
  property int serial: 0

  readonly property bool running: daemon.running
  readonly property var sessions: state.sessions || []
  readonly property var counts: state.counts || ({ waiting: 0, working: 0, idle: 0 })
  readonly property int burn: state.burn || 0
  readonly property var config: state.config || ({})

  function send(cmd, args) {
    if (!daemon.running) return false
    daemon.write(JSON.stringify(Object.assign({ cmd: cmd, id: ++serial }, args || {})) + "\n")
    return true
  }

  function focus(sessionId) { return send("focus", { session: sessionId || "next" }) }

  function handleLine(line) {
    var msg
    try { msg = JSON.parse(line) } catch (e) { return }
    if (msg.type === "state") {
      root.state = msg.state || {}
    } else if (msg.type === "result" && !msg.ok) {
      root.lastError = msg.error || "command failed"
      clearError.restart()
    } else if (msg.type === "log" && msg.error) {
      console.warn("grivera.agents:", msg.error)
    }
  }

  Process {
    id: daemon
    command: [root.cli, "daemon"]
    running: true
    stdinEnabled: true
    stdout: SplitParser { onRead: function(line) { root.handleLine(line) } }
    onRunningChanged: {
      if (running) return
      root.state = Object.assign({}, root.state, { status: "stopped" })
      restart.restart()
    }
  }

  Timer {
    id: restart
    interval: 10000
    onTriggered: daemon.running = true
  }

  Timer {
    id: clearError
    interval: 6000
    onTriggered: root.lastError = ""
  }
}
