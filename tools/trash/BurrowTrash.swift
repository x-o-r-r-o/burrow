// Moves files to the Trash and reports where each one landed, so Burrow can undo.
// Usage: BurrowTrash PATH...   →   {"moved": {"/orig": "/Users/me/.Trash/orig"}, "failed": ["/path"]}
import Foundation

var moved: [String: String] = [:]
var failed: [String] = []
let fm = FileManager.default
for path in CommandLine.arguments.dropFirst() {
    var isLink = false
    if let attrs = try? fm.attributesOfItem(atPath: path), attrs[.type] as? FileAttributeType == .typeSymbolicLink { isLink = true }
    guard isLink || fm.fileExists(atPath: path) else { continue }
    var result: NSURL?
    do {
        try fm.trashItem(at: URL(fileURLWithPath: path), resultingItemURL: &result)
        if let landed = result?.path { moved[path] = landed } else { moved[path] = "" }
    } catch {
        failed.append(path)
    }
}
let data = try! JSONSerialization.data(withJSONObject: ["moved": moved, "failed": failed])
FileHandle.standardOutput.write(data)
