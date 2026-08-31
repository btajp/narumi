import NarumiMenuBarCore
import SwiftUI

/// Displays the saved audio selection whose full configuration is checked before submission.
struct TranscriptionGenerationDisclosure: View {
    let config: MeetingConfig
    let catalog: TranscriptionModelCatalogStore

    private var selectedModel: ProviderModelDescriptor? {
        guard let selection = config.transcriptionModel,
            let response = catalog.catalogs[selection.connectionID],
            response.connectionRevision == selection.connectionRevision else { return nil }
        return response.models.first { $0.modelID == selection.modelID }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            if let selection = config.transcriptionModel {
                Text("OpenAI API で音声を文字起こし").font(.subheadline.bold())
                LabeledContent("接続", value: catalog.connection(selection.connectionID)?.displayName ?? selection.connectionID)
                Text("\(selection.connectionID) / rev \(selection.connectionRevision)")
                    .font(.caption2).foregroundStyle(.secondary)
                LabeledContent("音声認識モデル", value: selection.modelID)
                LabeledContent("言語", value: languageDescription)
                LabeledContent("再試行の確認番号", value: String(selection.cacheEpoch))
                LabeledContent("音声単価", value: ProviderDisplay.price(selectedModel?.billing.audioUSDPerMinute, unit: "分"))
                if let date = selectedModel?.availabilityExpiresOn {
                    LabeledContent("提供終了予定日", value: date)
                    Text("アプリでは UTC の日付が予定日以降になったモデルを選択できません。公式の終了時刻を示すものではありません。")
                        .foregroundStyle(.secondary)
                }
                Text("送信先: https://api.openai.com/v1/audio/transcriptions。api_ok の許可に基づき音声を送信し、ChatGPT の利用枠とは別に API 利用料が発生する場合があります。")
                Text("送信する音声: 前処理済みのマイク音声・システム音声を、それぞれ最長10分の区間に分けて送ります。同じ会議でも2トラック分の利用となる場合があります。")
                Text("会議名・ファイルパス・登録語彙・話者名の指定・参照音声は追加送信しません。音声自体には会話中の名前などが含まれる場合があります。登録語彙は後段の発話統合で使います。")
                    .foregroundStyle(.secondary)
                if selection.modelID == "gpt-4o-transcribe-diarize" {
                    Text("匿名話者ラベルはトラック・区間ごとに扱い、実名や別区間の同一人物と推測しません。既存の話者分離とマイク本人設定は維持します。")
                        .foregroundStyle(.secondary)
                }
                Text("同じ入力・設定で確認済みの成功区間は再利用します。結果不明の区間は自動再送せず、対象区間と重複課金の可能性を確認する別の再送操作が必要です。")
                    .foregroundStyle(.secondary)
                Text("取消してもサービス側の処理や課金の停止は保証できません。文字起こし完了前の匿名話者を実名とは扱いません。")
                    .foregroundStyle(.secondary)
                if let message = catalog.validationMessage(for: ProcessingConfigurationForm(config: config)) {
                    Text(message).foregroundStyle(.orange)
                }
            } else {
                Text("文字起こし: \(config.transcriptionEngine ?? "auto")（ローカル設定）")
                    .foregroundStyle(.secondary)
            }
            Text("外部送信ポリシー: \(config.externalSendPolicy ?? "local_only")")
        }
        .font(.caption)
        .fixedSize(horizontal: false, vertical: true)
        .task(id: TranscriptionModelForm(selection: config.transcriptionModel).catalogReadIdentity
              + "/" + catalog.supportedProviders.joined(separator: ",")) {
            guard let selection = config.transcriptionModel else { return }
            await catalog.loadCachedCatalog(connectionID: selection.connectionID, selectedModelID: selection.modelID)
        }
    }

    private var languageDescription: String {
        let language = config.language ?? "ja"
        return language == "auto" ? "auto（API の言語指定を省略）" : language
    }
}
