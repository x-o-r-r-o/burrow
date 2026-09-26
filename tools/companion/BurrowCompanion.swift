// Burrow Companion: the optional menu bar icon and window for the Burrow Alfred workflow.
//
// It finds the installed Burrow workflow in Alfred's preferences and runs its Python
// (engine.py, updates.py, browsers.py), so every action has the same safety checks as
// Alfred. Open sections with burrow-companion://updates, …://browsers or …://uninstall.
import AppKit
import ServiceManagement
import SwiftUI

let workflowBundleID = "io.github.burrow-alfred"
let releasesURL = URL(string: "https://github.com/x-o-r-r-o/burrow/releases/latest")!
var startSection = "updates"

/// The installed Burrow workflow folder (Alfred may keep its preferences in a synced folder).
func findWorkflow() -> String? {
    let fm = FileManager.default
    let home = NSHomeDirectory()
    var roots: [String] = []
    if let data = fm.contents(atPath: home + "/Library/Application Support/Alfred/prefs.json"),
       let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
       let current = obj["current"] as? String {
        roots.append((current as NSString).expandingTildeInPath)
    }
    roots.append(home + "/Library/Application Support/Alfred/Alfred.alfredpreferences")
    for root in roots {
        let dir = root + "/workflows"
        for name in (try? fm.contentsOfDirectory(atPath: dir)) ?? [] {
            let info = NSDictionary(contentsOfFile: dir + "/" + name + "/info.plist")
            if info?["bundleid"] as? String == workflowBundleID { return dir + "/" + name }
        }
    }
    return nil
}

var workflowDir: String { findWorkflow() ?? "" }

/// Environment for the workflow's Python: the same cache folder Alfred gives it.
func workflowEnvironment() -> [String: String] {
    var env = ProcessInfo.processInfo.environment
    env["alfred_workflow_bundleid"] = workflowBundleID
    env["alfred_workflow_cache"] = NSHomeDirectory() + "/Library/Caches/com.runningwithcrayons.Alfred/Workflow Data/" + workflowBundleID
    return env
}

// MARK: - Talking to Python

/// Runs `python3 <script> cli <args…>` and delivers each JSON line on the main thread.
func runCLI(_ script: String, _ args: [String], onLine: @escaping ([String: Any]) -> Void, done: @escaping () -> Void = {}) {
    DispatchQueue.global(qos: .userInitiated).async {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        p.arguments = [workflowDir + "/" + script, "cli"] + args
        p.currentDirectoryURL = URL(fileURLWithPath: workflowDir)
        p.environment = workflowEnvironment()
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { DispatchQueue.main.async(execute: done); return }
        // Blocking reads until the command closes its output: every line arrives, the last one too.
        let handle = out.fileHandleForReading
        var buffer = Data()
        func deliver(_ line: Data) {
            if let obj = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any] {
                DispatchQueue.main.async { onLine(obj) }
            }
        }
        while true {
            let chunk = handle.availableData
            if chunk.isEmpty { break }
            buffer.append(chunk)
            while let nl = buffer.firstIndex(of: 10) {
                deliver(buffer.subdata(in: buffer.startIndex..<nl))
                buffer.removeSubrange(buffer.startIndex...nl)
            }
        }
        if !buffer.isEmpty { deliver(buffer) }
        p.waitUntilExit()
        DispatchQueue.main.async(execute: done)
    }
}

func formatBytes(_ n: Double) -> String {
    let f = ByteCountFormatter(); f.countStyle = .file
    return f.string(fromByteCount: Int64(n))
}

func relative(_ t: Double?) -> String {
    guard let t = t else { return "never" }
    let f = RelativeDateTimeFormatter(); f.unitsStyle = .full
    return f.localizedString(for: Date(timeIntervalSince1970: t), relativeTo: Date())
}

/// A search field above a list (the window has no toolbar for .searchable).
struct SearchField: View {
    @Binding var text: String
    let prompt: String

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
            TextField(prompt, text: $text).textFieldStyle(.plain)
            if !text.isEmpty {
                Button { text = "" } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary) }
                    .buttonStyle(.plain).accessibilityLabel("Clear search")
            }
        }
        .padding(.horizontal, 8).padding(.vertical, 5)
        .background(Color(nsColor: .textBackgroundColor))
        .overlay(RoundedRectangle(cornerRadius: 7).stroke(Color.secondary.opacity(0.3)))
        .clipShape(RoundedRectangle(cornerRadius: 7))
        .padding(8)
    }
}

// MARK: - App updates

struct AppUpdate: Identifiable, Hashable {
    let id: String          // app path
    let name, installed, version, source, bundleId: String
    let installable: Bool
    let url, notes, releaseNotes: String?
}

final class UpdatesModel: ObservableObject {
    @Published var updates: [AppUpdate] = []
    @Published var checked: Double?
    @Published var current = 0
    @Published var unknown = 0
    @Published var ignored: [(String, String)] = []
    @Published var history: [(id: String, label: String, time: Double)] = []
    @Published var mode = "notify"
    @Published var loading = false
    @Published var progress: [String: String] = [:]   // path -> "Downloading…" / "Updated" / error
    @Published var finished: Set<String> = []
    @Published var message: String?
    @Published var hasMas = true
    @Published var hasBrew = false
    @Published var loadedOnce = false

    func load(check: Bool = false) {
        loading = true
        runCLI("updates.py", check ? ["state", "--check"] : ["state"], onLine: { o in
            self.checked = o["checked"] as? Double
            self.current = o["current"] as? Int ?? 0
            self.unknown = o["unknown"] as? Int ?? 0
            self.mode = o["mode"] as? String ?? "notify"
            self.updates = (o["updates"] as? [[String: Any]] ?? []).map {
                AppUpdate(id: $0["path"] as? String ?? "", name: $0["name"] as? String ?? "", installed: $0["installed"] as? String ?? "",
                          version: $0["version"] as? String ?? "", source: $0["source"] as? String ?? "", bundleId: $0["bundle_id"] as? String ?? "",
                          installable: $0["installable"] as? Bool ?? false, url: $0["url"] as? String, notes: $0["notes"] as? String,
                          releaseNotes: $0["release_notes"] as? String)
            }
            self.ignored = (o["ignored"] as? [[String: Any]] ?? []).map { ($0["bundle_id"] as? String ?? "", $0["rule"] as? String ?? "") }
            self.history = (o["history"] as? [[String: Any]] ?? []).map { (id: $0["id"] as? String ?? "", label: $0["label"] as? String ?? "", time: $0["time"] as? Double ?? 0) }
            self.hasMas = o["mas"] as? Bool ?? true
            if let e = o["error"] as? String { self.message = e }
            self.hasBrew = o["brew"] as? Bool ?? false
        }, done: {
            self.loading = false
            let first = !self.loadedOnce
            self.loadedOnce = true
            if first && self.checked == nil && !check { self.load(check: true) }  // never checked: check now
        })
    }

    func install(_ items: [AppUpdate]) {
        for u in items { progress[u.id] = "Waiting…" }
        runCLI("updates.py", ["install"] + items.map { $0.id }, onLine: { o in
            guard let path = o["path"] as? String else { return }
            if let d = o["done"] as? String { self.progress[path] = "Updated"; self.finished.insert(path); self.message = d }
            else if let e = o["error"] as? String { self.progress[path] = "Not updated: " + e }
            else if let m = o["message"] as? String { self.progress[path] = m }
            else if o["stage"] as? String == "downloading" { self.progress[path] = "Downloading…" }
        }, done: { self.load() })
    }

    func simple(_ args: [String]) {
        runCLI("updates.py", args, onLine: { o in
            if let e = o["error"] as? String { self.message = e }
            else if let d = o["done"] as? String { self.message = d }
        }, done: { self.load() })
    }

    func copyMasCommand() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString("brew install mas", forType: .string)
        message = "Copied “brew install mas”. Run it in Terminal, then check again."
    }
}

/// View-local state kept in an object: @State needs a compiler plugin that
/// Apple's Command Line Tools don't include, and Burrow builds without Xcode.
final class UpdatesUI: ObservableObject {
    @Published var selection: AppUpdate.ID?
    @Published var filter = ""
    @Published var confirmAll = false
}

struct UpdatesView: View {
    @ObservedObject var model: UpdatesModel
    @StateObject private var ui = UpdatesUI()

    var visible: [AppUpdate] {
        ui.filter.isEmpty ? model.updates : model.updates.filter { $0.name.localizedCaseInsensitiveContains(ui.filter) }
    }
    var installable: [AppUpdate] { model.updates.filter { $0.installable && !model.finished.contains($0.id) } }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text(model.loading ? "Checking…" : "Checked \(relative(model.checked))").foregroundStyle(.secondary).font(.callout)
                Spacer()
                Button { model.load(check: true) } label: { Label("Check now", systemImage: "arrow.clockwise") }.disabled(model.loading)
                Button("Update all (\(installable.count))") { ui.confirmAll = true }.disabled(installable.isEmpty).keyboardShortcut(.defaultAction)
            }.padding(10)
            if !model.hasMas && model.updates.contains(where: { $0.source == "App Store" && !$0.installable }) {
                HStack {
                    Image(systemName: "bag")
                    Text(model.hasBrew ? "To update App Store apps here too, install the free mas tool: brew install mas"
                                       : "App Store apps update in the App Store. Install Homebrew (brew.sh) to update them here.")
                    Spacer()
                    if model.hasBrew { Button("Copy Command") { model.copyMasCommand() } }
                }.padding(8).background(Color.secondary.opacity(0.08))
            }
            Divider()
            HSplitView {
                VStack(spacing: 0) {
                SearchField(text: $ui.filter, prompt: "Search apps")
                List(selection: $ui.selection) {
                    let ready = visible.filter { $0.installable }
                    let store = visible.filter { !$0.installable }
                    if !ready.isEmpty {
                        Section("Ready to install · \(ready.count)") { ForEach(ready) { row($0).tag($0.id) } }
                    }
                    if !store.isEmpty {
                        Section("Update elsewhere · \(store.count)") { ForEach(store) { row($0).tag($0.id) } }
                    }
                    if model.updates.isEmpty && !model.loading && model.checked != nil {
                        Label("Everything's up to date", systemImage: "checkmark.circle").foregroundStyle(.secondary)
                    }
                    if model.loading && model.updates.isEmpty {
                        HStack { ProgressView().controlSize(.small).accessibilityLabel("Checking"); Text("Checking your apps for updates…") }
                            .foregroundStyle(.secondary)
                    }
                    Section("Up to date · \(model.current)  ·  No update source · \(model.unknown)") { EmptyView() }
                    if !model.ignored.isEmpty {
                        Section("Skipped or ignored · \(model.ignored.count)") {
                            ForEach(model.ignored, id: \.0) { item in
                                HStack {
                                    Text(item.0).lineLimit(1)
                                    Spacer()
                                    Text(item.1 == "*" ? "always" : "v" + item.1).foregroundStyle(.secondary)
                                    Button("Show again") { model.simple(["unignore", item.0]) }.buttonStyle(.link).accessibilityLabel("Show \(item.0) again")
                                }
                            }
                        }
                    }
                }
                }
                .frame(minWidth: 320)
                detail.frame(minWidth: 300, maxWidth: .infinity, maxHeight: .infinity)
            }
            Divider()
            footer
        }
        .alert(installable.count == 1 ? "Install 1 update?" : "Install \(installable.count) updates?", isPresented: $ui.confirmAll) {
            Button("Install") { model.install(installable) }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Each download is checked before installing: the same app, the same developer and a valid Apple signature. Old versions go to the Trash, so you can roll back. Open apps are quit and reopened. App Store updates ask for your password once.")
        }
        .onAppear { model.load() }
        .onReceive(model.$updates) { list in
            if ui.selection == nil || !list.contains(where: { $0.id == ui.selection }) { ui.selection = list.first?.id }
        }
    }

    func row(_ u: AppUpdate) -> some View {
        HStack(spacing: 10) {
            Image(nsImage: NSWorkspace.shared.icon(forFile: u.id)).resizable().frame(width: 28, height: 28).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(u.name).fontWeight(.medium)
                Text("\(u.installed) → \(u.version) · \(u.source)").font(.caption).foregroundStyle(.secondary)
                if let p = model.progress[u.id] { Text(p).font(.caption).foregroundStyle(p.hasPrefix("Not") ? .red : .secondary) }
            }
            Spacer()
            if model.finished.contains(u.id) {
                Label("Updated", systemImage: "checkmark").labelStyle(.titleAndIcon).foregroundStyle(.green).font(.caption)
            } else if let p = model.progress[u.id], !p.hasPrefix("Not") {
                ProgressView().controlSize(.small)
            } else if u.installable {
                Button("Update") { model.install([u]) }.accessibilityLabel("Update \(u.name)")
            } else if let url = u.url, let link = URL(string: url) {
                Button("Open") { NSWorkspace.shared.open(link) }.accessibilityLabel("Open \(u.name) in the App Store")
            }
        }.padding(.vertical, 2)
    }

    @ViewBuilder var detail: some View {
        if let u = model.updates.first(where: { $0.id == ui.selection }) {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    HStack(spacing: 12) {
                        Image(nsImage: NSWorkspace.shared.icon(forFile: u.id)).resizable().frame(width: 44, height: 44)
                        VStack(alignment: .leading) {
                            Text("\(u.name) \(u.version)").font(.title3).fontWeight(.medium)
                            Text("You have \(u.installed)").foregroundStyle(.secondary)
                        }
                    }
                    HStack(spacing: 6) {
                        badge(u.source, "shippingbox")
                        if u.installable { badge("Checked before install", "checkmark.shield") }
                    }
                    if let notes = u.releaseNotes, !notes.isEmpty {
                        Text("What's new").font(.headline)
                        Text(notes).textSelection(.enabled)
                    }
                    if let notes = u.notes, let link = URL(string: notes) {
                        Link("Release notes and website", destination: link)
                    }
                    HStack {
                        if u.installable { Button("Update \(u.name)") { model.install([u]) } }
                        else if let url = u.url, let link = URL(string: url) { Button("Open in App Store") { NSWorkspace.shared.open(link) } }
                        Button("Skip \(u.version)") { model.simple(["skip", u.bundleId, u.version]) }
                        Button("Ignore app") { model.simple(["ignore", u.bundleId]) }
                    }
                    if u.installable {
                        Label("The old version goes to the Trash, so you can roll back.", systemImage: "arrow.uturn.backward")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }.padding(16).frame(maxWidth: .infinity, alignment: .leading)
            }
        } else {
            Text(model.updates.isEmpty ? "No updates" : "Select an app").foregroundStyle(.secondary)
        }
    }

    func badge(_ text: String, _ symbol: String) -> some View {
        Label(text, systemImage: symbol).font(.caption).padding(.horizontal, 8).padding(.vertical, 3)
            .background(Color.secondary.opacity(0.12)).clipShape(Capsule())
    }

    var footer: some View {
        HStack {
            Image(systemName: "clock")
            Text("Automatic checks: " + (["off": "off", "notify": "notify me daily", "install": "install daily"][model.mode] ?? model.mode))
            Text("· change it in Burrow's Workflow Configuration in Alfred").foregroundStyle(.secondary)
            Spacer()
            if !model.history.isEmpty {
                Menu("Roll back") {
                    ForEach(model.history, id: \.id) { h in
                        Button("\(h.label) (\(relative(h.time)))") { model.simple(["rollback", h.id]) }
                    }
                }.fixedSize()
            }
            if let m = model.message { Text(m).foregroundStyle(.secondary).lineLimit(1) }
        }.font(.callout).padding(8)
    }
}

// MARK: - Browsers

struct BrowserInfo: Identifiable {
    let id, name, kind: String
    let app: String?
    let installed, running: Bool
    let access: Bool?
    let cache: Double
    let profiles: [(id: String, name: String)]
}

final class BrowsersModel: ObservableObject {
    @Published var browsers: [BrowserInfo] = []
    @Published var categories: [(key: String, label: String, detail: String)] = []
    @Published var ranges: [(key: String, label: String)] = []
    @Published var chosen: [String: Set<String>] = [:]
    @Published var range: [String: String] = [:]
    @Published var profile: [String: String] = [:]
    @Published var busy: String?
    @Published var message: String?
    @Published var loaded = false
    @Published var notes: [String: [String]] = [:]

    func load() {
        runCLI("browsers.py", ["state"], onLine: { o in
            self.loaded = true
            let saved = o["choices"] as? [String: [String: Any]] ?? [:]
            for (id, c) in saved {
                if let cats = c["categories"] as? [String] { self.chosen[id] = Set(cats) }
                if let r = c["range"] as? String { self.range[id] = r }
                if let p = c["profile"] as? String { self.profile[id] = p }
            }
            self.browsers = (o["browsers"] as? [[String: Any]] ?? []).map { b in
                BrowserInfo(id: b["id"] as? String ?? "", name: b["name"] as? String ?? "", kind: b["kind"] as? String ?? "",
                            app: b["app"] as? String, installed: b["installed"] as? Bool ?? false, running: b["running"] as? Bool ?? false,
                            access: b["access"] as? Bool, cache: b["cache"] as? Double ?? 0,
                            profiles: (b["profiles"] as? [[String: Any]] ?? []).map { (id: $0["id"] as? String ?? "", name: $0["name"] as? String ?? "") })
            }
            self.categories = (o["categories"] as? [[String: Any]] ?? []).map { (key: $0["key"] as? String ?? "", label: $0["label"] as? String ?? "", detail: $0["detail"] as? String ?? "") }
            self.ranges = (o["ranges"] as? [[String: Any]] ?? []).map { (key: $0["key"] as? String ?? "", label: $0["label"] as? String ?? "") }
            for b in self.browsers where self.chosen[b.id] == nil { self.chosen[b.id] = ["cache", "history", "downloads"] }
        })
    }

    func profilesArg(_ b: BrowserInfo) -> String { (profile[b.id] ?? "all") == "all" ? "" : (profile[b.id] ?? "") }

    /// Save the choices (shared with Alfred's bubrowsers) and refresh the notes for them.
    func choicesChanged(_ b: BrowserInfo) {
        let cats = Array(chosen[b.id] ?? []).sorted()
        let change: [String: Any] = ["categories": cats, "range": range[b.id] ?? "all", "profile": profile[b.id] ?? "all"]
        if let data = try? JSONSerialization.data(withJSONObject: change), let json = String(data: data, encoding: .utf8) {
            runCLI("browsers.py", ["choose", b.id, json], onLine: { _ in })
        }
        runCLI("browsers.py", ["plan", b.id, cats.joined(separator: ","), range[b.id] ?? "all", profilesArg(b)], onLine: { o in
            self.notes[b.id] = o["notes"] as? [String] ?? (o["error"] as? String).map { [$0] } ?? []
        })
    }

    func clean(_ b: BrowserInfo) {
        busy = b.id
        let cats = (chosen[b.id] ?? []).sorted().joined(separator: ",")
        runCLI("browsers.py", ["clean", b.id, cats, range[b.id] ?? "all", profilesArg(b)], onLine: { o in
            if let e = o["error"] as? String { self.message = "Couldn't clean \(b.name): \(e)" }
            else { self.message = "\(b.name) cleaned · \(formatBytes(o["freed"] as? Double ?? 0)) moved to the Trash · undo in Alfred with burrow" }
        }, done: { self.busy = nil; self.load() })
    }

    func reset(_ b: BrowserInfo, full: Bool) {
        busy = b.id
        runCLI("browsers.py", ["reset", b.id, full ? "full" : "settings", "", profilesArg(b)], onLine: { o in
            self.message = (o["error"] as? String).map { "Couldn't reset \(b.name): \($0)" } ?? "\(b.name) \(full ? "fully reset" : "settings reset") · undo in Alfred with burrow"
        }, done: { self.busy = nil; self.load() })
    }
}

final class BrowsersUI: ObservableObject {
    @Published var selection: String?
    @Published var filter = ""
    @Published var confirm: String?   // "clean", "passwords", "reset", "full"
}

struct BrowsersView: View {
    @ObservedObject var model: BrowsersModel
    @StateObject private var ui = BrowsersUI()

    var body: some View {
        VStack(spacing: 0) {
            HSplitView {
                VStack(spacing: 0) {
                SearchField(text: $ui.filter, prompt: "Search browsers")
                List(selection: $ui.selection) {
                    ForEach(model.browsers.filter { ui.filter.isEmpty || $0.name.localizedCaseInsensitiveContains(ui.filter) }) { b in
                        HStack(spacing: 10) {
                            if let app = b.app { Image(nsImage: NSWorkspace.shared.icon(forFile: app)).resizable().frame(width: 28, height: 28) }
                            else { Image(systemName: "globe").frame(width: 28, height: 28) }
                            VStack(alignment: .leading, spacing: 2) {
                                Text(b.name).fontWeight(.medium)
                                Text([b.profiles.count == 1 ? "1 profile" : "\(b.profiles.count) profiles",
                                      b.cache > 0 ? "cache \(formatBytes(b.cache))" : "", b.running ? "open" : "",
                                      b.installed ? "" : "not installed"].filter { !$0.isEmpty }.joined(separator: " · "))
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }.tag(b.id)
                    }
                }
                }.frame(minWidth: 240)
                detail.frame(minWidth: 380, maxWidth: .infinity, maxHeight: .infinity)
            }
            if let m = model.message { Divider(); Text(m).font(.callout).foregroundStyle(.secondary).padding(8).frame(maxWidth: .infinity, alignment: .leading) }
        }
        .onAppear { model.load() }
        .onReceive(model.$browsers) { list in
            if ui.selection == nil || !list.contains(where: { $0.id == ui.selection }) { ui.selection = list.first?.id }
        }
        .onChange(of: ui.selection) { id in
            if let b = model.browsers.first(where: { $0.id == id }) { model.choicesChanged(b) }
        }
    }

    @ViewBuilder var detail: some View {
        if let b = model.browsers.first(where: { $0.id == ui.selection }) {
            if b.kind == "safari" && b.access == false {
                VStack(spacing: 12) {
                    Image(systemName: "lock.shield").font(.largeTitle)
                    Text("Safari's data is protected by macOS").font(.headline)
                    Text("To clean Safari, give Burrow Companion Full Disk Access (and Alfred too, to clean it from Alfred).")
                        .foregroundStyle(.secondary).multilineTextAlignment(.center)
                    Button("Open Privacy settings") {
                        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles")!)
                    }
                }.padding()
            } else {
                Form {
                    Section("Clean \(b.name)") {
                        ForEach(model.categories, id: \.key) { c in
                            Toggle(isOn: Binding(
                                get: { model.chosen[b.id, default: []].contains(c.key) },
                                set: { on in
                                    if on { model.chosen[b.id, default: []].insert(c.key) } else { model.chosen[b.id, default: []].remove(c.key) }
                                    model.choicesChanged(b)
                                })) {
                                VStack(alignment: .leading) { Text(c.label); Text(c.detail).font(.caption).foregroundStyle(.secondary) }
                            }
                        }
                        Picker("Time range", selection: Binding(get: { model.range[b.id] ?? "all" }, set: { model.range[b.id] = $0; model.choicesChanged(b) })) {
                            ForEach(model.ranges, id: \.key) { Text($0.label).tag($0.key) }
                        }
                        if b.profiles.count > 1 {
                            Picker("Profiles", selection: Binding(get: { model.profile[b.id] ?? "all" }, set: { model.profile[b.id] = $0; model.choicesChanged(b) })) {
                                Text("All profiles").tag("all")
                                ForEach(b.profiles, id: \.id) { Text($0.name).tag($0.id) }
                            }
                        }
                        ForEach(model.notes[b.id] ?? [], id: \.self) { n in
                            Label(n, systemImage: "info.circle").font(.caption).foregroundStyle(.secondary)
                        }
                        Text("Cache and open tabs are always cleared completely; the time range applies to the rest.")
                            .font(.caption).foregroundStyle(.secondary)
                        HStack {
                            if b.running { Label("\(b.name) will be quit first", systemImage: "exclamationmark.triangle").font(.caption) }
                            Spacer()
                            if model.busy == b.id { ProgressView().controlSize(.small) }
                            Button("Clean") { ui.confirm = model.chosen[b.id, default: []].contains("passwords") ? "passwords" : "clean" }
                                .disabled(model.chosen[b.id, default: []].isEmpty || model.busy != nil)
                                .keyboardShortcut(.defaultAction)
                        }
                    }
                    if b.kind == "chromium" || b.kind == "firefox" || b.kind == "orion" {
                        Section("Reset") {
                            if b.kind != "orion" {
                                HStack {
                                    VStack(alignment: .leading) {
                                        Text("Reset settings")
                                        Text(b.kind == "chromium" ? "Defaults. Extensions and their stored data go to the Trash too (Undo brings them back). Bookmarks, history and passwords stay."
                                                                  : "Defaults. Extensions, bookmarks, history and passwords stay.").font(.caption).foregroundStyle(.secondary)
                                    }
                                    Spacer()
                                    Button("Reset settings") { ui.confirm = "reset" }.disabled(model.busy != nil)
                                }
                            }
                            HStack {
                                VStack(alignment: .leading) { Text("Full reset"); Text("Moves the whole profile to the Trash, like a fresh install.").font(.caption).foregroundStyle(.secondary) }
                                Spacer()
                                Button("Full reset…") { ui.confirm = "full" }.disabled(model.busy != nil)
                            }
                        }
                    }
                }
                .formStyle(.grouped)
                .alert(alertTitle(b), isPresented: Binding(get: { ui.confirm != nil }, set: { if !$0 { ui.confirm = nil } })) {
                    let what = ui.confirm
                    Button(what == "passwords" ? "Remove passwords and clean" : (what == "full" ? "Fully reset" : (what == "reset" ? "Reset settings" : "Clean")), role: .destructive) {
                        if what == "clean" || what == "passwords" { model.clean(b) }
                        else { model.reset(b, full: what == "full") }
                    }
                    Button("Cancel", role: .cancel) {}
                } message: { Text(alertMessage(b)) }
            }
        } else {
            Text(!model.loaded ? "Finding browsers…" : (model.browsers.isEmpty ? "No browsers found" : "Select a browser")).foregroundStyle(.secondary)
        }
    }

    func alertTitle(_ b: BrowserInfo) -> String {
        switch ui.confirm {
        case "passwords": return "Also remove saved passwords?"
        case "full": return "Fully reset \(b.name)?"
        case "reset": return "Reset \(b.name) settings?"
        default: return "Clean \(b.name)?"
        }
    }

    func alertMessage(_ b: BrowserInfo) -> String {
        switch ui.confirm {
        case "passwords": return "Every password saved in \(b.name) will be removed along with the rest. Make sure you can sign in without them."
        case "full":
            let n = b.profiles.count
            let which = n > 1 && (model.profile[b.id] ?? "all") == "all" ? "All \(n) profiles of \(b.name)" : "The profile"
            return "\(which): bookmarks, history, passwords, extensions, cookies and settings all go to the Trash. You can undo this in Alfred with burrow."
        case "reset": return b.kind == "chromium"
            ? "Settings go back to their defaults. Extensions and their stored data (for example a wallet or password-manager extension's local vault) move to the Trash; Undo brings them back. Bookmarks, history and passwords stay."
            : "Settings go back to their defaults. Extensions, bookmarks, history and passwords stay."
        default: return "Everything goes to the Trash first, so you can undo it in Alfred with burrow."
        }
    }
}

// MARK: - Uninstaller

struct InstalledApp: Identifiable, Hashable {
    let id: String          // app path
    let name: String
    let size: Double
    let lastUsed: Double?
    let knownUse: Bool
    let modified: Double?
    let running: Bool

    var usage: String {
        if let t = lastUsed { return "opened \(relative(t))" }
        if knownUse { return "no recorded use" }
        if let m = modified { return "modified \(relative(m))" }
        return ""
    }
}

struct Leftover: Identifiable, Hashable {
    let id: String          // path
    let name, location: String
    let size: Double
    let locked, data: Bool
    var kept: Bool
}

struct AppReview {
    let path, name, version: String
    let size: Double
    let locked, running: Bool
    var items: [Leftover]
    let extensions: [String]
    let uninstallers: [String]
}

final class UninstallModel: ObservableObject {
    @Published var apps: [InstalledApp] = []
    @Published var loaded = false
    @Published var sizing = false
    @Published var unusedDays = 90
    @Published var undoLabel: String?
    @Published var review: AppReview?
    @Published var reviewing = false
    @Published var busy = false
    @Published var message: String?
    @Published var stillRunning: String?   // app path that didn't quit when asked

    func load() {
        runCLI("uninstaller.py", ["list"], onLine: { o in
            self.loaded = true
            self.sizing = o["sizing"] as? Bool ?? false
            self.unusedDays = o["unused_days"] as? Int ?? 90
            self.undoLabel = o["undo"] as? String
            self.apps = (o["apps"] as? [[String: Any]] ?? []).map { a in
                InstalledApp(id: a["path"] as? String ?? "", name: a["name"] as? String ?? "", size: a["size"] as? Double ?? 0,
                             lastUsed: a["last_used"] as? Double, knownUse: a["known_use"] as? Bool ?? false,
                             modified: a["mtime"] as? Double, running: a["running"] as? Bool ?? false)
            }
        }, done: {
            // Sizes are measured in the background the first time: check back shortly
            if self.sizing { DispatchQueue.main.asyncAfter(deadline: .now() + 5) { self.load() } }
        })
    }

    func loadReview(_ path: String?) {
        guard let path = path else { review = nil; return }
        reviewing = true
        runCLI("uninstaller.py", ["review", path], onLine: { o in
            guard (o["path"] as? String) == path else { self.review = nil; self.message = o["error"] as? String; return }
            self.review = AppReview(
                path: path, name: o["name"] as? String ?? "", version: o["version"] as? String ?? "",
                size: o["size"] as? Double ?? 0, locked: o["locked"] as? Bool ?? false, running: o["running"] as? Bool ?? false,
                items: (o["items"] as? [[String: Any]] ?? []).map { i in
                    Leftover(id: i["path"] as? String ?? "", name: i["name"] as? String ?? "", location: i["location"] as? String ?? "",
                             size: i["size"] as? Double ?? 0, locked: i["locked"] as? Bool ?? false, data: i["data"] as? Bool ?? false,
                             kept: i["kept"] as? Bool ?? false)
                },
                extensions: o["extensions"] as? [String] ?? [], uninstallers: o["uninstallers"] as? [String] ?? [])
        }, done: { self.reviewing = false })
    }

    /// Keep a leftover, or include it again (shared with Alfred's review screen).
    func toggle(_ item: Leftover) {
        guard var r = review, let i = r.items.firstIndex(of: item) else { return }
        r.items[i].kept.toggle()
        review = r
        runCLI("uninstaller.py", ["toggle", r.path, item.id], onLine: { _ in })
    }

    func run(reset: Bool, force: Bool = false) {
        guard let r = review else { return }
        busy = true
        stillRunning = nil
        message = reset ? "Resetting \(r.name)…" : "Uninstalling \(r.name)…"
        let req: [String: Any] = ["path": r.path, "reset": reset, "force": force]
        guard let data = try? JSONSerialization.data(withJSONObject: req), let json = String(data: data, encoding: .utf8) else { return }
        runCLI("uninstaller.py", ["uninstall", json], onLine: { o in
            if let d = o["done"] as? String { self.message = d }
            else if let e = o["error"] as? String {
                self.message = e
                if o["running"] as? Bool ?? false { self.stillRunning = r.path }
            }
        }, done: {
            self.busy = false
            self.load()
            self.loadReview(FileManager.default.fileExists(atPath: r.path) ? r.path : nil)
        })
    }

    func undo() {
        busy = true
        runCLI("uninstaller.py", ["undo"], onLine: { o in
            self.message = (o["done"] ?? o["error"]) as? String
        }, done: {
            self.busy = false
            self.load()
            if let r = self.review { self.loadReview(r.path) }
        })
    }
}

final class UninstallUI: ObservableObject {
    @Published var selection: InstalledApp.ID?
    @Published var filter = ""
    @Published var scope = "all"       // all, unused, largest
    @Published var confirm: String?     // "uninstall", "reset", "force", "undo"
}

struct UninstallView: View {
    @ObservedObject var model: UninstallModel
    @StateObject private var ui = UninstallUI()

    var visible: [InstalledApp] {
        var list = model.apps
        if !ui.filter.isEmpty { list = list.filter { $0.name.localizedCaseInsensitiveContains(ui.filter) } }
        let cutoff = Date().timeIntervalSince1970 - Double(model.unusedDays) * 86400
        switch ui.scope {
        case "unused": list = list.filter { ($0.lastUsed ?? 0) < cutoff }.sorted { $0.size > $1.size }
        case "largest": list = list.sorted { $0.size > $1.size }
        default: break
        }
        return list
    }

    var body: some View {
        VStack(spacing: 0) {
            HSplitView {
                VStack(spacing: 0) {
                    Picker("Show", selection: $ui.scope) {
                        Text("All").tag("all")
                        Text("Unused").tag("unused")
                        Text("Largest").tag("largest")
                    }
                    .pickerStyle(.segmented).labelsHidden().padding([.horizontal, .top], 8)
                    SearchField(text: $ui.filter, prompt: "Search apps")
                    List(selection: $ui.selection) {
                        ForEach(visible) { a in
                            HStack(spacing: 10) {
                                Image(nsImage: NSWorkspace.shared.icon(forFile: a.id)).resizable().frame(width: 28, height: 28)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(a.name).fontWeight(.medium)
                                    Text([a.size > 0 ? formatBytes(a.size) : "", a.usage, a.running ? "open" : ""]
                                            .filter { !$0.isEmpty }.joined(separator: " · "))
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                            }.tag(a.id)
                        }
                    }
                    if model.sizing {
                        HStack { ProgressView().controlSize(.small); Text("Measuring app sizes…").font(.caption).foregroundStyle(.secondary) }.padding(6)
                    }
                }.frame(minWidth: 260)
                detail.frame(minWidth: 400, maxWidth: .infinity, maxHeight: .infinity)
            }
            Divider()
            HStack {
                if model.busy { ProgressView().controlSize(.small) }
                Text(model.message ?? "Everything goes to the Trash, so it can be put back.").font(.callout).foregroundStyle(.secondary).lineLimit(2)
                Spacer()
                if let label = model.undoLabel {
                    Button("Undo “\(label)”") { ui.confirm = "undo" }.disabled(model.busy)
                }
            }.padding(8)
        }
        .onAppear { model.load() }
        .onReceive(model.$apps) { list in
            if ui.selection == nil || !list.contains(where: { $0.id == ui.selection }) {
                ui.selection = ProcessInfo.processInfo.environment["BURROW_SELECT"].flatMap { p in list.first { $0.id == p }?.id } ?? visible.first?.id
            }
        }
        .onChange(of: ui.selection) { id in model.stillRunning = nil; model.loadReview(id) }
        .alert(alertTitle, isPresented: Binding(get: { ui.confirm != nil }, set: { if !$0 { ui.confirm = nil } })) {
            let what = ui.confirm
            Button(what == "undo" ? "Undo" : (what == "reset" ? "Reset" : (what == "force" ? "Force quit and uninstall" : "Uninstall")),
                   role: what == "undo" ? nil : .destructive) {
                switch what {
                case "undo": model.undo()
                case "reset": model.run(reset: true)
                case "force": model.run(reset: false, force: true)
                default: model.run(reset: false)
                }
            }
            Button("Cancel", role: .cancel) {}
        } message: { Text(alertMessage) }
    }

    var removing: [Leftover] { (model.review?.items ?? []).filter { !$0.kept } }

    @ViewBuilder var detail: some View {
        if let r = model.review, r.path == ui.selection {
            VStack(alignment: .leading, spacing: 12) {
                HStack(spacing: 12) {
                    Image(nsImage: NSWorkspace.shared.icon(forFile: r.path)).resizable().frame(width: 48, height: 48)
                    VStack(alignment: .leading, spacing: 2) {
                        Text([r.name, r.version].filter { !$0.isEmpty }.joined(separator: " ")).font(.title3).fontWeight(.semibold)
                        Text("\(formatBytes(r.size + removing.reduce(0) { $0 + $1.size })) with leftovers" + (r.running ? " · open now" : ""))
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: r.path)]) } label: { Label("Reveal", systemImage: "folder") }
                }
                ForEach(r.uninstallers, id: \.self) { u in
                    HStack {
                        Label("The developer's uninstaller also removes drivers and extensions", systemImage: "wrench.and.screwdriver")
                        Spacer()
                        Button("Open \((u as NSString).lastPathComponent.replacingOccurrences(of: ".app", with: ""))") { NSWorkspace.shared.open(URL(fileURLWithPath: u)) }
                    }.padding(8).background(Color.accentColor.opacity(0.1)).clipShape(RoundedRectangle(cornerRadius: 6))
                }
                ForEach(r.extensions, id: \.self) { e in
                    HStack {
                        Label("Has a system extension (\(e)). macOS removes it in Login Items & Extensions.", systemImage: "exclamationmark.triangle")
                        Spacer()
                        Button("Open Settings") { NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.LoginItems-Settings.extension")!) }
                    }.padding(8).background(Color.orange.opacity(0.12)).clipShape(RoundedRectangle(cornerRadius: 6))
                }
                if r.running {
                    Label("\(r.name) is open and will be quit first", systemImage: "exclamationmark.triangle").font(.callout)
                }
                Text(r.items.isEmpty ? "No leftover files found" : "What will be removed · untick anything you want to keep")
                    .font(.callout).foregroundStyle(.secondary)
                List {
                    HStack {
                        Image(systemName: "checkmark.square.fill").foregroundStyle(.secondary)
                        Text("\(r.name).app").fontWeight(.medium)
                        if r.locked { Image(systemName: "lock.fill").font(.caption).foregroundStyle(.secondary) }
                        Spacer()
                        Text(formatBytes(r.size)).foregroundStyle(.secondary)
                    }
                    ForEach(r.items) { i in
                        HStack {
                            Toggle(isOn: Binding(get: { !i.kept }, set: { _ in model.toggle(i) })) {
                                VStack(alignment: .leading, spacing: 1) {
                                    HStack(spacing: 4) {
                                        Text(i.name)
                                        if i.locked { Image(systemName: "lock.fill").font(.caption).foregroundStyle(.secondary) }
                                    }
                                    Text(i.location).font(.caption).foregroundStyle(.secondary)
                                }
                            }
                            Spacer()
                            Text(formatBytes(i.size)).foregroundStyle(.secondary)
                        }
                        .contextMenu {
                            Button("Reveal in Finder") { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: i.id)]) }
                            Button("Copy Path") { NSPasteboard.general.clearContents(); NSPasteboard.general.setString(i.id, forType: .string) }
                        }
                    }
                }
                .listStyle(.bordered(alternatesRowBackgrounds: false))
                if r.locked || removing.contains(where: { $0.locked }) {
                    Label("Items with a lock belong to the system, so macOS asks for your password once", systemImage: "lock").font(.caption).foregroundStyle(.secondary)
                }
                HStack {
                    if model.stillRunning == r.path {
                        Button("Force quit and uninstall") { ui.confirm = "force" }.disabled(model.busy)
                    }
                    if removing.contains(where: { $0.data }) {
                        Button("Reset app") { ui.confirm = "reset" }.disabled(model.busy)
                            .help("Keep the app, remove its settings and data so it starts fresh")
                    }
                    Spacer()
                    Button("Uninstall · \(formatBytes(r.size + removing.reduce(0) { $0 + $1.size }))") { ui.confirm = "uninstall" }
                        .disabled(model.busy).keyboardShortcut(.defaultAction)
                }
            }.padding(16)
        } else if model.reviewing || (ui.selection != nil && model.review == nil && model.loaded) {
            VStack(spacing: 8) { ProgressView(); Text("Looking for leftover files…").foregroundStyle(.secondary) }
        } else {
            Text(!model.loaded ? "Listing apps…" : "Select an app to see everything it installed").foregroundStyle(.secondary)
        }
    }

    var alertTitle: String {
        let name = model.review?.name ?? "the app"
        switch ui.confirm {
        case "undo": return "Undo “\(model.undoLabel ?? "")”?"
        case "reset": return "Reset \(name)?"
        case "force": return "Force quit \(name)?"
        default: return "Uninstall \(name)?"
        }
    }

    var alertMessage: String {
        guard let r = model.review else { return "Everything goes back where it was." }
        let open = r.running ? "\(r.name) will be quit first. " : ""
        switch ui.confirm {
        case "undo": return "Everything it moved to the Trash goes back where it was. Apps that are open are quit first."
        case "reset": return open + "Its settings, caches and data go to the Trash. The app stays installed and starts fresh."
        case "force": return "Unsaved changes in \(r.name) are lost. Then \(r.name) and \(removing.count) leftover items go to the Trash."
        default: return open + "\(r.name) and \(removing.count) leftover items go to the Trash. You can put them back with Undo."
        }
    }
}

// MARK: - Window

final class RootUI: ObservableObject {
    static weak var shared: RootUI?
    @Published var section: String? = startSection
    init() { RootUI.shared = self }
}

struct RootView: View {
    @StateObject var updates = UpdatesModel()
    @StateObject var browsers = BrowsersModel()
    @StateObject var uninstaller = UninstallModel()
    @StateObject var ui = RootUI()

    var body: some View {
        HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 4) {
                sidebarButton("updates", "App updates", "arrow.down.circle", updates.updates.count)
                sidebarButton("browsers", "Browsers", "globe", 0)
                sidebarButton("uninstall", "Uninstaller", "trash", 0)
                Spacer()
            }
            .padding(10)
            .frame(width: 180)
            .background(Color(nsColor: .windowBackgroundColor))
            Divider()
            Group {
                if ui.section == "browsers" { BrowsersView(model: browsers) }
                else if ui.section == "uninstall" { UninstallView(model: uninstaller) }
                else { UpdatesView(model: updates) }
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .frame(minWidth: 860, minHeight: 540)
    }

    func sidebarButton(_ key: String, _ title: String, _ symbol: String, _ count: Int) -> some View {
        Button { ui.section = key } label: {
            HStack {
                Label(title, systemImage: symbol)
                Spacer()
                if count > 0 {
                    Text("\(count)").font(.caption).padding(.horizontal, 6).padding(.vertical, 1)
                        .background(Color.secondary.opacity(0.2)).clipShape(Capsule())
                }
            }
            .padding(.horizontal, 8).padding(.vertical, 6)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(ui.section == key ? Color.accentColor.opacity(0.18) : Color.clear)
            .clipShape(RoundedRectangle(cornerRadius: 6))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(ui.section == key ? .isSelected : [])
        .accessibilityLabel(count > 0 ? "\(title), \(count) available" : title)
    }
}

// MARK: - Menu bar

final class MenuBar: NSObject {
    var statusItem: NSStatusItem!
    var timer: Timer?
    var refreshing = false
    let openWindow: (String) -> Void

    init(openWindow: @escaping (String) -> Void) {
        self.openWindow = openWindow
        super.init()
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "Burrow"
        statusItem.menu = buildMenu(rows: ["Reading system status…"])
        refresh()
        schedule()
    }

    var interval: TimeInterval {
        let v = UserDefaults.standard.double(forKey: "refreshInterval")
        return v >= 5 ? v : 30
    }

    func schedule() {
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in self?.refresh() }
    }

    @objc func refresh() {
        let wf = workflowDir
        if wf.isEmpty {
            statusItem.button?.image = nil
            statusItem.button?.title = "Burrow ⚠︎"
            statusItem.menu = buildMenu(rows: ["Burrow isn't installed in Alfred"])
            return
        }
        if refreshing { return }
        refreshing = true
        DispatchQueue.global(qos: .utility).async {
            let data = MenuBar.runEngine(wf + "/engine.py")
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
        process.environment = workflowEnvironment()
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        do { try process.run() } catch { return nil }
        let output = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return (try? JSONSerialization.jsonObject(with: output)) as? [String: Any]
    }

    static func bytes(_ value: Any?) -> String {
        let n = (value as? Double) ?? Double(value as? Int ?? 0)
        let f = ByteCountFormatter(); f.countStyle = .memory
        return f.string(fromByteCount: Int64(n))
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
        var rootDisk: [String: Any]?
        if let disks = s["disks"] as? [[String: Any]] { rootDisk = disks.first(where: { ($0["mount"] as? String) == "/" }) }
        if let root = rootDisk {
            let total = root["total"] as? Double ?? 0
            let free = root["available"] as? Double ?? (total - (root["used"] as? Double ?? 0))
            lowDisk = total > 0 && (free < 10 * 1_073_741_824 || free / total < 0.1)
        }
        statusItem.button?.title = " \(score)" + (lowDisk ? " ⚠︎" : "")
        statusItem.button?.toolTip = "Burrow: health \(score) of 100"
        statusItem.button?.setAccessibilityLabel("Burrow, health \(score) of 100" + (lowDisk ? ", startup disk almost full" : ""))

        var rows = ["Health \(score)/100 — \(s["health_score_msg"] as? String ?? "")"]
        if lowDisk { rows.append("⚠︎ Startup disk almost full — try Clean System") }
        let cpu = s["cpu"] as? [String: Any] ?? [:]
        let thermal = s["thermal"] as? [String: Any] ?? [:]
        var cpuRow = String(format: "CPU %.0f%%", cpu["usage"] as? Double ?? 0)
        if let t = thermal["cpu_temp"] as? Double, t > 0 { cpuRow += String(format: " · %.0f°C", t) }
        rows.append(cpuRow)
        if let mem = s["memory"] as? [String: Any] {
            rows.append(String(format: "Memory %.0f%% · %@ of %@ · pressure %@", mem["used_percent"] as? Double ?? 0,
                               MenuBar.bytes(mem["used"]), MenuBar.bytes(mem["total"]), mem["pressure"] as? String ?? "normal"))
        }
        if let root = rootDisk {
            let free = root["available"] as? Double ?? ((root["total"] as? Double ?? 0) - (root["used"] as? Double ?? 0))
            rows.append("Disk \(MenuBar.bytes(free)) available of \(MenuBar.bytes(root["total"]))")
        }
        if let battery = (s["batteries"] as? [[String: Any]])?.first {
            rows.append("Battery \(battery["percent"] as? Int ?? 0)% · \(battery["status"] as? String ?? "")")
        }
        if let fans = thermal["fans"] as? [Int], !fans.isEmpty {
            rows.append("Fans " + fans.map { "\($0) rpm" }.joined(separator: " / "))
        }
        if let n = s["app_updates"] as? Int, n > 0 {
            rows.append("⬆︎ \(n) app update\(n == 1 ? "" : "s") available")
        }
        statusItem.menu = buildMenu(rows: rows)
    }

    func buildMenu(rows: [String]) -> NSMenu {
        let menu = NSMenu()
        for (i, row) in rows.enumerated() {
            let item = NSMenuItem(title: row, action: nil, keyEquivalent: "")
            if i == 0 { item.attributedTitle = NSAttributedString(string: row, attributes: [.font: NSFont.boldSystemFont(ofSize: 13)]) }
            menu.addItem(item)
        }
        menu.addItem(.separator())
        if workflowDir.isEmpty {
            let get = NSMenuItem(title: "Get the Burrow Workflow…", action: #selector(openReleases), keyEquivalent: "")
            get.target = self
            menu.addItem(get)
        } else {
            for (title, target, key) in [
                ("App Updates…", "window:updates", "u"), ("Browsers…", "window:browsers", "b"), ("Uninstaller…", "window:uninstall", "i"),
                ("System Status in Alfred", "status", "s"), ("Clean System in Alfred", "clean", "c"),
                ("Analyze Disk in Alfred", "analyze", "a"), ("Optimize System in Alfred", "optimize", "o"),
            ] {
                let item = NSMenuItem(title: title, action: #selector(go(_:)), keyEquivalent: key)
                item.representedObject = target
                item.target = self
                menu.addItem(item)
            }
        }
        menu.addItem(.separator())
        let refreshItem = NSMenuItem(title: "Refresh Now", action: #selector(refresh), keyEquivalent: "r")
        refreshItem.target = self
        menu.addItem(refreshItem)
        let every = NSMenuItem(title: "Refresh Every", action: nil, keyEquivalent: "")
        let sub = NSMenu()
        for (label, secs) in [("10 Seconds", 10.0), ("30 Seconds", 30.0), ("1 Minute", 60.0), ("5 Minutes", 300.0)] {
            let it = NSMenuItem(title: label, action: #selector(setInterval(_:)), keyEquivalent: "")
            it.representedObject = secs
            it.target = self
            it.state = abs(interval - secs) < 0.5 ? .on : .off
            sub.addItem(it)
        }
        every.submenu = sub
        menu.addItem(every)
        let login = NSMenuItem(title: "Start at Login", action: #selector(toggleLogin), keyEquivalent: "")
        login.target = self
        login.state = SMAppService.mainApp.status == .enabled ? .on : .off
        menu.addItem(login)
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quit Burrow Companion", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        return menu
    }

    @objc func setInterval(_ sender: NSMenuItem) {
        UserDefaults.standard.set(sender.representedObject as? Double ?? 30, forKey: "refreshInterval")
        schedule()
        refresh()
    }

    @objc func toggleLogin() {
        do {
            if SMAppService.mainApp.status == .enabled { try SMAppService.mainApp.unregister() }
            else { try SMAppService.mainApp.register() }
        } catch {
            let alert = NSAlert()
            alert.messageText = "Couldn't change Start at Login"
            alert.informativeText = error.localizedDescription + "\n\nYou can also add Burrow Companion in System Settings → General → Login Items."
            alert.runModal()
        }
        refresh()
    }

    @objc func openReleases() { NSWorkspace.shared.open(releasesURL) }

    @objc func go(_ sender: NSMenuItem) {
        guard let target = sender.representedObject as? String else { return }
        if target.hasPrefix("window:") { openWindow(String(target.dropFirst("window:".count))); return }
        var c = URLComponents()
        c.scheme = "alfred"; c.host = "runtrigger"; c.path = "/\(workflowBundleID)/open/"
        c.queryItems = [URLQueryItem(name: "argument", value: target)]
        if let url = c.url { NSWorkspace.shared.open(url) }
    }
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    var window: NSWindow?
    var menuBar: MenuBar!

    func applicationWillFinishLaunching(_ n: Notification) {
        NSAppleEventManager.shared().setEventHandler(self, andSelector: #selector(handleURL(_:reply:)),
                                                     forEventClass: AEEventClass(kInternetEventClass), andEventID: AEEventID(kAEGetURL))
    }

    func applicationDidFinishLaunching(_ n: Notification) {
        buildMainMenu()
        menuBar = MenuBar(openWindow: { [weak self] section in self?.showWindow(section) })
        if CommandLine.arguments.count > 1, ["updates", "browsers", "uninstall"].contains(CommandLine.arguments[1]) {
            showWindow(CommandLine.arguments[1])
        }
    }

    @objc func handleURL(_ event: NSAppleEventDescriptor, reply: NSAppleEventDescriptor) {
        guard let s = event.paramDescriptor(forKeyword: keyDirectObject)?.stringValue, let url = URL(string: s) else { return }
        let section = url.host ?? "updates"
        if section == "menubar" { return }  // just launching shows the menu bar icon
        showWindow(section)
    }

    func showWindow(_ section: String) {
        startSection = section
        RootUI.shared?.section = section
        if window == nil {
            let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 960, height: 620),
                             styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
            w.title = "Burrow"
            w.isReleasedWhenClosed = false
            w.delegate = self
            w.contentView = NSHostingView(rootView: RootView())
            w.center()
            w.setFrameAutosaveName("BurrowWindow")
            window = w
        }
        NSApp.setActivationPolicy(.regular)  // a Dock icon and menus while the window is open
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        if let out = ProcessInfo.processInfo.environment["BURROW_SNAPSHOT"] {  // development aid
            DispatchQueue.main.asyncAfter(deadline: .now() + 8) {
                guard let view = self.window?.contentView, let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { exit(1) }
                view.cacheDisplay(in: view.bounds, to: rep)
                try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: out))
                exit(0)
            }
        }
    }

    func windowWillClose(_ n: Notification) {
        NSApp.setActivationPolicy(.accessory)  // back to a menu bar-only app
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows: Bool) -> Bool {
        showWindow(startSection)
        return true
    }
}

func buildMainMenu() {
    let main = NSMenu()
    let appItem = NSMenuItem(); main.addItem(appItem)
    let appMenu = NSMenu()
    appMenu.addItem(withTitle: "Quit Burrow Companion", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
    appItem.submenu = appMenu
    let fileItem = NSMenuItem(); main.addItem(fileItem)
    let fileMenu = NSMenu(title: "File")
    fileMenu.addItem(withTitle: "Close Window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
    fileItem.submenu = fileMenu
    let editItem = NSMenuItem(); main.addItem(editItem)
    let edit = NSMenu(title: "Edit")
    edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
    edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
    edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
    edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
    editItem.submenu = edit
    NSApp.mainMenu = main
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
