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

    static func symbol(_ name: String, _ color: NSColor) -> NSImage? {
        NSImage(systemSymbolName: name, accessibilityDescription: nil)?
            .withSymbolConfiguration(NSImage.SymbolConfiguration(paletteColors: [color]))
    }
}
