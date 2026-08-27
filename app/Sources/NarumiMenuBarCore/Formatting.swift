import Foundation

/// Pure formatting for the main window (Foundation-only so `swift test` covers it).
public enum NarumiFormat {
    /// "12:34" / "1:02:34" from seconds (floored; negative clamps to 0:00).
    public static func duration(_ seconds: Double) -> String {
        let total = max(0, Int(seconds))
        let hours = total / 3600
        let minutes = (total % 3600) / 60
        let secs = total % 60
        if hours > 0 {
            return String(format: "%d:%02d:%02d", hours, minutes, secs)
        }
        return String(format: "%d:%02d", minutes, secs)
    }

    /// Segment timecode: "0:04" / "12:34" / "1:02:34" (same as `duration`).
    public static func timecode(_ seconds: Double) -> String {
        duration(seconds)
    }

    // ISO8601DateFormatter / DateFormatter are not Sendable, so (Swift 6) they cannot live in
    // static stored properties. Fresh locals per call keep this enum concurrency-safe; the
    // formatters are cheap relative to the 5 s UI refresh that drives them.
    private static func makeISOParser(fractional: Bool) -> ISO8601DateFormatter {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = fractional
            ? [.withInternetDateTime, .withFractionalSeconds]
            : [.withInternetDateTime]
        return formatter
    }

    private static func makeJSTFormatter() -> DateFormatter {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "Asia/Tokyo")
        formatter.dateFormat = "yyyy-MM-dd HH:mm"
        return formatter
    }

    /// Contract timestamps are ISO 8601 UTC, with or without fractional seconds.
    public static func parseTimestamp(_ iso: String) -> Date? {
        makeISOParser(fractional: false).date(from: iso)
            ?? makeISOParser(fractional: true).date(from: iso)
    }

    /// "2026-08-27T03:05:00Z" → "2026-08-27 12:05" (JST). Unparseable input comes back verbatim
    /// so the UI still shows something.
    public static func jstDateTime(_ iso: String) -> String {
        guard let date = parseTimestamp(iso) else {
            return iso
        }
        return makeJSTFormatter().string(from: date)
    }

    /// Meeting status → Japanese label.
    public static func meetingStatusLabel(_ status: String) -> String {
        switch status {
        case "recording": return "録画中"
        case "recorded": return "録画済み"
        case "processing": return "処理中"
        case "ready": return "完了"
        case "failed": return "失敗"
        default: return status
        }
    }

    /// Job kind → Japanese label.
    public static func jobKindLabel(_ kind: String) -> String {
        switch kind {
        case "process": return "処理"
        case "regenerate": return "再生成"
        case "export": return "エクスポート"
        default: return kind
        }
    }

    /// Job status → Japanese label.
    public static func jobStatusLabel(_ status: String) -> String {
        switch status {
        case "queued": return "待機中"
        case "running": return "実行中"
        case "succeeded": return "完了"
        case "failed": return "失敗"
        case "cancelled": return "キャンセル"
        default: return status
        }
    }

    /// "処理 実行中 (transcribe 40%)" — the one-line job text used in rows and the jobs list.
    public static func jobText(kind: String, status: String, progress: JobProgress?) -> String {
        var text = "\(jobKindLabel(kind)) \(jobStatusLabel(status))"
        var parts: [String] = []
        if let stage = progress?.stage, !stage.isEmpty {
            parts.append(stage)
        }
        if let fraction = progress?.fraction {
            let clamped = min(max(fraction, 0), 1)
            parts.append("\(Int((clamped * 100).rounded()))%")
        }
        if !parts.isEmpty {
            text += " (\(parts.joined(separator: " ")))"
        }
        return text
    }

    /// Free-text scope field → scope selector values: whitespace / comma separated, empties
    /// dropped, order kept, duplicates removed (the contract requires uniqueItems).
    public static func parseScopeInput(_ text: String) -> [String] {
        var seen = Set<String>()
        return text.split(whereSeparator: { $0 == "," || $0.isWhitespace })
            .map(String.init)
            .filter { seen.insert($0).inserted }
    }
}

/// What one 会議一覧 row shows, derived from a `meeting_summary`.
public struct MeetingRowPresentation: Equatable, Sendable {
    public var title: String
    /// "2026-08-27 12:05 · 完了" (+ " · <scope>" when scoped).
    public var subtitle: String
    public var statusLabel: String
    /// One-line active-job text ("処理 実行中 (transcribe 40%)"), nil when no active job.
    public var jobText: String?

    public init(meeting: MeetingSummary) {
        title = meeting.meetingName
        statusLabel = NarumiFormat.meetingStatusLabel(meeting.status)
        var parts = [NarumiFormat.jstDateTime(meeting.startedAt), statusLabel]
        if let scope = meeting.scope, !scope.isEmpty {
            parts.append(scope)
        }
        subtitle = parts.joined(separator: " · ")
        if let job = meeting.activeJob, job.status == "queued" || job.status == "running" {
            jobText = NarumiFormat.jobText(kind: job.kind, status: job.status, progress: job.progress)
        } else {
            jobText = nil
        }
    }
}
