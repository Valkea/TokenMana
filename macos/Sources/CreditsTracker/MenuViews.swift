import AppKit

// MARK: - Formatting

enum Fmt {
    static func usageColor(_ pct: Double?) -> NSColor {
        guard let pct else { return .secondaryLabelColor }
        if pct >= 85 { return .systemRed }
        if pct >= 60 { return .systemOrange }
        return .systemGreen
    }

    static func gaugeSymbol(_ pct: Double?) -> String {
        guard let pct else { return "gauge.with.dots.needle.0percent" }
        switch pct {
        case ..<20: return "gauge.with.dots.needle.0percent"
        case ..<45: return "gauge.with.dots.needle.33percent"
        case ..<60: return "gauge.with.dots.needle.50percent"
        case ..<85: return "gauge.with.dots.needle.67percent"
        default: return "gauge.with.dots.needle.100percent"
        }
    }

    static func countdown(to date: Date, now: Date) -> String {
        let secs = Int(date.timeIntervalSince(now))
        if secs <= 0 { return "now" }
        let d = secs / 86400, h = (secs % 86400) / 3600, m = (secs % 3600) / 60
        if d > 0 { return "\(d)d \(h)h" }
        if h > 0 { return "\(h)h \(String(format: "%02d", m))m" }
        return "\(max(m, 1))m"
    }

    static func clock(_ date: Date, now: Date) -> String {
        let f = DateFormatter()
        if Calendar.current.isDate(date, inSameDayAs: now) {
            f.setLocalizedDateFormatFromTemplate("jm")
        } else if abs(date.timeIntervalSince(now)) < 6 * 86400 {
            f.setLocalizedDateFormatFromTemplate("EEEjm")
        } else {
            f.setLocalizedDateFormatFromTemplate("dMMM")
        }
        return f.string(from: date)
    }

    static func resetLine(_ m: Metric, now: Date) -> String? {
        if m.idle { return "Not started — ping to start the 5h window" }
        guard let date = m.resetDate else { return nil }
        return "Resets in \(countdown(to: date, now: now)) · \(clock(date, now: now))"
    }

    static func time(_ iso: String?) -> String? {
        guard let iso, let d = parseISO(iso) else { return nil }
        return clock(d, now: Date())
    }
}

// MARK: - Vendor badge (menu item image)

enum VendorImage {
    static func badge(_ vendor: String, size: CGFloat = 16) -> NSImage {
        let (symbol, color): (String, NSColor) = {
            switch vendor {
            case "anthropic": return ("sparkle", NSColor(red: 0.85, green: 0.47, blue: 0.34, alpha: 1))
            case "openai": return ("circle.hexagongrid.fill", NSColor(red: 0.06, green: 0.64, blue: 0.5, alpha: 1))
            case "github": return ("chevron.left.forwardslash.chevron.right", NSColor(red: 0.51, green: 0.31, blue: 0.86, alpha: 1))
            case "bedrock": return ("cloud.fill", NSColor(red: 0.95, green: 0.6, blue: 0.1, alpha: 1))
            case "command": return ("terminal.fill", .systemGray)
            default: return ("questionmark", .systemGray)
            }
        }()
        return NSImage(size: NSSize(width: size, height: size), flipped: false) { rect in
            color.setFill()
            NSBezierPath(roundedRect: rect, xRadius: size * 0.27, yRadius: size * 0.27).fill()
            let config = NSImage.SymbolConfiguration(pointSize: size * 0.5, weight: .bold)
                .applying(NSImage.SymbolConfiguration(paletteColors: [.white]))
            if let glyph = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)?.withSymbolConfiguration(config) {
                let s = glyph.size
                glyph.draw(in: NSRect(x: (rect.width - s.width) / 2, y: (rect.height - s.height) / 2,
                                      width: s.width, height: s.height))
            }
            return true
        }
    }
}

// MARK: - Menu item icons (standard NSMenuItem images, no custom views)

enum MenuIcons {
    /// Small usage ring, coloured by level (like the system's own status icons).
    static func ring(_ pct: Double?, size: CGFloat = 16) -> NSImage {
        NSImage(size: NSSize(width: size, height: size), flipped: false) { rect in
            let inset = rect.insetBy(dx: 1.75, dy: 1.75)
            let track = NSBezierPath(ovalIn: inset)
            track.lineWidth = 2.5
            NSColor.tertiaryLabelColor.withAlphaComponent(0.35).setStroke()
            track.stroke()
            if let p = pct, p > 0 {
                let arc = NSBezierPath()
                let center = NSPoint(x: rect.midX, y: rect.midY)
                arc.appendArc(withCenter: center, radius: inset.width / 2, startAngle: 90,
                              endAngle: 90 - 360 * min(p, 100) / 100, clockwise: true)
                arc.lineWidth = 2.5
                arc.lineCapStyle = .round
                Fmt.usageColor(p).setStroke()
                arc.stroke()
            }
            return true
        }
    }

    static let palette: [(name: String, color: NSColor)] = [
        ("blue", .systemBlue), ("green", .systemGreen), ("purple", .systemPurple), ("orange", .systemOrange),
        ("pink", .systemPink), ("teal", .systemTeal), ("red", .systemRed), ("gray", .systemGray)]

    static func color(named name: String?) -> NSColor? {
        palette.first { $0.name == name }?.color
    }

    /// The item's icon with a coloured group bar along its left edge (a standard menu-item image).
    /// Every top-level item uses the same width, so titles stay aligned with or without a bar.
    static func striped(_ icon: NSImage?, color: NSColor?, tall: Bool = false) -> NSImage {
        let size = NSSize(width: 27, height: tall ? 30 : 20)
        return NSImage(size: size, flipped: false) { rect in
            if let icon {
                let s = icon.size
                let scale = min(1, 16 / max(s.width, s.height))
                let w = s.width * scale, h = s.height * scale
                let r = NSRect(x: 10 + (16 - w) / 2, y: (rect.height - h) / 2, width: w, height: h)
                icon.draw(in: r)
                if icon.isTemplate {  // composited images lose template tinting: tint for the current appearance
                    NSColor.labelColor.set()
                    r.fill(using: .sourceAtop)
                }
            }
            if let color {
                color.setFill()
                NSBezierPath(roundedRect: NSRect(x: 0, y: 0, width: 4, height: rect.height), xRadius: 2, yRadius: 2).fill()
            }
            return true
        }
    }

    static func dot(_ color: NSColor) -> NSImage {
        NSImage(size: NSSize(width: 12, height: 12), flipped: false) { rect in
            color.setFill()
            NSBezierPath(ovalIn: rect.insetBy(dx: 1, dy: 1)).fill()
            return true
        }
    }

    static func symbol(_ name: String, _ color: NSColor) -> NSImage? {
        NSImage(systemSymbolName: name, accessibilityDescription: nil)?
            .withSymbolConfiguration(NSImage.SymbolConfiguration(paletteColors: [color]))
    }
}

// MARK: - Metric row (attributed title with content-sized, tab-aligned columns)

/// Label · value (right-aligned) · bar · reset — column positions measured from all rows in the menu.
struct MetricLayout: Equatable {
    var valueRight: CGFloat
    var barLeft: CGFloat
    var resetLeft: CGFloat

    static let barWidth: CGFloat = 70
    static let gap: CGFloat = 12

    static var font: NSFont { NSFont.menuFont(ofSize: 0) }
    static var valueFont: NSFont { NSFont.monospacedDigitSystemFont(ofSize: font.pointSize, weight: .semibold) }

    static func measure(_ metrics: [Metric]) -> MetricLayout {
        let labelW = metrics.map { ($0.label as NSString).size(withAttributes: [.font: font]).width }.max() ?? 80
        let valueW = metrics.map { (MetricRow.value($0) as NSString).size(withAttributes: [.font: valueFont]).width }.max() ?? 40
        let valueRight = ceil(labelW + gap + valueW)
        let barLeft = valueRight + gap
        return MetricLayout(valueRight: valueRight, barLeft: barLeft, resetLeft: barLeft + barWidth + gap)
    }
}

enum MetricRow {
    static func value(_ m: Metric) -> String { m.idle ? "idle" : m.text }

    static func icon(_ m: Metric) -> NSImage? {
        let name: String = switch m.key {
        case "session": "clock"
        case let k where k.hasPrefix("weekly"): "calendar"
        case "budget", "extra_usage": "dollarsign.circle"
        case "team": "person.2"
        case "premium_interactions": "sparkles"
        default: "chart.bar"
        }
        let img = NSImage(systemSymbolName: name, accessibilityDescription: nil)?
            .withSymbolConfiguration(.init(pointSize: 12, weight: .regular))
        img?.isTemplate = true
        return img
    }

    static func title(_ m: Metric, layout: MetricLayout, now: Date) -> NSAttributedString {
        let font = MetricLayout.font
        let para = NSMutableParagraphStyle()
        para.tabStops = [NSTextTab(textAlignment: .right, location: layout.valueRight),
                         NSTextTab(textAlignment: .left, location: layout.barLeft),
                         NSTextTab(textAlignment: .left, location: layout.resetLeft)]
        let pct = m.idle ? 0 : m.percent
        let out = NSMutableAttributedString(string: m.label, attributes: [.font: font, .foregroundColor: NSColor.labelColor])
        out.append(NSAttributedString(string: "\t" + value(m), attributes: [
            .font: MetricLayout.valueFont,
            .foregroundColor: m.idle ? NSColor.secondaryLabelColor : ((pct ?? 0) >= 85 ? NSColor.systemRed : NSColor.labelColor)]))
        out.append(NSAttributedString(string: "\t"))
        let bar = NSTextAttachment()
        bar.image = barImage(pct)
        bar.bounds = CGRect(x: 0, y: (font.capHeight - 7) / 2, width: MetricLayout.barWidth, height: 7)
        out.append(NSAttributedString(attachment: bar))
        out.append(NSAttributedString(string: "\t" + (shortReset(m, now: now) ?? ""), attributes: [
            .font: NSFont.systemFont(ofSize: font.pointSize - 1), .foregroundColor: NSColor.secondaryLabelColor]))
        out.addAttribute(.paragraphStyle, value: para, range: NSRange(location: 0, length: out.length))
        return out
    }

    /// "3h 11m · 05:00", "2d 1h · Mon 03:00", "Dec 1", "ping to start"
    static func shortReset(_ m: Metric, now: Date) -> String? {
        if m.idle { return "ping to start" }
        guard let date = m.resetDate else { return nil }
        if date.timeIntervalSince(now) > 6 * 86400 { return "resets \(Fmt.clock(date, now: now))" }
        return "\(Fmt.countdown(to: date, now: now)) · \(Fmt.clock(date, now: now))"
    }

    static func barImage(_ pct: Double?) -> NSImage {
        NSImage(size: NSSize(width: MetricLayout.barWidth, height: 7), flipped: false) { rect in
            NSColor.labelColor.withAlphaComponent(0.15).setFill()
            NSBezierPath(roundedRect: rect, xRadius: 3.5, yRadius: 3.5).fill()
            if let p = pct, p > 0 {
                var fill = rect
                fill.size.width = max(7, rect.width * min(p, 100) / 100)
                Fmt.usageColor(p).setFill()
                NSBezierPath(roundedRect: fill, xRadius: 3.5, yRadius: 3.5).fill()
            }
            return true
        }
    }
}
