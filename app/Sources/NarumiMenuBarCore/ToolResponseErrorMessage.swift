import Foundation

/// Decode failures can include literal response values. Credential-writing tools must not
/// turn an unexpected response value into an alert or another public error message.
public enum ToolResponseErrorMessage {
    public static func decoding(toolName: String, error: any Error) -> String {
        let summary = "\(toolName) の応答を解釈できません"
        if toolName == ToolCatalog.setGaiaConnection {
            return "\(summary)（安全のため応答の詳細は表示しません）"
        }
        return "\(summary): \(error)"
    }
}
