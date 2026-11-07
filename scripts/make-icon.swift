


import AppKit


let half: [(CGFloat, CGFloat)] = [
    (0.000, 0.000), (0.035, 0.080), (0.070, 0.190), (0.110, 0.300), (0.150, 0.400),
    (0.165, 0.450), (0.480, 0.660), (0.480, 0.735), (0.215, 0.755),
    (0.190, 0.800), (0.370, 0.905), (0.370, 0.975), (0.130, 0.965),
    (0.085, 1.010), (0.000, 1.030),
]

func planform(in r: NSRect) -> NSBezierPath {
    let p = NSBezierPath()
    func pt(_ x: CGFloat, _ y: CGFloat) -> NSPoint { NSPoint(x: r.midX + x * r.width, y: r.maxY - y * r.height / 1.03) }
    p.move(to: pt(half[0].0, half[0].1))
    for (x, y) in half.dropFirst() { p.line(to: pt(x, y)) }
    for (x, y) in half.reversed().dropFirst() { p.line(to: pt(-x, y)) }
    p.close()
    return p
}

let out = CommandLine.arguments[1]
try? FileManager.default.createDirectory(atPath: out, withIntermediateDirectories: true)
for (size, scale) in [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2), (512, 1), (512, 2)] {
    let px = CGFloat(size * scale)
    let img = NSImage(size: NSSize(width: px, height: px))
    img.lockFocus()
    let inset = px * 0.09
    let rect = NSRect(x: inset, y: inset, width: px - 2 * inset, height: px - 2 * inset)
    let squircle = NSBezierPath(roundedRect: rect, xRadius: rect.width * 0.23, yRadius: rect.width * 0.23)
    NSGradient(colors: [NSColor(red: 0.20, green: 0.25, blue: 0.31, alpha: 1), NSColor(red: 0.08, green: 0.10, blue: 0.13, alpha: 1)])!
        .draw(in: squircle, angle: -90)

    NSGraphicsContext.saveGraphicsState()
    let t = NSAffineTransform()
    t.translateX(by: px / 2, yBy: px / 2)
    t.rotate(byDegrees: -40)
    t.translateX(by: -px / 2, yBy: -px / 2)
    t.concat()
    let jetH = rect.height * 0.80, jetW = jetH * 0.66
    let jr = NSRect(x: px / 2 - jetW / 2, y: px / 2 - jetH / 2, width: jetW, height: jetH)
    let body = planform(in: jr)

    let shadow = NSShadow()
    shadow.shadowColor = NSColor.black.withAlphaComponent(0.45)
    shadow.shadowBlurRadius = px * 0.03
    shadow.shadowOffset = NSSize(width: 0, height: -px * 0.012)
    shadow.set()
    NSGradient(colors: [NSColor(red: 0.56, green: 0.60, blue: 0.64, alpha: 1), NSColor(red: 0.33, green: 0.36, blue: 0.40, alpha: 1)])!
        .draw(in: body, angle: -60)
    NSShadow().set()

    if px >= 64 {
        NSColor.white.withAlphaComponent(0.18).setStroke()
        body.lineWidth = max(0.5, px * 0.004)
        body.stroke()
        let spine = NSBezierPath()
        spine.move(to: NSPoint(x: jr.midX, y: jr.maxY - jr.height * 0.42))
        spine.line(to: NSPoint(x: jr.midX, y: jr.maxY - jr.height * 0.95))
        NSColor.black.withAlphaComponent(0.18).setStroke()
        spine.lineWidth = max(0.5, px * 0.004)
        spine.stroke()
    }

    let cw = jr.width * 0.085, ch = jr.height * 0.17
    let canopy = NSBezierPath(ovalIn: NSRect(x: jr.midX - cw / 2, y: jr.maxY - jr.height * 0.36, width: cw, height: ch))
    NSGradient(colors: [NSColor(red: 1.0, green: 0.78, blue: 0.36, alpha: 1), NSColor(red: 0.86, green: 0.45, blue: 0.10, alpha: 1)])!
        .draw(in: canopy, angle: -90)
    NSGraphicsContext.restoreGraphicsState()

    img.unlockFocus()
    let rep = NSBitmapImageRep(data: img.tiffRepresentation!)!
    let name = scale == 1 ? "icon_\(size)x\(size).png" : "icon_\(size)x\(size)@2x.png"
    try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: "\(out)/\(name)"))
}
