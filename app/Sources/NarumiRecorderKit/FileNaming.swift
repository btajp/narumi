import Foundation

/// Track identifiers. The order of `CaseIterable` is the order used in every event.
public enum TrackKind: String, Codable, Sendable, CaseIterable {
    case screen
    case mic
    case system
}

/// Fixed file names inside the output directory (`<bundle>/tracks/`).
public enum TrackFileNames {
    public static let screen = "screen.mp4"
    public static let mic = "mic.m4a"
    public static let system = "system.m4a"
    public static let recorderJSON = "recorder.json"

    public static func fileName(for kind: TrackKind) -> String {
        switch kind {
        case .screen: return screen
        case .mic: return mic
        case .system: return system
        }
    }

    /// Tracks that a recording produces, in protocol order. `screen` is omitted with `--no-video`.
    public static func tracks(includeVideo: Bool) -> [TrackKind] {
        includeVideo ? [.screen, .mic, .system] : [.mic, .system]
    }

    public static func url(for kind: TrackKind, in outputDir: URL) -> URL {
        outputDir.appendingPathComponent(fileName(for: kind), isDirectory: false)
    }

    public static func recorderJSONURL(in outputDir: URL) -> URL {
        outputDir.appendingPathComponent(recorderJSON, isDirectory: false)
    }
}
