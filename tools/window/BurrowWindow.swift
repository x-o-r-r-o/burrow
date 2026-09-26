// Burrow's window: App Updates and Browsers.
// Usage: BurrowWindow <workflow folder> [updates|browsers]
// All work is done by the workflow's Python (updates.py / browsers.py "cli" commands),
// so the window has exactly the same safety checks as Alfred.
import AppKit
import SwiftUI

let workflowDir = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : FileManager.default.currentDirectoryPath
let startSection = CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : "updates"

// MARK: - Talking to Python

/// Runs `python3 <script> cli <args…>` and delivers each JSON line on the main thread.
func runCLI(_ script: String, _ args: [String], onLine: @escaping ([String: Any]) -> Void, done: @escaping () -> Void = {}) {
    DispatchQueue.global(qos: .userInitiated).async {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        p.arguments = [workflowDir + "/" + script, "cli"] + args
        p.currentDirectoryURL = URL(fileURLWithPath: workflowDir)
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
    @Published var selfUpdate: String?
    @Published var mode = "notify"
    @Published var loading = false
    @Published var progress: [String: String] = [:]   // path -> "Downloading…" / "Updated" / error
    @Published var finished: Set<String> = []
    @Published var message: String?
    @Published var hasMas = true
    @Published var hasBrew = false
    @Published var installingMas = false
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
            self.selfUpdate = (o["self"] as? [String: Any])?["version"] as? String
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

    func installMas() {
        installingMas = true
        runCLI("updates.py", ["install-mas"], onLine: { o in self.message = (o["done"] ?? o["error"]) as? String },
               done: { self.installingMas = false; self.load() })
    }

    func selfUpdateNow() {
        runCLI("updates.py", ["self-update"], onLine: { o in self.message = (o["done"] ?? o["error"]) as? String })
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
            if let v = model.selfUpdate {
                HStack {
                    Image(systemName: "sparkles")
                    Text("Burrow \(v) is available")
                    Spacer()
                    Button("Update Burrow") { model.selfUpdateNow() }
                }.padding(8).background(Color.accentColor.opacity(0.1))
            }
            if !model.hasMas && model.updates.contains(where: { $0.source == "App Store" && !$0.installable }) {
                HStack {
                    Image(systemName: "bag")
                    Text(model.hasBrew ? "Update App Store apps here too: Burrow installs the free mas tool with Homebrew."
                                       : "App Store apps update in the App Store. Install Homebrew (brew.sh) to update them here.")
                    Spacer()
                    if model.installingMas { ProgressView().controlSize(.small) }
                    if model.hasBrew { Button("Install mas") { model.installMas() }.disabled(model.installingMas) }
                }.padding(8).background(Color.secondary.opacity(0.08))
            }
            Divider()
            HSplitView {
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
                .searchable(text: $ui.filter, placement: .sidebar, prompt: "Filter apps")
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
            Text("· change in Alfred's workflow settings").foregroundStyle(.secondary)
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
            else { self.message = "\(b.name) cleaned · \(formatBytes(o["freed"] as? Double ?? 0)) moved to the Trash · undo in Alfred with bu" }
        }, done: { self.busy = nil; self.load() })
    }

    func reset(_ b: BrowserInfo, full: Bool) {
        busy = b.id
        runCLI("browsers.py", ["reset", b.id, full ? "full" : "settings", "", profilesArg(b)], onLine: { o in
            self.message = (o["error"] as? String).map { "Couldn't reset \(b.name): \($0)" } ?? "\(b.name) \(full ? "fully reset" : "settings reset") · undo in Alfred with bu"
        }, done: { self.busy = nil; self.load() })
    }
}

final class BrowsersUI: ObservableObject {
    @Published var selection: String?
    @Published var confirm: String?   // "clean", "passwords", "reset", "full"
}

struct BrowsersView: View {
    @ObservedObject var model: BrowsersModel
    @StateObject private var ui = BrowsersUI()

    var body: some View {
        VStack(spacing: 0) {
            HSplitView {
                List(selection: $ui.selection) {
                    ForEach(model.browsers) { b in
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
                    Text("Give Alfred Full Disk Access to clean Safari (and BurrowWindow too if you opened this window from the menu bar).")
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
            return "\(which): bookmarks, history, passwords, extensions, cookies and settings all go to the Trash. You can undo this in Alfred with bu."
        case "reset": return b.kind == "chromium"
            ? "Settings go back to their defaults. Extensions and their stored data (for example a wallet or password-manager extension's local vault) move to the Trash; Undo brings them back. Bookmarks, history and passwords stay."
            : "Settings go back to their defaults. Extensions, bookmarks, history and passwords stay."
        default: return "Everything goes to the Trash first, so you can undo it in Alfred with bu."
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
    @StateObject var ui = RootUI()

    var body: some View {
        HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 4) {
                sidebarButton("updates", "App updates", "arrow.down.circle", updates.updates.count)
                sidebarButton("browsers", "Browsers", "globe", 0)
                Spacer()
            }
            .padding(10)
            .frame(width: 180)
            .background(Color(nsColor: .windowBackgroundColor))
            Divider()
            Group {
                if ui.section == "browsers" { BrowsersView(model: browsers) } else { UpdatesView(model: updates) }
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

final class AppDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!

    func applicationDidFinishLaunching(_ n: Notification) {
        // One window at a time: bring an already-running copy forward instead.
        let me = ProcessInfo.processInfo.processIdentifier
        if let other = NSWorkspace.shared.runningApplications.first(where: {
            $0.executableURL == Bundle.main.executableURL && $0.processIdentifier != me
        }) {
            DistributedNotificationCenter.default().postNotificationName(
                Notification.Name("io.github.burrow-alfred.show"), object: startSection, userInfo: nil, deliverImmediately: true)
            other.activate(options: [.activateIgnoringOtherApps])
            NSApp.terminate(nil)
            return
        }
        DistributedNotificationCenter.default().addObserver(forName: Notification.Name("io.github.burrow-alfred.show"), object: nil, queue: .main) { n in
            if let section = n.object as? String { RootUI.shared?.section = section }
            self.window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        }
        if let icon = NSImage(contentsOfFile: workflowDir + "/icon.png") { NSApp.applicationIconImage = icon }
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 960, height: 620),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Burrow"
        window.contentView = NSHostingView(rootView: RootView())
        window.center()
        window.setFrameAutosaveName("BurrowWindow")
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        // Development aid: BURROW_SNAPSHOT=/path.png saves a picture of the window and quits.
        if let out = ProcessInfo.processInfo.environment["BURROW_SNAPSHOT"] {
            DispatchQueue.main.asyncAfter(deadline: .now() + 8) {
                guard let view = self.window.contentView, let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { exit(1) }
                view.cacheDisplay(in: view.bounds, to: rep)
                try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: out))
                exit(0)
            }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }
}

func buildMainMenu() {
    let main = NSMenu()
    let appItem = NSMenuItem(); main.addItem(appItem)
    let appMenu = NSMenu()
    appMenu.addItem(withTitle: "Quit Burrow", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
    appItem.submenu = appMenu
    let fileItem = NSMenuItem(); main.addItem(fileItem)
    let fileMenu = NSMenu(title: "File")
    fileMenu.addItem(withTitle: "Close Window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
    fileItem.submenu = fileMenu
    let editItem = NSMenuItem(); main.addItem(editItem)
    let edit = NSMenu(title: "Edit")
    edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
    edit.addItem(.separator())
    edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
    edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
    edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
    edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
    editItem.submenu = edit
    NSApp.mainMenu = main
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
buildMainMenu()
let delegate = AppDelegate()
app.delegate = delegate
app.run()
