import Foundation

/// A capturable display as reported by `list-displays`.
public struct DisplayInfo: Equatable, Sendable, Codable {
    public var id: UInt32
    public var width: Int
    public var height: Int
    public var name: String

    public init(id: UInt32, width: Int, height: Int, name: String) {
        self.id = id
        self.width = width
        self.height = height
        self.name = name
    }

    public func json() -> JSONValue {
        .obj([
            "id": .integer(Int(id)),
            "width": .integer(width),
            "height": .integer(height),
            "name": .string(name),
        ])
    }

    public static func jsonArray(_ displays: [DisplayInfo]) -> String {
        JSONValue.array(displays.map { $0.json() }).serialized()
    }
}

public enum DisplaySelection {
    /// Pick the requested display, or the first one when no id was given.
    public static func select(from displays: [DisplayInfo], requestedID: UInt32?) throws -> DisplayInfo {
        guard !displays.isEmpty else {
            throw RecorderError(
                .noDisplay,
                "no capturable display found (no active display: asleep, locked or headless?)")
        }
        guard let requestedID else {
            return displays[0]
        }
        guard let match = displays.first(where: { $0.id == requestedID }) else {
            let known = displays.map { String($0.id) }.joined(separator: ", ")
            throw RecorderError(.noDisplay, "display \(requestedID) not found (available: \(known))")
        }
        return match
    }
}

/// Encoded video size: capped width, aspect preserved, even dimensions (4:2:0 friendly).
public struct VideoDimensions: Equatable, Sendable {
    public var width: Int
    public var height: Int

    public init(width: Int, height: Int) {
        self.width = width
        self.height = height
    }

    public static func fit(width: Int, height: Int, maxWidth: Int = 1920) -> VideoDimensions {
        let safeWidth = max(width, 2)
        let safeHeight = max(height, 2)
        var outWidth = safeWidth
        var outHeight = safeHeight
        if safeWidth > maxWidth {
            outWidth = maxWidth
            outHeight = Int((Double(safeHeight) * Double(maxWidth) / Double(safeWidth)).rounded())
        }
        return VideoDimensions(width: even(outWidth), height: even(outHeight))
    }

    private static func even(_ value: Int) -> Int {
        let rounded = value + (value % 2)
        return max(rounded, 2)
    }
}
