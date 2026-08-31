import Foundation

/// `defs/common.json#/$defs/error`.
public struct ToolErrorInfo: Codable, Equatable, Sendable {
    public var code: String
    public var message: String
    public var transcriptionOutcome: TranscriptionOutcomeUnknownDetails?

    enum CodingKeys: String, CodingKey { case code, message }
    private enum DetailKeys: String, CodingKey { case details }
    private struct OutcomeDetails: Decodable {
        let stage: String?
        let reason: String?
        let outcomeUnknown: Bool?
        enum CodingKeys: String, CodingKey {
            case stage, reason
            case outcomeUnknown = "outcome_unknown"
        }
    }

    public init(code: String, message: String, transcriptionOutcome: TranscriptionOutcomeUnknownDetails? = nil) {
        self.code = code
        self.message = message
        self.transcriptionOutcome = transcriptionOutcome
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        code = try container.decode(String.self, forKey: .code)
        let original = try container.decode(String.self, forKey: .message)
        let extra = try decoder.container(keyedBy: DetailKeys.self)
        let details = try? extra.decode(OutcomeDetails.self, forKey: .details)
        transcriptionOutcome = try? extra.decode(TranscriptionOutcomeUnknownDetails.self, forKey: .details)
        message = Self.generationOutcomeMessage(
            reason: details?.reason, unknown: details?.outcomeUnknown == true, stage: details?.stage) ?? original
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(code, forKey: .code)
        try container.encode(message, forKey: .message)
        if let transcriptionOutcome {
            var extra = encoder.container(keyedBy: DetailKeys.self)
            try extra.encode(transcriptionOutcome, forKey: .details)
        }
    }

    /// Only known generation facts are localized; arbitrary error details are not retained.
    public static func generationOutcomeMessage(reason: String?, unknown: Bool, stage: String? = nil) -> String? {
        if reason == "profile_save_outcome_unknown" {
            return "プロファイルが保存された可能性がありますが、保存結果を確認できません。自動で再保存しません。一覧からプロファイルを選び直して現在の設定を読み込み、確認してから再試行してください。"
        }
        if reason == "transcription_outcome_unknown" || (unknown && stage == "transcribe") {
            return "音声認識の送信結果が不明なため、自動再送しません。文字起こしタブで対象区間を確認して「不明区間を再送」を選んでください。再送では API 利用料が重複する場合があります。試行番号の変更だけでは再送しません。"
        }
        guard unknown || ["provider_generation_outcome_unknown", "codex_generation_outcome_unknown"].contains(reason ?? "") else {
            return nil
        }
        return "議事録生成の結果が不明なため、自動再送しません。新しい試行では外部プロバイダの課金・利用枠を重複して消費する可能性があります。会議設定で試行番号を増やす前に確認してください。"
    }
}
