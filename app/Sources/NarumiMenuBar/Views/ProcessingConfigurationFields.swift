import NarumiMenuBarCore
import SwiftUI

/// Meeting and profile editors use the same independent ASR/minutes selections and policy.
struct ProcessingConfigurationFields: View {
    @Binding var form: ProcessingConfigurationForm
    let capabilities: ServerCapabilities?
    let contractVersion: String?
    let catalog: MinutesModelCatalogStore
    let transcriptionCatalog: TranscriptionModelCatalogStore
    var isProfile = false
    @Binding var minutesValidationMessage: String?

    var body: some View {
        capabilityPicker("ローカル文字起こしエンジン", selection: $form.transcriptionEngine,
                         options: capabilities?.transcriptionEngines ?? [])
        Text("API 音声認識を選ばない場合に使います。API の選択を解除すると、この保存済み設定に戻ります。")
            .font(.caption).foregroundStyle(.secondary)
        capabilityPicker("話者分離エンジン", selection: $form.diarizationEngine,
                         options: capabilities?.diarizationEngines ?? [])
        capabilityPicker("従来の LLM プロバイダ", selection: $form.llmProvider,
                         options: (capabilities?.llmProviders ?? []).filter { !["codex-app-server", "openai-api"].contains($0) })
        Text("発話統合・画像解析などは従来の LLM 設定を使います。音声認識とテキスト議事録の接続・モデルは、下で別々に選びます。")
            .font(.caption).foregroundStyle(.secondary)
        Picker("外部送信ポリシー", selection: $form.externalSendPolicy) {
            Text(isProfile ? "（設定しない／既存値を維持）" : "（変更しない）").tag("")
            Text("local_only — ローカル完結").tag("local_only")
            Text("subscription_ok — サブスク LLM 可").tag("subscription_ok")
            Text("api_ok — 従量 API も可").tag("api_ok")
        }
        Text("OpenAI API の音声認識と OpenAI API・Anthropic API の議事録生成には api_ok、Codex には subscription_ok または api_ok の明示が必要です。Ollama は local_only で利用できます。モデルの選択だけでは送信許可を変更しません。")
            .font(.caption).foregroundStyle(.secondary)
        TextField("言語（auto / ja / en など）", text: $form.language)
        Text("API 音声認識では auto（自動判定）または小文字2文字の ISO 639-1 コードを使います。空欄は現在の値を維持し、未設定なら ja です。")
            .font(.caption).foregroundStyle(.secondary)
        TranscriptionModelSelectionView(
            form: $form.transcriptionModel, catalog: transcriptionCatalog,
            externalSendPolicy: form.effectiveExternalSendPolicy, language: form.effectiveLanguage, isProfile: isProfile)
        if capabilities?.supportsMinutesEnsembleWire(contractVersion: contractVersion) == true
            || form.minutesGenerationMode == .ensemble {
            MinutesGenerationSelectionView(
                form: $form, capabilities: capabilities, contractVersion: contractVersion, catalog: catalog,
                externalSendPolicy: form.effectiveExternalSendPolicy, isProfile: isProfile,
                validationMessage: $minutesValidationMessage)
        } else {
            MinutesModelSelectionView(
                form: $form.minutesModel, catalog: catalog,
                externalSendPolicy: form.effectiveExternalSendPolicy, isProfile: isProfile)
        }
        TextField("自分の名前（マイク話者の表示名）", text: $form.selfName)
        Text("空欄で名前を解除します。API 音声認識へ既知話者の名前や参照音声は送りません。")
            .font(.caption).foregroundStyle(.secondary)
        VStack(alignment: .leading) {
            Text("語彙ヒント（1 行 1 語）")
            TextEditor(text: $form.vocabHintsText)
                .font(.body)
                .frame(minHeight: 70)
                .overlay(RoundedRectangle(cornerRadius: 4).stroke(Color.secondary.opacity(0.3)))
        }
        Text("語彙ヒントは空欄で構いません。ローカル文字起こし・発話統合で使い、API 音声認識には送信しません。")
            .font(.caption).foregroundStyle(.secondary)
    }

    private func capabilityPicker(_ title: String, selection: Binding<String>, options: [String]) -> some View {
        Picker(title, selection: selection) {
            Text(isProfile ? "（設定しない／既存値を維持）" : "（変更しない）").tag("")
            let values = options.contains(selection.wrappedValue) || selection.wrappedValue.isEmpty
                ? options : options + [selection.wrappedValue]
            ForEach(values, id: \.self) { Text($0).tag($0) }
        }
    }
}
