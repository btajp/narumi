import Foundation

public struct MinutesParticipantDisclosure: Equatable, Sendable {
    public let providerName: String
    public let destination: String
    public let billing: ProviderBillingKind

    public static func make(provider: String, endpoint: String? = nil) -> Self? {
        guard let providerID = ProviderID(rawValue: provider) else { return nil }
        let destination: String
        let billing: ProviderBillingKind
        switch providerID {
        case .codexAppServer:
            destination = "OpenAI（chatgpt.com）"
            billing = .subscription
        case .openaiAPI:
            destination = "OpenAI API（api.openai.com）"
            billing = .api
        case .anthropicAPI:
            destination = "Anthropic API（api.anthropic.com）"
            billing = .api
        case .ollama:
            destination = endpoint.map { "この Mac の Ollama（\($0)）" } ?? "この Mac の Ollama"
            billing = .local
        case .claudeAgentSDK:
            return nil
        }
        return Self(providerName: ProviderDisplay.name(providerID), destination: destination, billing: billing)
    }
}
