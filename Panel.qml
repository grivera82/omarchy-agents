import QtQuick
import QtQuick.Controls
import Quickshell
import qs.Ui
import qs.Commons

// Agent sessions panel. Watching lives in Service.qml (one per session); this
// widget renders its state and sends commands through it.
Panel {
  id: root
  moduleName: "grivera.agents"
  ipcTarget: "grivera.agents"

  readonly property var svc: root.bar && root.bar.shell ? root.bar.shell.serviceFor("grivera.agents") : null
  readonly property var sessions: svc ? svc.sessions : []
  readonly property var counts: svc ? svc.counts : ({ waiting: 0, working: 0, idle: 0 })
  readonly property int burn: svc ? svc.burn : 0
  readonly property var config: svc ? svc.config : ({})

  readonly property color fg: root.bar ? root.bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(fg, 1.4)
  readonly property color urgent: root.bar ? root.bar.urgent : Color.urgent
  readonly property string fontFamily: root.bar ? root.bar.fontFamily : Style.font.family
  readonly property bool lightSurface: luminance(Color.popups.background) >= 0.5

  readonly property string glyph: "󰚩"
  readonly property bool anyWaiting: counts.waiting > 0
  readonly property bool anyWorking: counts.working > 0

  // Durations read this instead of Date.now() so they keep ticking while the
  // panel or tooltip is showing.
  property double nowMs: Date.now()

  // Breathes while an agent is working, so a glance tells you something's
  // still running without opening the panel.
  property real pulse: 1
  SequentialAnimation on pulse {
    running: root.anyWorking && !root.anyWaiting
    loops: Animation.Infinite
    alwaysRunToEnd: true
    NumberAnimation { to: 0.45; duration: 1100; easing.type: Easing.InOutSine }
    NumberAnimation { to: 1; duration: 1100; easing.type: Easing.InOutSine }
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  Timer {
    interval: 1000
    repeat: true
    running: root.opened || root.anyWorking || root.anyWaiting
    triggeredOnStart: true
    onTriggered: root.nowMs = Date.now()
  }

  // ---- formatting ----

  function luminance(c) {
    function ch(v) { return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4) }
    return 0.2126 * ch(c.r) + 0.7152 * ch(c.g) + 0.0722 * ch(c.b)
  }

  function tokens(n) {
    n = n || 0
    if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M"
    if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + "k"
    return String(Math.round(n))
  }

  function duration(sec) {
    sec = Math.max(0, Math.floor(sec))
    if (sec < 60) return sec + "s"
    if (sec < 600) return Math.floor(sec / 60) + "m " + ("0" + sec % 60).slice(-2) + "s"
    if (sec < 3600) return Math.floor(sec / 60) + "m"
    if (sec < 86400) return Math.floor(sec / 3600) + "h " + ("0" + Math.floor(sec % 3600 / 60)).slice(-2) + "m"
    return Math.floor(sec / 86400) + "d"
  }

  function age(s) { return duration(root.nowMs / 1000 - s.since) }

  function model(m) {
    if (!m) return ""
    var c = /^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$/.exec(m)
    if (c) return c[1].charAt(0).toUpperCase() + c[1].slice(1) + " " + c[2] + (c[3] ? "." + c[3] : "")
    return m.replace(/^gpt-/, "GPT-")
  }

  function context(s) {
    if (!s.context) return ""
    if (s.contextWindow) return Math.round(100 * s.context / s.contextWindow) + "% ctx"
    return tokens(s.context) + " ctx"
  }

  function stateLabel(s) {
    if (s.state === "waiting") return "waiting " + age(s)
    if (s.state === "working") return "working " + age(s)
    return "idle " + age(s)
  }

  // 1-based position in the overall list, which is what the number keys use.
  function position(id) {
    for (var i = 0; i < sessions.length; i++)
      if (sessions[i].id === id) return i + 1
    return 0
  }

  function sessionsIn(state) {
    return sessions.filter(function(s) { return s.state === state })
  }

  function summary() {
    if (!svc) return "SERVICE NOT LOADED"
    if (svc.lastError) return svc.lastError.toUpperCase()
    if (!svc.running) return "BACKEND STOPPED"
    if (!sessions.length) return "NO SESSIONS RUNNING"
    var bits = []
    if (counts.waiting) bits.push(counts.waiting + " waiting")
    if (counts.working) bits.push(counts.working + " working")
    if (counts.idle) bits.push(counts.idle + " idle")
    if (burn) bits.push(tokens(burn) + " tok/min")
    return bits.join(" · ").toUpperCase()
  }

  function tooltip() {
    if (!sessions.length) return "No agent sessions"
    return sessions.map(function(s) {
      var mark = s.state === "waiting" ? "! " : s.state === "working" ? "● " : "○ "
      var line = mark + s.title + " — " + stateLabel(s)
      if (s.state === "waiting" && s.waitingFor) line += " (" + s.waitingFor + ")"
      if (s.burn) line += " · " + tokens(s.burn) + "/min"
      return line
    }).join("\n")
  }

  function focusSession(id) {
    if (!svc) return
    svc.focus(id)
    root.close()
  }

  function setConfig(key, value) {
    if (!svc) return
    var args = {}
    args[key] = value
    svc.send("config", args)
  }

  // ---- bar button ----

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.glyph
    active: root.anyWaiting
    opacity: (root.sessions.length ? 1 : 0.5) * root.pulse
    tooltipText: root.tooltip()
    onPressed: function(b) {
      if (b === Qt.RightButton) root.focusSession("next")
      else if (b === Qt.MiddleButton && root.svc) root.svc.send("refresh")
      else root.toggle()
    }
  }

  Rectangle {
    id: badge
    readonly property int count: root.anyWaiting ? root.counts.waiting : root.counts.working
    visible: count > 0
    z: 2
    width: Math.max(height, badgeText.implicitWidth + Style.space(5))
    height: Math.max(10, Math.round(Style.bar.iconFont * 0.62))
    radius: height / 2
    color: root.anyWaiting ? root.urgent : Color.accent
    anchors.right: button.right
    anchors.top: button.top
    anchors.rightMargin: Math.max(0, (button.width - Style.bar.iconCanvas) / 2 - width / 3)
    anchors.topMargin: Math.max(1, (button.height - Style.bar.iconCanvas) / 2 - height / 4)

    Text {
      id: badgeText
      anchors.centerIn: parent
      text: badge.count
      color: Color.background
      font.family: root.fontFamily
      font.pixelSize: Math.round(badge.height * 0.78)
      font.bold: true
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(420))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(720))

    onOpenChanged: if (open && root.svc) root.svc.send("refresh")

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === "r" && root.svc) root.svc.send("refresh")
        else if (t === "n") root.focusSession("next")
        else if (/^[1-9]$/.test(t) && root.sessions.length >= Number(t)) root.focusSession(root.sessions[Number(t) - 1].id)
      }

      Flickable {
        anchors.fill: parent
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height

        Column {
          id: column
          width: parent.width
          spacing: Style.space(12)

          PanelHero {
            width: parent.width
            title: "Agents"
            meta: root.summary()
            foreground: root.fg
            fontFamily: root.fontFamily
            iconComponent: Component {
              Text {
                textFormat: Text.PlainText
                text: root.glyph
                color: root.anyWaiting ? root.urgent : root.anyWorking ? Color.accent : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
              }
            }
          }

          Text {
            visible: root.sessions.length === 0
            width: parent.width
            topPadding: Style.space(8)
            bottomPadding: Style.space(8)
            text: "Start claude or codex in a terminal and it shows up here."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
          }

          Repeater {
            model: [
              { key: "waiting", label: "NEEDS YOU" },
              { key: "working", label: "WORKING" },
              { key: "idle", label: "IDLE" }
            ]

            Column {
              required property var modelData
              readonly property var items: root.sessionsIn(modelData.key)
              visible: items.length > 0
              width: column.width
              spacing: Style.space(6)

              PanelSeparator { foreground: root.fg }

              PanelSectionHeader {
                text: modelData.label
                foreground: modelData.key === "waiting" ? root.urgent : root.fg
                fontFamily: root.fontFamily
              }

              Repeater {
                model: parent.items
                SessionRow {
                  required property var modelData
                  session: modelData
                  index1: root.position(modelData.id)
                }
              }
            }
          }

          // ---------- Settings ----------
          Column {
            width: parent.width
            spacing: Style.space(6)

            PanelSeparator { foreground: root.fg }

            Toggle {
              width: parent.width
              label: "Notify when a turn finishes"
              description: "Only turns longer than " + (root.config.minTurnSeconds || 20) + " s, and not while you're looking at that terminal."
              foreground: root.fg
              fontFamily: root.fontFamily
              checked: root.config.notifyFinished !== false
              onClicked: root.setConfig("notifyFinished", !checked)
            }

            Toggle {
              width: parent.width
              label: "Notify when an agent needs you"
              description: "Permission prompts, questions and dialogs."
              foreground: root.fg
              fontFamily: root.fontFamily
              checked: root.config.notifyWaiting !== false
              onClicked: root.setConfig("notifyWaiting", !checked)
            }
          }

          Row {
            spacing: Style.space(8)

            Button {
              text: "Jump to next"
              enabled: root.sessions.length > 0
              tooltipText: "Focus the session waiting longest, else the one that finished last (n)"
              foreground: root.fg
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              bordered: true
              onClicked: root.focusSession("next")
            }

            Button {
              text: "Test notification"
              foreground: root.fg
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              bordered: true
              onClicked: if (root.svc) root.svc.send("test")
            }

            Button {
              text: "Refresh"
              tooltipText: "Rescan sessions now (r)"
              foreground: root.fg
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              bordered: true
              onClicked: if (root.svc) root.svc.send("refresh")
            }
          }

          Item { width: parent.width; height: Style.space(2) }
        }
      }
    }
  }

  component SessionRow: Item {
    id: row
    property var session: ({})
    property int index1: 0
    readonly property bool waiting: session.state === "waiting"
    readonly property bool working: session.state === "working"

    width: column.width
    implicitHeight: rowColumn.implicitHeight + Style.space(10)

    Rectangle {
      anchors.fill: parent
      radius: Style.cornerRadius
      color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, rowMouse.containsMouse ? 0.08 : 0)
      Behavior on color { ColorAnimation { duration: 120 } }
    }

    MouseArea {
      id: rowMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: root.focusSession(row.session.id)
    }

    Image {
      id: mark
      anchors.left: parent.left
      anchors.leftMargin: Style.space(6)
      anchors.top: parent.top
      anchors.topMargin: Style.space(7)
      width: Style.font.title
      height: Style.font.title
      sourceSize.width: width * 2
      sourceSize.height: height * 2
      fillMode: Image.PreserveAspectFit
      source: Qt.resolvedUrl("assets/" + row.session.provider
        + (row.session.provider === "codex" && root.lightSurface ? "-light" : "") + ".svg")
      opacity: row.session.state === "idle" ? 0.6 : 1
    }

    Column {
      id: rowColumn
      anchors.left: mark.right
      anchors.leftMargin: Style.space(10)
      anchors.right: parent.right
      anchors.rightMargin: Style.space(6)
      anchors.verticalCenter: parent.verticalCenter
      spacing: Style.space(2)

      Item {
        width: parent.width
        implicitHeight: titleText.implicitHeight

        Text {
          id: titleText
          anchors.left: parent.left
          anchors.right: stateText.left
          anchors.rightMargin: Style.space(10)
          textFormat: Text.PlainText
          text: (row.index1 >= 1 && row.index1 <= 9 ? row.index1 + "  " : "") + row.session.title
          color: root.fg
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          font.bold: true
          elide: Text.ElideRight
        }

        Text {
          id: stateText
          anchors.right: parent.right
          anchors.baseline: titleText.baseline
          textFormat: Text.PlainText
          text: root.stateLabel(row.session)
          color: row.waiting ? root.urgent : row.working ? Color.accent : root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: row.session.state !== "idle"
        }
      }

      Text {
        width: parent.width
        visible: text !== ""
        textFormat: Text.PlainText
        text: row.waiting ? "󰀦  " + (row.session.waitingFor || "input needed")
          : row.working ? (row.session.lastPrompt || "")
          : (row.session.lastText || row.session.lastPrompt || "")
        color: row.waiting ? root.urgent : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        font.italic: row.working
        elide: Text.ElideRight
      }

      Text {
        width: parent.width
        textFormat: Text.PlainText
        text: [row.session.cwd, root.model(row.session.model), root.context(row.session),
               row.session.burn ? root.tokens(row.session.burn) + "/min" : "",
               row.session.tokens ? root.tokens(row.session.tokens.fresh) + " tokens" : "",
               row.session.kind && row.session.kind !== "interactive" && row.session.provider === "claude" ? row.session.kind : ""]
          .filter(function(x) { return !!x }).join("  ·  ")
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }

      // Context fill, where the CLI tells us the window size (Codex).
      Rectangle {
        visible: !!row.session.contextWindow
        width: parent.width
        height: Math.max(3, Style.space(4))
        radius: height / 2
        color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.12)

        Rectangle {
          readonly property real frac: row.session.contextWindow ? Math.min(1, row.session.context / row.session.contextWindow) : 0
          width: parent.width * frac
          height: parent.height
          radius: parent.radius
          color: frac >= 0.85 ? root.urgent : Color.accent
        }
      }
    }
  }
}
