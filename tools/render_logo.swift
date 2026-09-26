// Draws the Burrow logo: a squircle with sky, layered earth, a tunnel opening
// and a sparkle. Usage: swift tools/render_logo.swift <out.png> [size]
import AppKit

let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "icon.png"
let size = CGFloat(Double(CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : "1024") ?? 1024)

func rgb(_ hex: UInt32, _ a: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255, blue: CGFloat(hex & 0xFF) / 255, alpha: a)
}

let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size), pixelsHigh: Int(size), bitsPerSample: 8,
                           samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
let s = size / 1024  // design on a 1024 grid

// macOS-style icon: 824pt squircle centred on a 1024 canvas.
let tile = NSRect(x: 100 * s, y: 100 * s, width: 824 * s, height: 824 * s)
let squircle = NSBezierPath(roundedRect: tile, xRadius: 185 * s, yRadius: 185 * s)

// Soft drop shadow under the tile.
NSGraphicsContext.saveGraphicsState()
let shadow = NSShadow()
shadow.shadowColor = rgb(0x000000, 0.35)
shadow.shadowBlurRadius = 28 * s
shadow.shadowOffset = NSSize(width: 0, height: -12 * s)
shadow.set()
rgb(0x1B2A4A).setFill()
squircle.fill()
NSGraphicsContext.restoreGraphicsState()

NSGraphicsContext.saveGraphicsState()
squircle.addClip()

// Sky: deep night blue to teal.
NSGradient(colors: [rgb(0x2E8FA8), rgb(0x1B3F6B), rgb(0x16264A)], atLocations: [0, 0.55, 1], colorSpace: .sRGB)!
    .draw(in: tile, angle: 90)

// Earth layers, back to front, each a gentle hill.
func hill(baseY: CGFloat, peakY: CGFloat, peakX: CGFloat, top: NSColor, bottom: NSColor) {
    let p = NSBezierPath()
    p.move(to: NSPoint(x: tile.minX - 10 * s, y: baseY * s))
    p.curve(to: NSPoint(x: tile.maxX + 10 * s, y: (baseY - 30) * s),
            controlPoint1: NSPoint(x: (peakX - 180) * s, y: peakY * s),
            controlPoint2: NSPoint(x: (peakX + 200) * s, y: peakY * s))
    p.line(to: NSPoint(x: tile.maxX + 10 * s, y: tile.minY - 10 * s))
    p.line(to: NSPoint(x: tile.minX - 10 * s, y: tile.minY - 10 * s))
    p.close()
    NSGradient(starting: top, ending: bottom)!.draw(in: p, angle: -90)
}
hill(baseY: 470, peakY: 610, peakX: 640, top: rgb(0xB8743A), bottom: rgb(0x7A4A24))
hill(baseY: 400, peakY: 520, peakX: 420, top: rgb(0xD98B3F), bottom: rgb(0x8E5426))
hill(baseY: 300, peakY: 380, peakX: 560, top: rgb(0xA35E2C), bottom: rgb(0x5E3517))

// Tunnel opening in the front hill.
let hole = NSRect(x: 395 * s, y: 250 * s, width: 250 * s, height: 190 * s)
let holePath = NSBezierPath()
holePath.move(to: NSPoint(x: hole.minX, y: hole.minY))
holePath.curve(to: NSPoint(x: hole.maxX, y: hole.minY),
               controlPoint1: NSPoint(x: hole.minX + 10 * s, y: hole.maxY + 60 * s),
               controlPoint2: NSPoint(x: hole.maxX - 10 * s, y: hole.maxY + 60 * s))
holePath.close()
rgb(0xE8A657).setStroke()
holePath.lineWidth = 16 * s
holePath.stroke()
NSGradient(starting: rgb(0x1A0E06), ending: rgb(0x3A2110))!.draw(in: holePath, angle: 90)

// Dirt clods tossed out of the tunnel.
for (x, y, r) in [(330, 262, 18), (690, 270, 22), (730, 250, 12), (300, 240, 10)] as [(CGFloat, CGFloat, CGFloat)] {
    rgb(0xC9803D).setFill()
    NSBezierPath(ovalIn: NSRect(x: (x - r) * s, y: (y - r) * s, width: r * 2 * s, height: r * 2 * s)).fill()
}

// Four-point sparkle above the burrow: "clean".
func sparkle(cx: CGFloat, cy: CGFloat, r: CGFloat, color: NSColor) {
    let p = NSBezierPath()
    let w = r * 0.28
    p.move(to: NSPoint(x: cx * s, y: (cy + r) * s))
    p.curve(to: NSPoint(x: (cx + r) * s, y: cy * s), controlPoint1: NSPoint(x: (cx + w * 0.3) * s, y: (cy + w) * s), controlPoint2: NSPoint(x: (cx + w) * s, y: (cy + w * 0.3) * s))
    p.curve(to: NSPoint(x: cx * s, y: (cy - r) * s), controlPoint1: NSPoint(x: (cx + w) * s, y: (cy - w * 0.3) * s), controlPoint2: NSPoint(x: (cx + w * 0.3) * s, y: (cy - w) * s))
    p.curve(to: NSPoint(x: (cx - r) * s, y: cy * s), controlPoint1: NSPoint(x: (cx - w * 0.3) * s, y: (cy - w) * s), controlPoint2: NSPoint(x: (cx - w) * s, y: (cy - w * 0.3) * s))
    p.curve(to: NSPoint(x: cx * s, y: (cy + r) * s), controlPoint1: NSPoint(x: (cx - w) * s, y: (cy + w * 0.3) * s), controlPoint2: NSPoint(x: (cx - w * 0.3) * s, y: (cy + w) * s))
    p.close()
    NSGraphicsContext.saveGraphicsState()
    let glow = NSShadow()
    glow.shadowColor = color.withAlphaComponent(0.8)
    glow.shadowBlurRadius = r * 0.5 * s
    glow.set()
    color.setFill()
    p.fill()
    NSGraphicsContext.restoreGraphicsState()
}
sparkle(cx: 610, cy: 735, r: 120, color: rgb(0xFFF4C9))
sparkle(cx: 420, cy: 790, r: 52, color: rgb(0xFFE08A))
sparkle(cx: 760, cy: 620, r: 38, color: rgb(0xFFE08A))

// Subtle top highlight for depth.
NSGradient(colors: [rgb(0xFFFFFF, 0.18), rgb(0xFFFFFF, 0)], atLocations: [0, 1], colorSpace: .sRGB)!
    .draw(in: NSRect(x: tile.minX, y: tile.midY, width: tile.width, height: tile.height / 2), angle: -90)

NSGraphicsContext.restoreGraphicsState()

// Hairline edge.
rgb(0xFFFFFF, 0.12).setStroke()
squircle.lineWidth = 3 * s
squircle.stroke()

NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: out))
print("wrote \(out)")
