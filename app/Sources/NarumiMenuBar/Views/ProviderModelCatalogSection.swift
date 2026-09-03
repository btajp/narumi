import NarumiMenuBarCore
import SwiftUI

struct ProviderModelCatalogSection: View {
    @Bindable var store: ProviderSettingsStore

    var body: some View {
        Section("モデル候補（確認のみ）") {
            if store.selectedConnection != nil {
                HStack {
                    Button("保存済み候補を表示") { Task { await store.loadModels() } }
                    Button("接続先から候補を更新") { Task { await store.loadModels(refresh: true) } }
                        .disabled(!store.canTest)
                }
                Text("更新時はモデル情報だけを接続先へ照会します。モデルを会議に適用したり、会議データを送信・生成したりしません。")
                    .font(.caption).foregroundStyle(.secondary)
                if let state = store.catalogState {
                    LabeledContent("候補一覧", value: ProviderDisplay.catalog(state))
                    LabeledContent("取得時刻", value: store.catalogFetchedAt ?? "未取得")
                }
                if store.models.isEmpty {
                    Text("モデル候補は表示されていません。ログイン・API キー・接続先を確認し、必要な場合だけ候補を更新してください。")
                        .font(.caption).foregroundStyle(.secondary)
                }
                ForEach(store.models, id: \.modelID) { model in
                    ProviderModelCandidateRow(model: model)
                }
                if store.nextModelCursor != nil {
                    Button("次の候補を表示") { Task { await store.loadModels(append: true) } }
                }
            } else {
                Text("接続を保存すると、プロバイダが返すモデル候補と確認できた能力・料金情報を表示できます。")
                    .foregroundStyle(.secondary)
            }
        }
    }
}

private struct ProviderModelCandidateRow: View {
    let model: ProviderModelDescriptor

    var body: some View {
        DisclosureGroup {
            VStack(alignment: .leading, spacing: 7) {
                Text(model.modelID).font(.caption.monospaced()).textSelection(.enabled)
                if let reason = ProviderDisplay.reason(model.reason) {
                    Text(reason).font(.caption).foregroundStyle(.orange)
                }
                LabeledContent("入力", value: modalities(model.inputModalities))
                LabeledContent("出力", value: modalities(model.outputModalities))
                LabeledContent("コンテキスト上限", value: model.contextWindow.map { "\($0) tokens" } ?? "未確認")
                LabeledContent("出力上限", value: model.maxOutputTokens.map { "\($0) tokens" } ?? "未確認")
                LabeledContent("時刻情報", value: timestampSupport)
                LabeledContent("モデルの固定版", value: model.resolvedRevision ?? "未確認")
                if let expiresOn = model.availabilityExpiresOn {
                    LabeledContent("提供終了予定日", value: expiresOn)
                    Text("UTC の日付が終了予定日に達した後は、候補から選択できません。公式の終了時刻を示すものではありません。")
                        .font(.caption).foregroundStyle(.secondary)
                }
                LabeledContent("課金区分", value: ProviderDisplay.billing(model.billing.kind))
                LabeledContent("入力単価", value: ProviderDisplay.price(model.billing.inputUSDPerMillionTokens, unit: "100万 tokens"))
                LabeledContent("出力単価", value: ProviderDisplay.price(model.billing.outputUSDPerMillionTokens, unit: "100万 tokens"))
                if model.inputModalities.contains(.audio) {
                    LabeledContent("音声単価", value: ProviderDisplay.price(model.billing.audioUSDPerMinute, unit: "分"))
                }
                LabeledContent("単価確認時刻", value: model.billing.fetchedAt ?? "未確認")
                if !model.parameterSchema.properties.isEmpty {
                    Text("確認済みパラメータ: " + model.parameterSchema.properties.keys.sorted().joined(separator: "、"))
                        .font(.caption)
                }
                Text("この一覧は候補情報です。議事録のモデルは会議の「設定」またはプロファイルの「議事録の生成方法」で選択して保存します。接続だけでは会議に適用しません。未検証の候補は、議事録モデルの選択画面で固定テスト文の送信を確認してから検証できます。")
                    .font(.caption).foregroundStyle(.secondary)
            }
            .font(.caption)
            .padding(.vertical, 6)
        } label: {
            VStack(alignment: .leading, spacing: 3) {
                Text(model.displayName)
                Text(model.availabilityExpired ? "提供終了予定日に到達（選択不可）" : ProviderDisplay.availability(model.availability))
                    .font(.caption)
                    .foregroundStyle(model.availability == .available && !model.availabilityExpired ? .green : .orange)
            }
        }
    }

    private func modalities(_ values: [ProviderModality]) -> String {
        values.isEmpty ? "未確認" : values.map(ProviderDisplay.modality).joined(separator: "、")
    }

    private var timestampSupport: String {
        switch model.timestampSupport {
        case .none: return "対応なし"
        case .segment: return "発話区間"
        case .word: return "単語"
        case .diarizedSegment: return "話者付き発話区間"
        }
    }
}
