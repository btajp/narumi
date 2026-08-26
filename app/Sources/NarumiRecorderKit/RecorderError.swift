import Foundation

/// Error codes of the `{"event":"error"}` line. The Python server maps these to its own codes.
public enum RecorderErrorCode: String, Codable, Sendable, CaseIterable {
    case permissionDenied = "permission_denied"
    case noDisplay = "no_display"
    case captureFailed = "capture_failed"
    case writerFailed = "writer_failed"
    case invalidArgument = "invalid_argument"
}

/// Structured recorder error. Everything the CLI reports on stdout goes through this type.
public struct RecorderError: Error, Equatable, Sendable, CustomStringConvertible {
    public let code: RecorderErrorCode
    public let message: String

    public init(_ code: RecorderErrorCode, _ message: String) {
        self.code = code
        self.message = message
    }

    public var description: String { "\(code.rawValue): \(message)" }

    /// Map any error to a ``RecorderError``. Unknown errors become `capture_failed`.
    public static func wrap(_ error: any Error, fallback: RecorderErrorCode = .captureFailed) -> RecorderError {
        if let recorderError = error as? RecorderError {
            return recorderError
        }
        return RecorderError(fallback, describe(error))
    }

    static func describe(_ error: any Error) -> String {
        let nsError = error as NSError
        return "\(nsError.localizedDescription) [\(nsError.domain) \(nsError.code)]"
    }
}
