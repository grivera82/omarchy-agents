import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Ui
import qs.Commons

// Agent sessions panel. Watching lives in Service.qml (one per session); this
// widget renders its state and sends commands through it.
Panel {
  id: root
  moduleName: "grivera.agents"
  ipcTarget: "grivera.agents"
  manageIpc: false

  // Panel commands plus status(), which voice assistants (Jarvis) and scripts
  // read: `omarchy-shell grivera.agents status`.
  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function usage(): void { root.showTab("usage"); root.open() }
    function sessions(): void { root.showTab("sessions"); root.open() }
    function status(): string { return JSON.stringify(root.statusSummary()) }
  }

  function statusTime(ts) { return ts ? Qt.formatDateTime(new Date(ts * 1000), "ddd MMM d, h:mm AP") : "" }

  // Session states only: titles and projects, never prompts or replies.
  function statusSummary() {
    if (!svc) return { error: "Agent Sessions isn't running" }
    var now = Date.now() / 1000
    function mins(t) { return t ? Math.round((now - t) / 60) : null }
    return {
      counts: svc.counts,
      tokensPerMinute: svc.burn,
      sessions: svc.sessions.map(function(x) {
        var o = { tool: root.toolNames[x.provider] || x.provider, title: x.title || "", project: x.cwd || "",
                  state: x.state, minutesInState: mins(x.since), model: x.model || "" }
        if (x.waitingFor) o.waitingFor = x.waitingFor
        if (x.kind && x.kind !== "interactive") o.kind = x.kind
        return o
      }),
      // Spend is an API-equivalent estimate unless the tool recorded its own cost (OpenCode, Grok).
      usage: root.usageProviders.map(function(p) {
        return { tool: p.name, plan: p.plan, estimate: p.estimate,
                 spentTodayUsd: p.spend.today, spent7DaysUsd: p.spend.week, spent30DaysUsd: p.spend.month,
                 limits: p.limits.map(function(l) {
                   return { label: l.label, percent: Math.round(l.percent * 100), resetsAt: root.statusTime(l.resetsAt) }
                 }) }
      })
    }
  }


  readonly property var toolNames: ({ claude: "Claude Code", codex: "Codex", opencode: "OpenCode", grok: "Grok" })
  readonly property var svc: root.bar && root.bar.shell ? root.bar.shell.serviceFor("grivera.agents") : null
  readonly property var sessions: svc ? svc.sessions : []
  readonly property var counts: svc ? svc.counts : ({ waiting: 0, working: 0, idle: 0 })
  readonly property int burn: svc ? svc.burn : 0
  readonly property var config: svc ? svc.config : ({})
  readonly property var usage: svc && svc.usage ? svc.usage : ({ providers: [] })
  readonly property var usageProviders: (usage.providers || []).filter(function(p) { return p.active })

  // "sessions", "usage" or "settings"; opening Usage asks the daemon for
  // fresh numbers. The gear goes back to the tab it was opened from.
  property string tab: "sessions"
  property string lastTab: "sessions"
  function showTab(t) {
    if (t !== "settings") root.lastTab = t
    root.tab = t
    if (t === "usage" && root.svc) root.svc.send("usage")
  }
  function toggleSettings() { root.showTab(root.tab === "settings" ? root.lastTab : "settings") }
  function refresh() {
    if (root.svc && root.tab !== "settings") root.svc.send(root.tab === "usage" ? "usage" : "refresh")
  }
  readonly property bool notificationsOff: root.config.notifyFinished === false && root.config.notifyWaiting === false

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
    return m.replace(/^gpt-/, "GPT-").replace(/^grok-(?=\d)/, "Grok ")
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

  function money(x) {
    x = x || 0
    if (x >= 100) return "$" + Math.round(x).toLocaleString(Qt.locale("en_US"), "f", 0)
    if (x >= 0.995 || x === 0) return "$" + x.toFixed(2)
    return "$" + x.toFixed(3)
  }

  function untilText(ts) {
    if (!ts) return ""
    var sec = ts - root.nowMs / 1000
    return sec <= 0 ? "resetting" : "resets in " + duration(sec)
  }

  function agoText(ts) {
    if (!ts) return ""
    var sec = root.nowMs / 1000 - ts
    return sec < 60 ? "just now" : duration(sec) + " ago"
  }

  function usageSummary() {
    var today = 0, month = 0
    usageProviders.forEach(function(p) { today += p.spend.today; month += p.spend.month })
    if (!usageProviders.length) return "NO USAGE YET"
    return (money(today) + " today · " + money(month) + " in 30 days").toUpperCase()
  }

  function summary() {
    if (root.tab === "usage" && svc && svc.running) return usageSummary()
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
    svc.focusSession(id)
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

    onOpenChanged: if (open && root.svc) root.svc.send(root.tab === "usage" ? "usage" : "refresh")

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === "r") root.refresh()
        else if (t === "u") root.showTab("usage")
        else if (t === "s") root.showTab("sessions")
        else if (t === ",") root.toggleSettings()
        else if (root.tab !== "sessions") return
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

          Item {
            width: parent.width
            implicitHeight: Math.max(tabs.implicitHeight, tools.implicitHeight)

            ButtonGroup {
              id: tabs
              anchors.verticalCenter: parent.verticalCenter
              options: [{ value: "sessions", label: "Sessions", tooltip: "Running sessions (s)" },
                        { value: "usage", label: "Usage", tooltip: "Plan limits and spend (u)" }]
              value: root.tab
              foreground: root.fg
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              focusable: false
              onChanged: function(v) { root.showTab(v) }
            }

            Row {
              id: tools
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(4)

              // Muting both notifications shouldn't go unnoticed.
              Text {
                anchors.verticalCenter: parent.verticalCenter
                visible: root.notificationsOff
                rightPadding: Style.space(4)
                textFormat: Text.PlainText
                text: "󰂛 notifications off"
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }

              Button {
                visible: root.tab !== "settings"
                iconText: "󰑐"
                tooltipText: root.tab === "usage" ? "Rescan spend and refresh stale limits (r)" : "Rescan sessions now (r)"
                foreground: root.fg
                onClicked: root.refresh()
              }

              Button {
                iconText: "󰒓"
                tooltipText: root.tab === "settings" ? "Close settings (,)" : "Settings (,)"
                selected: root.tab === "settings"
                foreground: root.fg
                onClicked: root.toggleSettings()
              }
            }
          }

          Column {
            width: parent.width
            visible: root.tab === "sessions"
            spacing: Style.space(12)

            Text {
              visible: root.sessions.length === 0
              width: parent.width
              topPadding: Style.space(8)
              bottomPadding: Style.space(8)
              text: "Start claude, codex, opencode or grok in a terminal and it shows up here."
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
          }

          UsageView {
            width: parent.width
            visible: root.tab === "usage"
          }

          SettingsView {
            width: parent.width
            visible: root.tab === "settings"
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
        + (row.session.provider !== "claude" && root.lightSurface ? "-light" : "") + ".svg")
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

  // ---- settings (gear) ----

  component SettingsView: Column {
    spacing: Style.space(6)

    PanelSeparator { foreground: root.fg }

    PanelSectionHeader { text: "NOTIFICATIONS"; foreground: root.fg; fontFamily: root.fontFamily }

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
      visible: root.config.notifyFinished !== false
      label: "Show the reply in notifications"
      description: "Off by default: Omarchy saves notifications in a way other users on this computer can read."
      foreground: root.fg
      fontFamily: root.fontFamily
      checked: root.config.notifyReplies === true
      onClicked: root.setConfig("notifyReplies", !checked)
    }

    Toggle {
      width: parent.width
      visible: root.config.notifyFinished !== false || root.config.notifyWaiting !== false
      label: "Name the session in notifications"
      description: "Adds the session title (often your first prompt) and folder. Off by default for the same reason."
      foreground: root.fg
      fontFamily: root.fontFamily
      checked: root.config.notifyDetails === true
      onClicked: root.setConfig("notifyDetails", !checked)
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

    Button {
      text: "Test notification"
      tooltipText: "Send a sample notification"
      foreground: root.fg
      fontFamily: root.fontFamily
      fontSize: Style.font.caption
      bordered: true
      onClicked: if (root.svc) root.svc.send("test")
    }

    Item { width: parent.width; height: Style.space(6) }

    PanelSeparator { foreground: root.fg }

    PanelSectionHeader { text: "ABOUT USAGE"; foreground: root.fg; fontFamily: root.fontFamily }

    Text {
      width: parent.width
      textFormat: Text.PlainText
      wrapMode: Text.WordWrap
      text: "Claude Code and Codex spend is what the same tokens would cost at API list prices (as of "
            + (root.usage.pricesUpdated || "?") + "); your plans don't bill per token. OpenCode shows the cost it records, and Grok "
            + "too when xAI reports one. "
            + "Plan limits come from Omarchy's usage collectors."
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }
  }

  // ---- usage tab ----

  component UsageView: Column {
    spacing: Style.space(12)

    Text {
      visible: root.usageProviders.length === 0
      width: parent.width
      topPadding: Style.space(8)
      bottomPadding: Style.space(8)
      text: root.svc && root.svc.usage && root.svc.usage.at ? "No Claude Code, Codex, OpenCode or Grok usage in the last 30 days."
                                                             : "Reading usage…"
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.body
      horizontalAlignment: Text.AlignHCenter
      wrapMode: Text.WordWrap
    }

    Repeater {
      model: root.usageProviders
      ProviderCard {
        required property var modelData
        provider: modelData
        width: parent.width
      }
    }

    Item { width: parent.width; height: Style.space(2) }
  }

  component ProviderCard: Column {
    id: card
    property var provider: ({})
    property int hoverDay: -1
    readonly property var days: provider.days || []
    // OpenCode's free models cost nothing, so chart tokens when there's no spend.
    readonly property bool chartTokens: provider.spend && provider.spend.month === 0
    readonly property real peak: Math.max.apply(null, days.map(function(d) { return card.chartTokens ? d.tokens : d.cost }).concat([0]))

    spacing: Style.space(6)

    PanelSeparator { foreground: root.fg }

    Item {
      width: parent.width
      implicitHeight: Math.max(nameRow.implicitHeight, todayText.implicitHeight)

      Row {
        id: nameRow
        spacing: Style.space(8)

        Image {
          anchors.verticalCenter: parent.verticalCenter
          width: Style.font.title
          height: Style.font.title
          sourceSize.width: width * 2
          sourceSize.height: height * 2
          fillMode: Image.PreserveAspectFit
          source: Qt.resolvedUrl("assets/" + card.provider.id
            + (card.provider.id !== "claude" && root.lightSurface ? "-light" : "") + ".svg")
        }

        Text {
          anchors.verticalCenter: parent.verticalCenter
          textFormat: Text.PlainText
          text: card.provider.name + (card.provider.plan
            ? "  ·  " + card.provider.plan.charAt(0).toUpperCase() + card.provider.plan.slice(1) : "")
          color: root.fg
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          font.bold: true
        }
      }

      Text {
        id: todayText
        anchors.right: parent.right
        anchors.verticalCenter: nameRow.verticalCenter
        textFormat: Text.PlainText
        text: root.money(card.provider.spend.today) + " today"
        color: root.fg
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        font.bold: true
      }
    }

    // ---- plan limits ----
    Repeater {
      model: card.provider.limits || []

      Column {
        required property var modelData
        readonly property real frac: Math.max(0, Math.min(1, modelData.percent))
        width: card.width
        spacing: Style.space(3)

        Item {
          width: parent.width
          implicitHeight: limitLabel.implicitHeight

          Text {
            id: limitLabel
            textFormat: Text.PlainText
            text: modelData.label
            color: root.fg
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          Text {
            anchors.right: parent.right
            textFormat: Text.PlainText
            text: Math.round(parent.parent.frac * 100) + "%" + (modelData.resetsAt ? "  ·  " + root.untilText(modelData.resetsAt) : "")
            color: parent.parent.frac >= 0.9 ? root.urgent : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: parent.parent.frac >= 0.9
          }
        }

        Rectangle {
          width: parent.width
          height: Math.max(4, Style.space(5))
          radius: height / 2
          color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.12)

          Rectangle {
            width: Math.max(frac > 0 ? height : 0, parent.width * frac)
            readonly property real frac: parent.parent.frac
            height: parent.height
            radius: parent.radius
            color: frac >= 0.9 ? root.urgent : Color.accent
          }
        }
      }
    }

    Text {
      width: parent.width
      visible: text !== ""
      textFormat: Text.PlainText
      wrapMode: Text.WordWrap
      text: card.provider.id === "opencode" || card.provider.id === "grok" ? ""
        : card.provider.limitsNote ? card.provider.limitsNote
        : card.provider.limitsAt ? "Limits updated " + root.agoText(card.provider.limitsAt) : ""
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }

    // ---- spend ----
    Text {
      width: parent.width
      textFormat: Text.PlainText
      wrapMode: Text.WordWrap
      text: (card.provider.estimate ? "API-equivalent  " : "Spent  ")
        + root.money(card.provider.spend.week) + " in 7 days  ·  " + root.money(card.provider.spend.month) + " in 30 days"
      color: root.fg
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }

    Text {
      width: parent.width
      textFormat: Text.PlainText
      elide: Text.ElideRight
      text: [root.tokens(card.provider.tokens.today) + " tokens today",
             root.tokens(card.provider.tokens.month) + " in 30 days",
             card.provider.todayPrompts ? card.provider.todayPrompts + " prompts today" : ""]
        .filter(function(x) { return !!x }).join("  ·  ")
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }

    // Daily spend for the last 14 days, today on the right. Hover a bar to read it.
    Item {
      id: chart
      width: parent.width
      height: Style.space(44)
      readonly property real gap: 2
      readonly property real barWidth: card.days.length ? (width - gap * (card.days.length - 1)) / card.days.length : 0

      Rectangle {
        anchors.bottom: parent.bottom
        width: parent.width
        height: 1
        color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.18)
      }

      Repeater {
        model: card.days

        Item {
          required property var modelData
          required property int index
          readonly property real value: card.chartTokens ? modelData.tokens : modelData.cost
          x: index * (chart.barWidth + chart.gap)
          width: chart.barWidth
          height: chart.height

          // Square at the baseline, rounded 4px at the data end.
          Item {
            anchors.bottom: parent.bottom
            width: parent.width
            height: card.peak > 0 && value > 0 ? Math.max(2, (chart.height - 1) * value / card.peak) : 0
            clip: true

            Rectangle {
              width: parent.width
              height: parent.height + radius
              radius: Math.min(4, parent.width / 2)
              color: Color.accent
              opacity: card.hoverDay < 0 || card.hoverDay === index ? 1 : 0.45
            }
          }

          MouseArea {
            anchors.fill: parent
            anchors.leftMargin: -chart.gap / 2
            anchors.rightMargin: -chart.gap / 2
            hoverEnabled: true
            onContainsMouseChanged: card.hoverDay = containsMouse ? index : (card.hoverDay === index ? -1 : card.hoverDay)
          }
        }
      }
    }

    Item {
      width: parent.width
      implicitHeight: chartLeft.implicitHeight

      Text {
        id: chartLeft
        textFormat: Text.PlainText
        text: {
          if (card.hoverDay >= 0 && card.days[card.hoverDay]) {
            var d = card.days[card.hoverDay]
            return Qt.formatDate(new Date(d.date + "T12:00:00"), "ddd MMM d") + ":  "
              + (card.chartTokens ? "" : root.money(d.cost) + "  ·  ") + root.tokens(d.tokens) + " tokens"
          }
          return card.days.length ? Qt.formatDate(new Date(card.days[0].date + "T12:00:00"), "MMM d") : ""
        }
        color: card.hoverDay >= 0 ? root.fg : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }

      Text {
        anchors.right: parent.right
        visible: card.hoverDay < 0
        textFormat: Text.PlainText
        text: (card.chartTokens ? "tokens" : "spend") + " per day  ·  today"
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }
    }

    // ---- models, priciest first ----
    Repeater {
      model: (card.provider.models || []).slice(0, 4)

      Item {
        required property var modelData
        width: card.width
        implicitHeight: modelName.implicitHeight

        Text {
          id: modelName
          anchors.left: parent.left
          anchors.right: modelCost.left
          anchors.rightMargin: Style.space(10)
          textFormat: Text.PlainText
          elide: Text.ElideRight
          text: root.model(modelData.model) + "  ·  " + root.tokens(modelData.tokens) + " tokens"
            + (modelData.cacheRead ? " + " + root.tokens(modelData.cacheRead) + " cached" : "")
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }

        Text {
          id: modelCost
          anchors.right: parent.right
          textFormat: Text.PlainText
          text: modelData.priced ? root.money(modelData.cost) : "no price"
          color: root.fg
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }
      }
    }
  }
}
