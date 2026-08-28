import Foundation

// MARK: - Event payloads
//
// Wire protocol (one JSON object per line on stdout, flushed after each line):
//
//   {"event":"started","started_at":"2026-08-27T03:05:00Z","tracks":{"screen":"screen.mp4","mic":"mic.m4a","system":"system.m4a"}}
//   {"event":"stopped","stopped_at":"...","duration_sec":123.4,"tracks":{"screen":{"path":"screen.mp4","bytes":1234,"duration_sec":123.4},"mic":{...},"system":{...}}}
//   {"event":"error","code":"permission_denied|no_display|capture_failed|writer_failed|invalid_argument","message":"..."}
//   {"event":"log","message":"..."}
//
// The structs below are `Codable` for round-tripping; the exact line format is produced by
// `RecorderEventLine.encode(_:)` which controls key order.

public struct StartedTracks: Codable, Equatable, Sendable {
    public var screen: String?
    public var mic: String
    public var system: String

    public init(screen: String?, mic: String, system: String) {
        self.screen = screen
        self.mic = mic
        self.system = system
    }

    /// Protocol file names for a recording with or without the screen track.
    public static func standard(includeVideo: Bool) -> StartedTracks {
        StartedTracks(
            screen: includeVideo ? TrackFileNames.screen : nil,
            mic: TrackFileNames.mic,
            system: TrackFileNames.system
        )
    }

    public func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encodeIfPresent(screen, forKey: .screen)
        try container.encode(mic, forKey: .mic)
        try container.encode(system, forKey: .system)
    }
}

public struct StartedEvent: Codable, Equatable, Sendable {
    public var startedAt: String
    public var tracks: StartedTracks

    public init(startedAt: String, tracks: StartedTracks) {
        self.startedAt = startedAt
        self.tracks = tracks
    }

    enum CodingKeys: String, CodingKey {
        case startedAt = "started_at"
        case tracks
    }
}

public struct TrackSummary: Codable, Equatable, Sendable {
    public var path: String
    public var bytes: Int
    public var durationSec: Double

    public init(path: String, bytes: Int, durationSec: Double) {
        self.path = path
        self.bytes = bytes
        self.durationSec = durationSec
    }

    enum CodingKeys: String, CodingKey {
        case path
        case bytes
        case durationSec = "duration_sec"
    }
}

public struct StoppedTracks: Codable, Equatable, Sendable {
    public var screen: TrackSummary?
    public var mic: TrackSummary
    public var system: TrackSummary

    public init(screen: TrackSummary?, mic: TrackSummary, system: TrackSummary) {
        self.screen = screen
        self.mic = mic
        self.system = system
    }

    public func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encodeIfPresent(screen, forKey: .screen)
        try container.encode(mic, forKey: .mic)
        try container.encode(system, forKey: .system)
    }
}

public struct StoppedEvent: Codable, Equatable, Sendable {
    public var stoppedAt: String
    public var durationSec: Double
    public var tracks: StoppedTracks

    public init(stoppedAt: String, durationSec: Double, tracks: StoppedTracks) {
        self.stoppedAt = stoppedAt
        self.durationSec = durationSec
        self.tracks = tracks
    }

    enum CodingKeys: String, CodingKey {
        case stoppedAt = "stopped_at"
        case durationSec = "duration_sec"
        case tracks
    }
}

public struct ErrorEvent: Codable, Equatable, Sendable {
    public var code: RecorderErrorCode
    public var message: String

    public init(code: RecorderErrorCode, message: String) {
        self.code = code
        self.message = message
    }

    public init(_ error: RecorderError) {
        self.init(code: error.code, message: error.message)
    }
}

public struct LogEvent: Codable, Equatable, Sendable {
    public var message: String

    public init(message: String) {
        self.message = message
    }
}

/// Discriminated union of every line the recorder prints on stdout.
public enum RecorderEvent: Equatable, Sendable {
    case started(StartedEvent)
    case stopped(StoppedEvent)
    case error(ErrorEvent)
    case log(LogEvent)

    public var name: String {
        switch self {
        case .started: return "started"
        case .stopped: return "stopped"
        case .error: return "error"
        case .log: return "log"
        }
    }
}

extension RecorderEvent: Codable {
    private enum EventKey: String, CodingKey {
        case event
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: EventKey.self)
        let name = try container.decode(String.self, forKey: .event)
        switch name {
        case "started": self = .started(try StartedEvent(from: decoder))
        case "stopped": self = .stopped(try StoppedEvent(from: decoder))
        case "error": self = .error(try ErrorEvent(from: decoder))
        case "log": self = .log(try LogEvent(from: decoder))
        default:
            throw DecodingError.dataCorruptedError(
                forKey: .event, in: container, debugDescription: "unknown event \(name)")
        }
    }

    public func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: EventKey.self)
        try container.encode(name, forKey: .event)
        switch self {
        case .started(let payload): try payload.encode(to: encoder)
        case .stopped(let payload): try payload.encode(to: encoder)
        case .error(let payload): try payload.encode(to: encoder)
        case .log(let payload): try payload.encode(to: encoder)
        }
    }
}

// MARK: - Exact line encoding

/// Produces the exact single-line JSON of the wire protocol (fixed key order).
public enum RecorderEventLine {
    public static func encode(_ event: RecorderEvent) -> String {
        json(for: event).serialized()
    }

    public static func json(for event: RecorderEvent) -> JSONValue {
        switch event {
        case .started(let payload):
            return .obj([
                "event": .string("started"),
                "started_at": .string(payload.startedAt),
                "tracks": json(for: payload.tracks),
            ])
        case .stopped(let payload):
            return .obj([
                "event": .string("stopped"),
                "stopped_at": .string(payload.stoppedAt),
                "duration_sec": .number(payload.durationSec.roundedToMilliseconds),
                "tracks": json(for: payload.tracks),
            ])
        case .error(let payload):
            return .obj([
                "event": .string("error"),
                "code": .string(payload.code.rawValue),
                "message": .string(payload.message),
            ])
        case .log(let payload):
            return .obj([
                "event": .string("log"),
                "message": .string(payload.message),
            ])
        }
    }

    public static func json(for tracks: StartedTracks) -> JSONValue {
        var members: [JSONMember] = []
        if let screen = tracks.screen {
            members.append(JSONMember(key: "screen", value: .string(screen)))
        }
        members.append(JSONMember(key: "mic", value: .string(tracks.mic)))
        members.append(JSONMember(key: "system", value: .string(tracks.system)))
        return .object(members)
    }

    public static func json(for tracks: StoppedTracks) -> JSONValue {
        var members: [JSONMember] = []
        if let screen = tracks.screen {
            members.append(JSONMember(key: "screen", value: json(for: screen)))
        }
        members.append(JSONMember(key: "mic", value: json(for: tracks.mic)))
        members.append(JSONMember(key: "system", value: json(for: tracks.system)))
        return .object(members)
    }

    public static func json(for summary: TrackSummary) -> JSONValue {
        .obj([
            "path": .string(summary.path),
            "bytes": .integer(summary.bytes),
            "duration_sec": .number(summary.durationSec.roundedToMilliseconds),
        ])
    }
}

// MARK: - recorder.json

/// Content of `<output>/recorder.json`: the `stopped` payload plus `started_at`.
public struct RecorderSummary: Codable, Equatable, Sendable {
    public var startedAt: String
    public var stoppedAt: String
    public var durationSec: Double
    /// nil when finalization failed before any track summary was produced.
    public var tracks: StoppedTracks?
    public var recorderVersion: String
    /// Present only when the recording ended because of a failure (files may be partial).
    public var error: ErrorEvent?

    public init(
        startedAt: String,
        stoppedAt: String,
        durationSec: Double,
        tracks: StoppedTracks?,
        recorderVersion: String = NarumiRecorderKit.version,
        error: ErrorEvent? = nil
    ) {
        self.startedAt = startedAt
        self.stoppedAt = stoppedAt
        self.durationSec = durationSec
        self.tracks = tracks
        self.recorderVersion = recorderVersion
        self.error = error
    }

    enum CodingKeys: String, CodingKey {
        case startedAt = "started_at"
        case stoppedAt = "stopped_at"
        case durationSec = "duration_sec"
        case tracks
        case recorderVersion = "recorder_version"
        case error
    }

    /// The `stopped` event for a clean stop; nil when no track summary exists.
    public var stoppedEvent: StoppedEvent? {
        guard let tracks else {
            return nil
        }
        return StoppedEvent(stoppedAt: stoppedAt, durationSec: durationSec, tracks: tracks)
    }

    public func json() -> JSONValue {
        var members: [JSONMember] = [
            JSONMember(key: "started_at", value: .string(startedAt)),
            JSONMember(key: "stopped_at", value: .string(stoppedAt)),
            JSONMember(key: "duration_sec", value: .number(durationSec.roundedToMilliseconds)),
            JSONMember(key: "tracks", value: tracks.map { RecorderEventLine.json(for: $0) } ?? .object([])),
            JSONMember(key: "recorder_version", value: .string(recorderVersion)),
        ]
        if let error {
            members.append(
                JSONMember(
                    key: "error",
                    value: .obj([
                        "code": .string(error.code.rawValue),
                        "message": .string(error.message),
                    ])))
        }
        return .object(members)
    }

    public func serialized() -> String {
        json().serialized()
    }
}

public enum NarumiRecorderKit {
    public static let version = "0.1.4"
}

// MARK: - Sinks

/// Where events go. The CLI uses stdout; tests capture lines in memory.
public protocol EventSink: Sendable {
    func emit(_ event: RecorderEvent)
}

public struct StdoutEventSink: EventSink {
    private static let lock = NSLock()

    public init() {}

    public func emit(_ event: RecorderEvent) {
        let line = RecorderEventLine.encode(event) + "\n"
        StdoutEventSink.lock.lock()
        defer { StdoutEventSink.lock.unlock() }
        fputs(line, stdout)
        fflush(stdout)
    }
}

public final class CollectingEventSink: EventSink, @unchecked Sendable {
    private let lock = NSLock()
    private var storage: [RecorderEvent] = []

    public init() {}

    public func emit(_ event: RecorderEvent) {
        lock.lock()
        defer { lock.unlock() }
        storage.append(event)
    }

    public var events: [RecorderEvent] {
        lock.lock()
        defer { lock.unlock() }
        return storage
    }

    public var lines: [String] {
        events.map(RecorderEventLine.encode)
    }
}
