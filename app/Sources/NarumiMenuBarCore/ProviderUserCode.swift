import Foundation

/// Displayed only during device authorization. This value is neither Encodable
/// nor exposed by reflection; copying it requires a separate user action.
public struct ProviderUserCode: Decodable, Equatable, Sendable,
    CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable
{
    public let displayValue: String

    public init?(_ value: String) {
        guard (1...32).contains(value.utf8.count), value.utf8.allSatisfy({
            (65...90).contains($0) || (97...122).contains($0) || (48...57).contains($0) || $0 == 45
        }) else { return nil }
        displayValue = value
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        let value = try container.decode(String.self)
        guard let verified = Self(value) else {
            throw DecodingError.dataCorruptedError(
                in: container, debugDescription: "The provider user code does not match the device authorization contract")
        }
        self = verified
    }

    public var description: String { "ProviderUserCode(<redacted>)" }
    public var debugDescription: String { description }
    public var customMirror: Mirror { Mirror(self, children: ["userCode": "<redacted>"]) }
}

public struct ProviderDeviceAuthorization: Equatable, Sendable {
    public let authorizationURL: ProviderAuthorizationURL
    public let userCode: ProviderUserCode
}
