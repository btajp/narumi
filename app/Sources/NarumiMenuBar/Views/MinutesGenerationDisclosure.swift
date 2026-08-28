import NarumiMenuBarCore
import SwiftUI

/// The same saved configuration is displayed here and sent as expected_config on submission.
struct MinutesGenerationDisclosure: View {
    let config: MeetingConfig
    let catalog: MinutesModelCatalogStore
    var includesNewContext = false

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            if let selection = config.minutesModel {
                Text("Codex App Server でテキスト議事録を生成").font(.subheadline.bold())
                LabeledContent("接続", value: catalog.connection(selection.connectionID)?.displayName ?? selection.connectionID)
                Text("\(selection.connectionID) / rev \(selection.connectionRevision)")
                    .font(.caption2).foregroundStyle(.secondary)
                LabeledContent("モデル", value: selection.modelID)
                LabeledContent("推論量", value: selection.parameters.reasoningEffort ?? "モデルの既定値")
                LabeledContent("生成の試行番号", value: String(selection.cacheEpoch))
                Text("送信先: OpenAI。ChatGPT の利用枠を使用し、API 課金へは切り替えません。")
                Text(includesNewContext
                     ? "送信する内容: 文字起こし・話者名・会議名・議事録に使うコンテキストのテキスト（今回登録する内容を含む）。"
                     : "送信する内容: 文字起こし・話者名・会議名・議事録に使うコンテキストのテキスト。")
                Text("Codex の議事録生成には音声・動画・画像を送信しません。他の工程には従来の設定を適用します。")
                    .foregroundStyle(.secondary)
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
        .task(id: MinutesModelForm(selection: config.minutesModel).catalogReadIdentity) {
            guard let selection = config.minutesModel else { return }
            await catalog.loadCachedCatalog(connectionID: selection.connectionID, selectedModelID: selection.modelID)
        }
    }
}
