// Renders tinted SF Symbols to PNGs for the Alfred workflow.
// Usage: swift tools/render_icons.swift <output-dir>
import AppKit

let colors: [String: NSColor] = [
    "green": NSColor(srgbRed: 0.20, green: 0.78, blue: 0.35, alpha: 1),
    "yellow": NSColor(srgbRed: 1.00, green: 0.78, blue: 0.00, alpha: 1),
    "orange": NSColor(srgbRed: 1.00, green: 0.58, blue: 0.00, alpha: 1),
    "red": NSColor(srgbRed: 1.00, green: 0.23, blue: 0.19, alpha: 1),
    "blue": NSColor(srgbRed: 0.04, green: 0.52, blue: 1.00, alpha: 1),
    "purple": NSColor(srgbRed: 0.69, green: 0.32, blue: 0.87, alpha: 1),
    "teal": NSColor(srgbRed: 0.19, green: 0.69, blue: 0.78, alpha: 1),
    "gray": NSColor(srgbRed: 0.56, green: 0.56, blue: 0.58, alpha: 1),
]

let usageColors = ["green", "yellow", "orange", "red"]

// name -> (symbol, colors). One PNG per color: "<name>-<color>.png"
let specs: [(String, String, [String])] = [
    ("health", "heart.fill", usageColors),
    ("cpu", "cpu", usageColors),
    ("memory", "memorychip", usageColors),
    ("disk", "internaldrive", usageColors),
    ("gpu", "square.stack.3d.up.fill", usageColors),
    ("battery", "battery.100percent", ["green", "orange", "red"]),
    ("battery-charging", "battery.100percent.bolt", ["green", "orange", "red"]),
    ("touchid", "touchid", ["green", "red"]),
    ("size", "circle.fill", ["red", "orange", "yellow", "gray"]),
    ("network", "network", ["blue"]),
    ("thermal", "bolt.fill", ["orange"]),
    ("temp", "thermometer.medium", usageColors),
    ("menubar", "menubar.rectangle", ["blue"]),
    ("large", "doc.viewfinder.fill", ["orange"]),
    ("dupes", "doc.on.doc.fill", ["purple"]),
    ("startup", "power.circle.fill", ["blue"]),
    ("undo", "arrow.uturn.backward.circle.fill", ["blue"]),
    ("process", "gearshape.fill", ["gray"]),
    ("bluetooth", "dot.radiowaves.left.and.right", ["blue"]),
    ("info", "info.circle.fill", ["gray"]),
    ("trash", "trash.fill", ["red"]),
    ("clean", "sparkles", ["blue"]),
    ("optimize", "gauge.with.dots.needle.67percent", ["blue"]),
    ("uninstall", "xmark.bin.fill", ["red"]),
    ("purge", "shippingbox.fill", ["orange"]),
    ("analyze", "chart.pie.fill", ["purple"]),
    ("installer", "opticaldiscdrive.fill", ["teal"]),
    ("update", "arrow.triangle.2.circlepath", ["green"]),
    ("status", "waveform.path.ecg", ["green"]),
    ("refresh", "arrow.clockwise", ["gray"]),
    ("check", "checkmark.circle.fill", ["green"]),
    ("item", "checkmark", ["blue"]),
    ("warning", "exclamationmark.triangle.fill", ["orange"]),
    ("error", "xmark.octagon.fill", ["red"]),
    ("search", "magnifyingglass", ["blue"]),
    ("doc", "doc.fill", ["gray"]),
    ("terminal", "terminal.fill", ["gray"]),
    ("hidden", "eye.slash", ["gray"]),
    ("back", "arrow.up.left", ["gray"]),
    ("download", "arrow.down.circle.fill", ["blue"]),
    ("github", "link", ["gray"]),
]

let outDir = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "icons"
try? FileManager.default.createDirectory(atPath: outDir, withIntermediateDirectories: true)

let canvas: CGFloat = 128
let inset: CGFloat = 14

func render(symbol: String, color: NSColor, to path: String) {
    guard let base = NSImage(systemSymbolName: symbol, accessibilityDescription: nil) else {
        FileHandle.standardError.write("missing symbol: \(symbol)\n".data(using: .utf8)!)
        exit(1)
    }
    let config = NSImage.SymbolConfiguration(pointSize: 96, weight: .regular)
        .applying(NSImage.SymbolConfiguration(paletteColors: [color]))
    let image = base.withSymbolConfiguration(config)!

    let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: Int(canvas), pixelsHigh: Int(canvas),
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)

    let box = canvas - inset * 2
    let scale = min(box / image.size.width, box / image.size.height)
    let w = image.size.width * scale
    let h = image.size.height * scale
    image.draw(in: NSRect(x: (canvas - w) / 2, y: (canvas - h) / 2, width: w, height: h))

    NSGraphicsContext.restoreGraphicsState()
    try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: path))
}

for (name, symbol, names) in specs {
    for colorName in names {
        let file = names.count == 1 ? "\(name).png" : "\(name)-\(colorName).png"
        render(symbol: symbol, color: colors[colorName]!, to: "\(outDir)/\(file)")
    }
}
print("rendered icons to \(outDir)")
