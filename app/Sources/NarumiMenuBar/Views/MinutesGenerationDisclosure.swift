import NarumiMenuBarCore
import SwiftUI

/// The same saved configuration is displayed here and sent as expected_config on submission.
struct MinutesGenerationDisclosure: View {
    let config: MeetingConfig
    let catalog: MinutesModelCatalogStore
    var includesNewContext = false

    private var selectedModel: ProviderModelDescriptor? {
        guard let selection = config.minutesModel,
            let response = catalog.catalogs[selection.connectionID],
            response.connectionRevision == selection.connectionRevision else { return nil }
        return response.models.first { $0.modelID == selection.modelID }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            if let selection = config.minutesModel {
                Text("\(providerName(selection.provider)) でテキスト議事録を生成").font(.subheadline.bold())
                LabeledContent("接続", value: catalog.connection(selection.connectionID)?.displayName ?? selection.connectionID)
                Text("\(selection.connectionID) / rev \(selection.connectionRevision)")
                    .font(.caption2).foregroundStyle(.secondary)
                LabeledContent("モデル", value: selection.modelID)
                if let date = selectedModel?.availabilityExpiresOn {
                    LabeledContent("提供終了予定日", value: date)
                }
                if selection.provider == "codex-app-server" || selection.provider == "openai-api" {
                    LabeledContent("推論量", value: reasoningDescription(selection))
                }
                if selection.provider != "codex-app-server" {
                    LabeledContent("要求する出力上限", value: outputLimitDescription(selection))
                    LabeledContent("モデルの出力上限", value: selectedModel?.maxOutputTokens.map { "\($0) tokens" } ?? "未確認")
                    Text("出力上限は 1 回の生成要求に適用します。複数回の処理を含む総利用量や金額の上限ではありません。")
                        .foregroundStyle(.secondary)
                } else {
                    Text("Codex では出力トークン数の上限を指定できません。利用枠の消費量の上限は保証しません。")
                        .foregroundStyle(.secondary)
                }
                LabeledContent("生成の試行番号", value: String(selection.cacheEpoch))
                Text(providerDisclosure(selection))
                Text(includesNewContext
                     ? "処理する内容: 文字起こし・話者名・会議名・議事録に使うコンテキストのテキスト（今回登録する内容を含む）。"
                     : "処理する内容: 文字起こし・話者名・会議名・議事録に使うコンテキストのテキスト。")
                Text("この議事録生成には音声・動画・画像を渡しません。他の工程には従来の設定を適用します。")
                    .foregroundStyle(.secondary)
                if selection.provider == "openai-api" || selection.provider == "anthropic-api" {
                    Text("取消しても、サービス側の処理や課金の停止は保証できません。送信後に結果が不明になっても自動再送せず、新しい試行では API 利用料が重複する場合があります。")
                        .foregroundStyle(.secondary)
                } else if selection.provider == "codex-app-server" {
                    Text("結果不明の送信は自動再送しません。新しい試行では ChatGPT の利用枠を重複して消費する場合があります。")
                        .foregroundStyle(.secondary)
                } else {
                    Text("同じ入力・選択では保存済みの結果を再利用し、結果不明の処理は自動再実行しません。新しい試行ではローカルで改めて生成します。")
                        .foregroundStyle(.secondary)
                }
                if selection.provider == "openai-api" {
                    Text("応答保存を無効にして要求しますが、サービス側の不正利用監視などによるデータ保持がなくなることは保証しません。")
                        .foregroundStyle(.secondary)
                }
                if let message = catalog.validationMessage(for: ProcessingConfigurationForm(config: config)) {
                    Text(message).foregroundStyle(.orange)
                }
            } else {
                Text("議事録の LLM: \(config.llmProvider ?? "none")")
                Text("送信の可否は保存済みの外部送信ポリシーに従います。")
                    .foregroundStyle(.secondary)
            }
            Text("外部送信ポリシー: \(config.externalSendPolicy ?? "local_only")")
        }
        .font(.caption)
        .fixedSize(horizontal: false, vertical: true)
        .task(id: MinutesModelForm(selection: config.minutesModel).catalogReadIdentity
              + "/" + catalog.supportedProviders.joined(separator: ",")) {
            guard let selection = config.minutesModel else { return }
            await catalog.loadCachedCatalog(connectionID: selection.connectionID, selectedModelID: selection.modelID)
        }
    }

    private func providerName(_ provider: String) -> String {
        ProviderID(rawValue: provider).map { ProviderDisplay.name($0) } ?? provider
    }

    private func reasoningDescription(_ selection: MinutesModelSelection) -> String {
        if let effort = selection.parameters.reasoningEffort { return effort }
        if let defaultValue = selectedModel?.parameterSchema.properties["reasoning_effort"]?.defaultValue,
            case .string(let value) = defaultValue {
            return "モデルの既定値（\(value)）"
        }
        return selection.provider == "codex-app-server" ? "モデルの既定値" : "指定なし（対応するモデルだけ設定可能）"
    }

    private func outputLimitDescription(_ selection: MinutesModelSelection) -> String {
        if let value = selection.parameters.maxTokens { return "\(value) tokens（指定値）" }
        return "\(MinutesModelForm.defaultOutputLimit(selectedModel)) tokens（アプリの既定値）"
    }

    private func providerDisclosure(_ selection: MinutesModelSelection) -> String {
        switch selection.provider {
        case "codex-app-server":
            return "送信先: OpenAI（https://chatgpt.com）。テキストを送信し、ChatGPT の利用枠を使用します。subscription_ok または api_ok が必要で、API 課金へは切り替えません。"
        case "openai-api":
            return "送信先: OpenAI（https://api.openai.com/v1/responses）。api_ok の許可に基づきテキストを送信し、従量 API 課金を使用します。ChatGPT の利用枠とは別です。"
        case "anthropic-api":
            return "送信先: Anthropic（https://api.anthropic.com）。api_ok の許可に基づきテキストを送信し、従量 API 課金を使用します。Claude のサブスクリプションとは別です。"
        case "ollama":
            let connection = catalog.connection(selection.connectionID)
            let endpoint = connection?.revision == selection.connectionRevision && connection?.providerID == .ollama
                ? connection?.endpoint ?? "接続先は未確認" : "保存時の接続先は再確認が必要"
            return "接続先: この Mac の Ollama（\(endpoint)）。ローカル実行を確認したモデルでテキストを処理します。local_only で利用でき、API 課金のプロバイダへは切り替えません。"
        default:
            return "このサーバーで選択したプロバイダを利用できるか確認してください。未対応のプロバイダでは生成しません。"
        }
    }
}
