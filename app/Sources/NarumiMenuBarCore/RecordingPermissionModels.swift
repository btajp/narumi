import Foundation

public enum RecordingPermission: String, Codable, CaseIterable, Identifiable, Sendable {
    case microphone
    case screenRecording = "screen_recording"

    public var id: String { rawValue }
}

public enum RecordingPermissionAction: String, Codable, CaseIterable, Identifiable, Sendable {
    case request
    case openSettings = "open_settings"

    public var id: String { rawValue }
}

/// An OS refusal is a successful action with denied permissions, not a transport error.
public struct ConfigureRecordingPermissionResponse: Codable, Equatable, Sendable {
    public var permission: RecordingPermission
    public var action: RecordingPermissionAction
    public var permissions: RecorderPermissions
    public var settingsOpened: Bool

    enum CodingKeys: String, CodingKey {
        case permission
        case action
        case permissions
        case settingsOpened = "settings_opened"
    }

    public init(
        permission: RecordingPermission, action: RecordingPermissionAction,
        permissions: RecorderPermissions, settingsOpened: Bool
    ) {
        self.permission = permission
        self.action = action
        self.permissions = permissions
        self.settingsOpened = settingsOpened
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        permission = try container.decode(RecordingPermission.self, forKey: .permission)
        action = try container.decode(RecordingPermissionAction.self, forKey: .action)
        permissions = try container.decode(RecorderPermissions.self, forKey: .permissions)
        settingsOpened = try container.decode(Bool.self, forKey: .settingsOpened)
        let validStates: Set<String> = ["granted", "denied", "unknown"]
        guard validStates.contains(permissions.screenRecording), validStates.contains(permissions.microphone) else {
            throw DecodingError.dataCorruptedError(
                forKey: .permissions, in: container,
                debugDescription: "Recording permission states must be granted, denied or unknown")
        }
    }
}

/// Authenticated contracts v2 through v4 keep permission setup but refuse v1 downgrade.
public enum RecordingPermissionContract {
    public static func isValidServerInstanceID(_ serverInstanceID: String?) -> Bool {
        guard let serverInstanceID else { return false }
        let bytes = Array(serverInstanceID.utf8)
        guard bytes.count == 36 else { return false }
        let hyphens: Set<Int> = [8, 13, 18, 23]
        for (index, byte) in bytes.enumerated() {
            if hyphens.contains(index) {
                guard byte == 45 else { return false }
            } else {
                guard (48...57).contains(byte) || (97...102).contains(byte) else { return false }
            }
        }
        return bytes[14] == 52 && [UInt8(56), 57, 97, 98].contains(bytes[19])
    }

    public static func supportsSetup(_ contractVersion: String?) -> Bool {
        guard let contractVersion else { return false }
        let release = contractVersion.split(separator: "-", maxSplits: 1, omittingEmptySubsequences: false)
        guard let core = release.first else { return false }
        let components = core.split(separator: ".", omittingEmptySubsequences: false)
        guard components.count == 3,
            components.allSatisfy(isVersionNumber),
            let major = Int(components[0]), (2...4).contains(major),
            let minor = Int(components[1]),
            let patch = Int(components[2])
        else { return false }
        if release.count == 2 {
            guard !release[1].isEmpty else { return false }
            let identifiers = release[1].split(separator: ".", omittingEmptySubsequences: false)
            guard identifiers.allSatisfy(isPrereleaseIdentifier) else { return false }
        }
        // Only released baselines of the supported authenticated contracts are accepted.
        return minor > 0 || patch > 0 || release.count == 1
    }

    public static func supportsSetup(_ contractVersion: String?, serverInstanceID: String?) -> Bool {
        supportsSetup(contractVersion) && isValidServerInstanceID(serverInstanceID)
    }

    /// The initial/legacy probe stays empty until the current server identity is known.
    public static func serverInfoArguments(
        contractVersion: String?, serverInstanceID: String? = nil, refreshPermissions: Bool
    ) -> [String: Bool] {
        guard refreshPermissions, supportsSetup(contractVersion, serverInstanceID: serverInstanceID) else { return [:] }
        return ["refresh_permissions": true]
    }

    private static func isVersionNumber(_ value: Substring) -> Bool {
        !value.isEmpty && value.utf8.allSatisfy { (48...57).contains($0) }
            && (value.count == 1 || value.first != "0")
    }

    private static func isPrereleaseIdentifier(_ value: Substring) -> Bool {
        guard !value.isEmpty,
            value.utf8.allSatisfy({ (48...57).contains($0) || (65...90).contains($0)
                || (97...122).contains($0) || $0 == 45 })
        else { return false }
        return !value.utf8.allSatisfy { (48...57).contains($0) } || isVersionNumber(value)
    }
}
