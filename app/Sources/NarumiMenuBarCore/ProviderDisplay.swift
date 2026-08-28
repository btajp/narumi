import Foundation

public enum ProviderDisplay {
    public static func name(_ provider: ProviderID) -> String {
        switch provider {
        case .anthropicAPI: return "Anthropic API"
        case .openaiAPI: return "OpenAI API"
        case .claudeAgentSDK: return "Claude Agent SDK"
        case .ollama: return "Ollama"
        case .codexAppServer: return "Codex App Server"
        }
    }

    public static func availability(_ state: ProviderAvailability) -> String {
        switch state {
        case .available: return "対応を確認済み"
        case .notPrepared: return "実行環境が未準備"
        case .unverified: return "対応は未確認"
        case .authenticationRequired: return "認証が必要"
        case .unsupported: return "未対応"
        case .retired: return "提供終了"
        }
    }

    public static func runtime(_ state: ProviderRuntimeState) -> String {
        switch state {
        case .ready: return "準備済み"
        case .notPrepared: return "未準備"
        case .preparing: return "準備中"
        case .unavailable: return "利用不可"
        case .failed: return "準備に失敗"
        case .unknown: return "準備状況は未確認"
        }
    }

    public static func authentication(_ state: ProviderAuthState) -> String {
        switch state {
        case .unconfigured: return "未設定"
        case .unverified: return "認証は未確認"
        case .authenticating: return "認証を確認中"
        case .authenticated: return "認証確認済み"
        case .failed: return "認証に失敗"
        case .unknown: return "認証結果は未確認"
        }
    }

    public static func authOperation(_ state: ProviderAuthOperationState) -> String {
        switch state {
        case .pending: return "認証操作を処理中"
        case .succeeded: return "認証操作が完了"
        case .failed: return "認証操作に失敗"
        case .cancelled: return "認証操作を取消済み"
        case .unknown: return "認証操作の結果は不明（再送しません）"
        }
    }

    public static func catalog(_ state: ProviderCatalogState) -> String {
        switch state {
        case .unfetched: return "未取得"
        case .ready: return "取得済み"
        case .stale: return "再取得が必要"
        case .failed: return "取得に失敗"
        case .authenticationRequired: return "認証が必要"
        }
    }

    public static func generation(_ state: ProviderGenerationState) -> String {
        switch state {
        case .never: return "未実行・未検証"
        case .succeeded: return "前回は成功"
        case .failed: return "前回は失敗"
        case .cancelled: return "前回は取消"
        case .unknown: return "前回の結果は不明"
        }
    }

    public static func connectionVerificationScope(_ provider: ProviderID) -> String {
        if provider == .openaiAPI {
            return "保存した API キーで https://api.openai.com/v1/models のモデル一覧を照会します。残高や生成権限は確認できず、会議データの送信・議事録生成は行いません。"
        }
        return "ログイン・認証確認とメタデータの照会だけを行います。会議データの送信・議事録生成は行いません。"
    }

    public static func connectionTestResult(_ result: ProviderConnectionTestResult) -> String {
        guard result.connected else {
            return "接続を確認できませんでした。認証・実行環境の状態を確認してください。"
        }
        if result.connection.providerID == .openaiAPI {
            return "モデル一覧を取得できました。残高・生成権限・議事録生成の成功は未確認です。"
        }
        return "接続とメタデータを確認しました。議事録生成は未検証です。"
    }

    public static func setup(_ state: ProviderSetupState) -> String {
        switch state {
        case .queued: return "準備の受付済み"
        case .running: return "準備中"
        case .succeeded: return "準備ジョブが完了"
        case .failed: return "準備ジョブに失敗"
        case .cancelled: return "準備ジョブを取消済み"
        case .unknown: return "準備の結果は不明（再送しません）"
        }
    }

    public static func modality(_ modality: ProviderModality) -> String {
        switch modality {
        case .text: return "テキスト"
        case .image: return "画像"
        case .audio: return "音声"
        }
    }

    public static func billing(_ billing: ProviderBillingKind) -> String {
        switch billing {
        case .local: return "ローカル実行"
        case .api: return "API 課金"
        case .subscription: return "サブスクリプションの利用枠"
        case .unknown: return "課金区分は未確認"
        }
    }

    public static func price(_ amount: String?, unit: String) -> String {
        amount.map { "$\($0) / \(unit)" } ?? "不明（無料とは扱いません）"
    }

    public static func reason(_ code: String?) -> String? {
        guard let code else { return nil }
        switch code {
        case "provider_generation_outcome_unknown", "codex_generation_outcome_unknown":
            return ToolErrorInfo.generationOutcomeMessage(reason: code, unknown: true)
        case "credential_required", "authentication_required": return "この接続の認証設定と認証確認が必要です。"
        case "runtime_verification_pending", "runtime_preparation_required": return "実行環境の準備・確認が必要です。"
        case "runtime_preparation_failed": return "実行環境の準備に失敗しました。準備状態を確認してください。"
        case "local_server_verification_required": return "ローカルサーバーの接続確認が必要です。"
        case "adapter_capability_verification_required": return "このモデルを実行するアダプタの対応を確認できていません。"
        case "authentication_operation_interrupted": return "サーバーの再起動などにより、認証操作の継続を確認できません。"
        case "authentication_verification_unavailable": return "認証の完了を確認できません。実行環境とログイン状態を確認してください。"
        case "device_code_login_unavailable":
            return "デバイスコード認証を開始できませんでした。ChatGPT 側の認証設定を確認してください。他方式への自動切替は行いません。"
        case "authentication_cancelled": return "認証操作は取り消されました。再開する場合は、認証を明示的に開始してください。"
        case "connection_configuration_changed": return "接続設定が変更されました。一覧を再読み込みして現在の設定を確認してください。"
        case "connection_logged_out": return "この接続からログアウトしています。利用するには再認証が必要です。"
        case "unsafe_sdk_persistence", "sdk_isolation_unverified", "sdk_authentication_and_history_isolation_unverified":
            return "SDK の認証情報・履歴の隔離を確認できていないため利用できません。"
        case "connection_disabled": return "接続が無効です。利用する場合は有効にして保存してください。"
        case "model_capabilities_unknown", "model_capabilities_unavailable", "codex_text_capability_unverified":
            return "モデルの能力を確認できていないため、利用可能とは扱いません。"
        case "local_model_metadata_unverified", "local_model_verification_failed":
            return "ローカルモデルの情報・実行場所を確認できていません。"
        case "remote_models_not_supported": return "この接続ではクラウド・リモートモデルを利用できません。"
        case "text_completion_not_supported": return "このモデルは文章生成に対応していません。"
        case "credential_rejected": return "保存済みの認証情報が受け付けられませんでした。ログインし直すか、API キーを確認してください。"
        case "model_list_verified_generation_unchecked":
            return "モデル一覧の取得を確認しました。残高・生成権限・実際の議事録生成は未確認です。"
        case "metadata_connection_failed", "metadata_timeout", "metadata_http_error", "metadata_unavailable":
            return "メタデータの取得に失敗しました。接続先の状態を確認してください。"
        case "metadata_catalog_limit", "metadata_page_limit", "metadata_size_limit":
            return "メタデータが安全に扱える取得上限を超えました。"
        case "invalid_metadata", "unsafe_metadata", "redirect_rejected", "metadata_response_rejected":
            return "安全性または形式を確認できない応答のため、取得を停止しました。"
        default: return "利用条件を確認できません。認証と実行環境の状態を確認してください。"
        }
    }
}
