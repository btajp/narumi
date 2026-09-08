import Foundation

/// A successful stop can still contain a recorder failure and incomplete saved output.
public struct RecordingStopFeedback: Decodable, Equatable, Sendable {
    public let recorderError: ToolErrorInfo?

    enum CodingKeys: String, CodingKey {
        case recorderError = "recorder_error"
    }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        recorderError = values.contains(.recorderError) ? try values.decode(ToolErrorInfo.self, forKey: .recorderError) : nil
    }

    public var warningMessage: String? {
        guard let recorderError else { return nil }
        return "録画を停止し、保存できたトラックを保持しています。録画処理が正常に完了しなかったため、内容を確認してください。\n\n\(recorderError.code): \(recorderError.message)"
    }
}
