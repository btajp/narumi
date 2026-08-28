import Foundation

/// The provider settings surface has no meeting mutation or generation method.
/// A fake implementation can exercise recovery without credentials, Keychain or a server.
public protocol ProviderSettingsClient: Sendable {
    func listProviders() async throws -> ListProvidersResponse
    func listProviderConnections() async throws -> ListProviderConnectionsResponse
    func setProviderConnection(_ request: SetProviderConnectionRequest) async throws -> ProviderConnectionResponse
    func deleteProviderConnection(_ request: DeleteProviderConnectionRequest) async throws -> DeleteProviderConnectionResponse
    func authenticateProviderConnection(_ request: AuthenticateProviderConnectionRequest) async throws -> ProviderAuthResponse
    func providerAuthStatus(_ request: GetProviderAuthStatusRequest) async throws -> ProviderAuthResponse
    func testProviderConnection(_ request: TestProviderConnectionRequest) async throws -> ProviderConnectionTestResult
    func listProviderModels(_ request: ListProviderModelsRequest) async throws -> ListProviderModelsResponse
    func prepareProviderRuntime(_ request: PrepareProviderRuntimeRequest) async throws -> PrepareProviderRuntimeResponse
    func jobStatus(jobID: String) async throws -> Job
    func cancelJob(jobID: String) async throws -> Job
}

public enum ProviderSettingsErrorCode: String, Sendable {
    case invalidArgument = "invalid_argument"
    case notFound = "not_found"
    case configurationConflict = "configuration_conflict"
    case authenticationRequired = "authentication_required"
    case engineUnavailable = "engine_unavailable"
    case modelUnavailable = "model_unavailable"
    case busy
    case cancelled
    case transport
    case protocolError = "protocol"
    case contractMismatch = "contract_mismatch"
    case unsupported
    case internalError = "internal"

    public var message: String {
        switch self {
        case .invalidArgument:
            return "入力内容を確認してください。接続先や認証方式には制限があります。"
        case .notFound:
            return "対象が見つかりません。一覧を更新して状態を確認してください。"
        case .configurationConflict:
            return "別の操作で設定が変更されました。一覧を更新してから編集してください。"
        case .authenticationRequired:
            return "認証が必要です。安全なサーバー接続と、対象のログインまたは API キー設定を確認してください。"
        case .engineUnavailable:
            return "実行環境または接続先を利用できません。準備状況と接続設定を確認してください。"
        case .modelUnavailable:
            return "モデル候補を利用できません。一覧を更新し、利用できない理由を確認してください。"
        case .busy:
            return "関連する操作が実行中です。状態を確認してから操作してください。"
        case .cancelled:
            return "操作は取り消されました。"
        case .transport:
            return "サーバーとの通信を確認できません。操作は自動再送しません。"
        case .protocolError, .contractMismatch:
            return "応答の互換性を確認できません。アプリとサーバーの対応バージョンを確認してください。"
        case .unsupported:
            return "この環境では対応していない操作です。"
        case .internalError:
            return "接続設定の操作に失敗しました。秘密を含む可能性がある詳細は表示しません。"
        }
    }

    /// The service guarantees these errors precede a new authentication/setup receipt.
    /// engine_unavailable can follow durable acceptance and must remain ambiguous.
    public var rejectsBeforeAcceptance: Bool {
        [.invalidArgument, .notFound, .configurationConflict, .busy, .authenticationRequired].contains(self)
    }
}

/// Raw upstream errors are intentionally discarded at the client boundary.
public struct ProviderSettingsFailure: Error, Equatable, Sendable {
    public let code: ProviderSettingsErrorCode

    public init(code: String) {
        self.code = ProviderSettingsErrorCode(rawValue: code) ?? .internalError
    }

    public init(_ code: ProviderSettingsErrorCode) { self.code = code }

    public var message: String { "\(code.rawValue): \(code.message)" }
}
