import Foundation

/// ISO 8601 UTC timestamps with second precision, e.g. `2026-08-27T03:05:00Z`.
public enum Timestamps {
    nonisolated(unsafe) private static let formatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        formatter.timeZone = TimeZone(identifier: "UTC")
        return formatter
    }()

    private static let lock = NSLock()

    public static func iso8601(_ date: Date) -> String {
        lock.lock()
        defer { lock.unlock() }
        return formatter.string(from: date)
    }
}
