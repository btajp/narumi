import Foundation

/// Sheet-local state only. It has no persistence or access to the settings file/environment.
/// All operation results are supplied by the view after a NarumiClient MCP call.
public struct GaiaConnectionSettings: Sendable {
    public static let defaultURL = "http://127.0.0.1:4111/mcp"

    public enum Operation: Equatable, Sendable {
        case loading
        case saving
        case testing
        case disabling

        public var label: String {
            switch self {
            case .loading: return "接続設定を読み込み中…"
            case .saving: return "保存中…"
            case .testing: return "接続テスト中（タイムアウト 5 秒）…"
            case .disabling: return "Gaia を無効化中…"
            }
        }
    }

    public private(set) var connection: GaiaConnection?
    public var url = ""
    public var apiKey = ""
    public private(set) var clearAPIKey = false
    public private(set) var operation: Operation?
    public private(set) var errorMessage: String?
    public private(set) var notice: String?
    public private(set) var testResult: GaiaConnectionTestResult?
    /// Only a get_gaia_connection invalid_argument permits replacing invalid environment
    /// settings without a readable current connection. Saved-file failures stay closed.
    public private(set) var needsEnvironmentRepair = false

    public init() {}

    public var isBusy: Bool { operation != nil }
    public var isLoaded: Bool { connection != nil }
    public var canEdit: Bool { !isBusy && (isLoaded || needsEnvironmentRepair) }
    public var normalizedURL: String { url.trimmingCharacters(in: .whitespacesAndNewlines) }

    public var hasUnsavedChanges: Bool {
        normalizedURL != (connection?.url ?? "") || !apiKey.isEmpty || clearAPIKey
    }

    public var canSave: Bool {
        guard canEdit && !normalizedURL.isEmpty else { return false }
        if needsEnvironmentRepair {
            // Never retain an unknown environment credential during recovery.
            return !apiKey.isEmpty && !clearAPIKey
        }
        return hasUnsavedChanges || connection?.source == .environment
    }

    public var canTest: Bool {
        isLoaded && !isBusy && connection?.url != nil && !hasUnsavedChanges
    }

    public var canDisable: Bool {
        canEdit && (needsEnvironmentRepair || connection?.url != nil || connection?.source != .saved)
    }

    public var changesExistingURL: Bool {
        connection?.url != nil && normalizedURL != connection?.url
    }

    public var currentTestResult: GaiaConnectionTestResult? {
        hasUnsavedChanges ? nil : testResult
    }

    public mutating func setClearAPIKey(_ clear: Bool) {
        guard !needsEnvironmentRepair else { return }
        clearAPIKey = clear
        if clear {
            apiKey = ""
        }
    }

    @discardableResult
    public mutating func beginLoad() -> Bool {
        guard !isBusy else { return false }
        needsEnvironmentRepair = false
        begin(.loading)
        return true
    }

    public mutating func loaded(_ connection: GaiaConnection) {
        guard operation == .loading else { return }
        adopt(connection)
        operation = nil
    }

    public mutating func beginSave() -> SetGaiaConnectionRequest? {
        guard canSave else { return nil }
        let key: SetGaiaConnectionRequest.APIKeyUpdate
        if clearAPIKey {
            key = .clear
        } else if apiKey.isEmpty {
            key = .unchanged
        } else {
            key = .replace(apiKey)
        }
        let request = SetGaiaConnectionRequest(url: normalizedURL, apiKey: key)
        begin(.saving)
        return request
    }

    public mutating func beginDisable() -> SetGaiaConnectionRequest? {
        guard canDisable else { return nil }
        // Never send an unsaved key with the disable request.
        apiKey = ""
        begin(.disabling)
        return SetGaiaConnectionRequest(url: nil)
    }

    public mutating func saved(_ connection: GaiaConnection) {
        guard operation == .saving || operation == .disabling else { return }
        let wasDisabling = operation == .disabling
        adopt(connection)
        operation = nil
        notice = wasDisabling
            ? "Gaia を無効にし、API キーを削除しました。"
            : "接続設定を保存しました。接続テストで確認してください。"
    }

    public mutating func beginTest() -> TestGaiaConnectionRequest? {
        guard canTest else { return nil }
        begin(.testing)
        return TestGaiaConnectionRequest()
    }

    public mutating func tested(_ result: GaiaConnectionTestResult) {
        guard operation == .testing else { return }
        testResult = result
        operation = nil
    }

    public mutating func failed(code: String, message: String) {
        guard let failedOperation = operation else { return }
        if failedOperation == .loading {
            connection = nil
            needsEnvironmentRepair = code == "invalid_argument"
            apiKey = ""
            clearAPIKey = false
            operation = nil
            // These values have never been validated or returned as public settings. Do
            // not render the raw error message, which could contain a URL or credential.
            errorMessage = needsEnvironmentRepair
                ? "invalid_argument: 環境変数の接続設定が不正です。新しい URL と API キーで置き換えるか、Gaia を無効にしてください。"
                : "\(code): 接続設定を安全に読み込めません。サーバーの状態を確認し、再読み込みしてください。"
            return
        }
        if needsEnvironmentRepair && code != "invalid_argument" {
            // A failed replacement may now indicate an unreadable saved file. Require a
            // fresh successful read or recoverable load error before attempting writes.
            needsEnvironmentRepair = false
        }
        var detail = "\(code): \(message)"
        let hadKeyInput = !apiKey.isEmpty
        if hadKeyInput {
            // Do not echo a key even if a transport/server error unexpectedly includes it.
            detail = detail.replacingOccurrences(of: apiKey, with: "[非表示]")
        }
        apiKey = ""
        operation = nil
        errorMessage = "\(Self.failureHint(code: code))\n\(detail)"
        if hadKeyInput {
            errorMessage? += "\nAPI キー入力は消去しました。必要な場合は再入力してください。"
        }
    }

    /// Also invalidates in-flight callbacks when the sheet/window disappears.
    public mutating func dismiss() {
        apiKey = ""
        clearAPIKey = false
        needsEnvironmentRepair = false
        operation = nil
        url = connection?.url ?? ""
        testResult = nil
        errorMessage = nil
        notice = nil
    }

    private mutating func begin(_ operation: Operation) {
        self.operation = operation
        errorMessage = nil
        notice = nil
        testResult = nil
    }

    private mutating func adopt(_ connection: GaiaConnection) {
        self.connection = connection
        url = connection.url ?? ""
        apiKey = ""
        clearAPIKey = false
        needsEnvironmentRepair = false
    }

    private static func failureHint(code: String) -> String {
        switch code {
        case "engine_unavailable":
            return "Gaia の起動状態・接続 URL・API キーを確認してください。"
        case "contract_mismatch":
            return "Gaia の契約または対応機能が Narumi と互換ではありません。対応バージョンを確認してください。"
        case "invalid_argument":
            return "接続 URL または API キーの入力内容を確認してください。"
        case "transport":
            return "Narumi サーバーに接続できません。サーバーの状態を確認してください。"
        default:
            return "Gaia 接続設定の操作に失敗しました。"
        }
    }
}
