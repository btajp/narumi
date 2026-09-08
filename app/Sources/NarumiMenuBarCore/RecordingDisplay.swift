import Foundation

/// A capture target returned by `list_recording_displays` and recording status tools.
public struct RecordingDisplay: Codable, Equatable, Sendable, Identifiable {
    public let id: UInt32
    public let name: String
    public let width: Int
    public let height: Int
    public let isMain: Bool

    enum CodingKeys: String, CodingKey {
        case id, name, width, height
        case isMain = "is_main"
    }

    public init(id: UInt32, name: String, width: Int, height: Int, isMain: Bool) {
        self.id = id
        self.name = name
        self.width = width
        self.height = height
        self.isMain = isMain
    }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(UInt32.self, forKey: .id)
        name = try values.decode(String.self, forKey: .name)
        width = try values.decode(Int.self, forKey: .width)
        height = try values.decode(Int.self, forKey: .height)
        isMain = try values.decode(Bool.self, forKey: .isMain)
        guard id > 0, !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, width > 0, height > 0 else {
            throw DecodingError.dataCorrupted(
                .init(codingPath: decoder.codingPath, debugDescription: "録画対象の画面情報が不正です"))
        }
    }

    public var selectionTitle: String {
        "\(name) — \(width) × \(height)（ID: \(id)\(isMain ? "、メイン" : "")）"
    }
}

public struct ListRecordingDisplaysResponse: Decodable, Equatable, Sendable {
    public let displays: [RecordingDisplay]

    public init(displays: [RecordingDisplay]) {
        self.displays = displays
    }

    /// The operating system's main display determines the default, never array order.
    public func defaultSelectionIndex() throws -> Int {
        guard !displays.isEmpty else { throw RecordingDisplaySelectionError.noDisplays }
        guard Set(displays.map(\.id)).count == displays.count else {
            throw RecordingDisplaySelectionError.duplicateDisplays
        }
        let mainIndices = displays.indices.filter { displays[$0].isMain }
        guard mainIndices.count == 1, let index = mainIndices.first else {
            throw RecordingDisplaySelectionError.mainDisplayUnavailable
        }
        return index
    }
}

public enum RecordingDisplaySelectionError: Error, LocalizedError {
    case noDisplays
    case duplicateDisplays
    case mainDisplayUnavailable

    public var errorDescription: String? {
        switch self {
        case .noDisplays:
            return "録画できる画面が見つかりません。画面の接続と画面収録の権限を確認してください。"
        case .duplicateDisplays:
            return "録画対象の画面情報が重複しています。画面の接続を確認してから再試行してください。"
        case .mainDisplayUnavailable:
            return "メイン画面を特定できないため、録画を開始していません。画面の接続を確認してから再試行してください。"
        }
    }
}
