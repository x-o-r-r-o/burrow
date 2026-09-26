// Burrow menu bar health indicator.
// Usage: BurrowMenu <path/to/engine.py> [refresh seconds]
// Shows the health score in the menu bar and a summary menu; menu actions open
// Burrow commands in Alfred through the workflow's `open` external trigger.
import AppKit

let bundleID = "io.github.burrow-alfred"

final class BurrowMenu: NSObject, NSApplicationDelegate {
    let enginePath: String
    let interval: TimeInterval
    var statusItem: NSStatusItem!
    var timer: Timer?
    var refreshing = false

    init(enginePath: String, interval: TimeInterval) {
        self.enginePath = enginePath
        self.interval = interval
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "Burrow"
        statusItem.menu = buildMenu(rows: ["Reading system status…"])
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in self?.refresh() }
    }

    @objc func refresh() {
        // Burrow was removed from Alfred: tidy up instead of failing at every login.
        if !FileManager.default.fileExists(atPath: enginePath) {
            let agent = NSHomeDirectory() + "/Library/LaunchAgents/\(bundleID).menubar.plist"
            try? FileManager.default.removeItem(atPath: agent)
            NSApp.terminate(nil)
            return
        }
        if refreshing { return }
        refreshing = true
        let engine = enginePath
        DispatchQueue.global(qos: .utility).async {
            let data = BurrowMenu.runEngine(engine)
            DispatchQueue.main.async {
                self.refreshing = false
                self.update(data)
            }
        }
    }

    static func runEngine(_ engine: String) -> [String: Any]? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = [engine, "status"]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        do { try process.run() } catch { return nil }
        let output = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return (try? JSONSerialization.jsonObject(with: output)) as? [String: Any]
    }

    func update(_ status: [String: Any]?) {
        guard let s = status, let score = s["health_score"] as? Int else {
            statusItem.button?.image = nil
            statusItem.button?.title = "Burrow ⚠︎"
            statusItem.menu = buildMenu(rows: ["Couldn't read system status"])
            return
        }
        let color: NSColor = score >= 90 ? .systemGreen : score >= 70 ? .systemYellow : score >= 50 ? .systemOrange : .systemRed
        if let heart = NSImage(systemSymbolName: "heart.fill", accessibilityDescription: "Health") {
            let config = NSImage.SymbolConfiguration(pointSize: 13, weight: .regular).applying(.init(paletteColors: [color]))
            statusItem.button?.image = heart.withSymbolConfiguration(config)
            statusItem.button?.imagePosition = .imageLeading
        }
        var lowDisk = false
        if let disks = s["disks"] as? [[String: Any]], let root = disks.first(where: { ($0["mount"] as? String) == "/" }) {
            let total = root["total"] as? Double ?? 0
            let free = total - (root["used"] as? Double ?? 0)
            lowDisk = total > 0 && (free < 10 * 1_073_741_824 || free / total < 0.1)
        }
        statusItem.button?.title = " \(score)" + (lowDisk ? " ⚠︎" : "")
        statusItem.button?.toolTip = "Burrow — health \(score)/100"

        var rows = ["Health \(score)/100 — \(s["health_score_msg"] as? String ?? "")"]
        if lowDisk { rows.append("⚠︎ Startup disk almost full — try Clean System") }
        let cpu = s["cpu"] as? [String: Any] ?? [:]
        let thermal = s["thermal"] as? [String: Any] ?? [:]
        var cpuRow = String(format: "CPU %.0f%%", cpu["usage"] as? Double ?? 0)
        if let t = thermal["cpu_temp"] as? Double, t > 0 { cpuRow += String(format: " · %.0f°C", t) }
        rows.append(cpuRow)
        if let mem = s["memory"] as? [String: Any] {
            rows.append(String(format: "Memory %.0f%% · %@ of %@ · pressure %@",
                               mem["used_percent"] as? Double ?? 0,
                               BurrowMenu.bytes(mem["used"]), BurrowMenu.bytes(mem["total"]),
                               mem["pressure"] as? String ?? "normal"))
        }
        if let disks = s["disks"] as? [[String: Any]], let root = disks.first(where: { ($0["mount"] as? String) == "/" }) {
            let free = (root["total"] as? Double ?? 0) - (root["used"] as? Double ?? 0)
            rows.append("Disk \(BurrowMenu.bytes(free)) free of \(BurrowMenu.bytes(root["total"]))")
        }
        if let battery = (s["batteries"] as? [[String: Any]])?.first {
            rows.append("Battery \(battery["percent"] as? Int ?? 0)% · \(battery["status"] as? String ?? "")")
        }
        if let n = s["app_updates"] as? Int, n > 0 {
            rows.append("⬆︎ \(n) app update\(n == 1 ? "" : "s") available — open App Updates")
        }
        if let fans = thermal["fans"] as? [Int], !fans.isEmpty {
            rows.append("Fans " + fans.map { "\($0) rpm" }.joined(separator: " / "))
        }
        statusItem.menu = buildMenu(rows: rows)
    }

    static func bytes(_ value: Any?) -> String {
        let n = (value as? Double) ?? Double(value as? Int ?? 0)
        let formatter = ByteCountFormatter()
        formatter.countStyle = .memory
        return formatter.string(fromByteCount: Int64(n))
    }

    func buildMenu(rows: [String]) -> NSMenu {
        let menu = NSMenu()
        for (i, row) in rows.enumerated() {
            let item = NSMenuItem(title: row, action: nil, keyEquivalent: "")
            if i == 0 { item.attributedTitle = NSAttributedString(string: row, attributes: [.font: NSFont.boldSystemFont(ofSize: 13)]) }
            menu.addItem(item)
        }
        menu.addItem(.separator())
        for (title, query, key) in [
            ("Open System Status", "status", "s"),
            ("App Updates…", "updates", "u"),
            ("Clean System…", "clean", "c"),
            ("Analyze Disk…", "analyze", "a"),
            ("Optimize System…", "optimize", "o"),
        ] {
            let item = NSMenuItem(title: title, action: #selector(openInAlfred(_:)), keyEquivalent: key)
            item.representedObject = query
            item.target = self
            menu.addItem(item)
        }
        menu.addItem(.separator())
        let refreshItem = NSMenuItem(title: "Refresh Now", action: #selector(refresh), keyEquivalent: "r")
        refreshItem.target = self
        menu.addItem(refreshItem)
        let quit = NSMenuItem(title: "Quit Burrow Menu", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        menu.addItem(quit)
        return menu
    }

    @objc func openInAlfred(_ sender: NSMenuItem) {
        guard let query = sender.representedObject as? String else { return }
        var components = URLComponents()
        components.scheme = "alfred"
        components.host = "runtrigger"
        components.path = "/\(bundleID)/open/"
        components.queryItems = [URLQueryItem(name: "argument", value: query)]
        if let url = components.url { NSWorkspace.shared.open(url) }
    }
}

let args = CommandLine.arguments
guard args.count > 1 else {
    FileHandle.standardError.write("usage: BurrowMenu <engine.py> [seconds]\n".data(using: .utf8)!)
    exit(2)
}
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = BurrowMenu(enginePath: args[1], interval: max(5, Double(args.count > 2 ? args[2] : "30") ?? 30))
app.delegate = delegate
app.run()
