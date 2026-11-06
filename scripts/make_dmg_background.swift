// Renders the DMG window background: an arrow from the app icon to the
// Applications shortcut, and an instruction line. AppKit only, so building a
// release needs no image tools.
//
//   swiftc -O scripts/make_dmg_background.swift -o /tmp/make_dmg_background
//   /tmp/make_dmg_background overlay/build
//
// Writes dmg-background.png (560x380) and dmg-background@2x.png (1120x760).
// The geometry must match build.dmg in overlay/package.json: icons centred at
// (150,190) and (410,190), measured from the top-left, 96 pt icons.

import AppKit

let width: CGFloat = 560
let height: CGFloat = 380
let iconY: CGFloat = 190          // from the top, as electron-builder measures it
let appX: CGFloat = 150
let applicationsX: CGFloat = 410
let iconSize: CGFloat = 96

func render(scale: CGFloat) -> Data {
    let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil,
        pixelsWide: Int(width * scale), pixelsHigh: Int(height * scale),
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
    )!
    rep.size = NSSize(width: width, height: height)

    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    defer { NSGraphicsContext.restoreGraphicsState() }

    // Light background: Finder draws icon labels in dark text regardless of
    // the window background, so a dark image would make them unreadable.
    let gradient = NSGradient(
        starting: NSColor(srgbRed: 0.98, green: 0.98, blue: 0.99, alpha: 1),
        ending: NSColor(srgbRed: 0.93, green: 0.94, blue: 0.96, alpha: 1)
    )!
    gradient.draw(in: NSRect(x: 0, y: 0, width: width, height: height), angle: -90)

    // AppKit's origin is bottom-left; the layout is specified from the top.
    let y = height - iconY
    let gap: CGFloat = iconSize / 2 + 22
    let start = NSPoint(x: appX + gap, y: y)
    let end = NSPoint(x: applicationsX - gap, y: y)
    let accent = NSColor(srgbRed: 0.31, green: 0.51, blue: 0.96, alpha: 1)

    let shaft = NSBezierPath()
    shaft.move(to: start)
    shaft.line(to: NSPoint(x: end.x - 20, y: end.y))
    shaft.lineWidth = 5
    shaft.lineCapStyle = .round
    accent.setStroke()
    shaft.stroke()

    let head = NSBezierPath()
    head.move(to: NSPoint(x: end.x, y: end.y))
    head.line(to: NSPoint(x: end.x - 22, y: end.y + 14))
    head.line(to: NSPoint(x: end.x - 22, y: end.y - 14))
    head.close()
    accent.setFill()
    head.fill()

    let paragraph = NSMutableParagraphStyle()
    paragraph.alignment = .center
    let title = NSAttributedString(string: "Drag Clueless to Applications", attributes: [
        .font: NSFont.systemFont(ofSize: 20, weight: .semibold),
        .foregroundColor: NSColor(srgbRed: 0.11, green: 0.11, blue: 0.13, alpha: 1),
        .paragraphStyle: paragraph,
    ])
    title.draw(in: NSRect(x: 0, y: height - 74, width: width, height: 30))

    let note = NSAttributedString(
        string: "Then open it from Applications. The first launch walks you through setup.",
        attributes: [
            .font: NSFont.systemFont(ofSize: 12),
            .foregroundColor: NSColor(srgbRed: 0.42, green: 0.43, blue: 0.47, alpha: 1),
            .paragraphStyle: paragraph,
        ])
    note.draw(in: NSRect(x: 0, y: 34, width: width, height: 20))

    return rep.representation(using: .png, properties: [:])!
}

let args = CommandLine.arguments
guard args.count == 2 else {
    FileHandle.standardError.write(Data("usage: make_dmg_background <output-dir>\n".utf8))
    exit(64)
}
let dir = URL(fileURLWithPath: args[1], isDirectory: true)
try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
for (scale, name) in [(CGFloat(1), "dmg-background.png"), (CGFloat(2), "dmg-background@2x.png")] {
    let url = dir.appendingPathComponent(name)
    try render(scale: scale).write(to: url)
    print("wrote \(url.path)")
}
