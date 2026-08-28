import Foundation

/// A transient, verified login destination. It cannot be encoded or printed into
/// persisted recovery state, diagnostics or logs.
public struct ProviderAuthorizationURL: Decodable, Equatable, Sendable,
    CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable
{
    public static let deviceLoginAddress = "https://auth.openai.com/codex/device"
    public let browserURL: URL

    public init?(_ value: String) {
        guard value == Self.deviceLoginAddress, let url = URL(string: value) else { return nil }
        browserURL = url
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        let value = try container.decode(String.self)
        guard let verified = Self(value) else {
            throw DecodingError.dataCorruptedError(
                in: container, debugDescription: "The provider authorization URL is not an approved login destination")
        }
        self = verified
    }

    public var description: String { "ProviderAuthorizationURL(<redacted>)" }
    public var debugDescription: String { description }
    public var customMirror: Mirror { Mirror(self, children: ["authorizationURL": "<redacted>"]) }
}
