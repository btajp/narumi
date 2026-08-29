import NarumiMenuBarCore
import SwiftUI

/// Meeting and profile editors use the same minutes selection and policy controls.
struct ProcessingConfigurationFields: View {
    @Binding var form: ProcessingConfigurationForm
    let capabilities: ServerCapabilities?
    let catalog: MinutesModelCatalogStore
    var isProfile = false

    var body: some View {
        capabilityPicker("文字起こしエンジン", selection: $form.transcriptionEngine,
                         options: capabilities?.transcriptionEngines ?? [])
        capabilityPicker("話者分離エンジン", selection: $form.diarizationEngine,
                         options: capabilities?.diarizationEngines ?? [])
        capabilityPicker("従来の LLM プロバイダ", selection: $form.llmProvider,
                         options: (capabilities?.llmProviders ?? []).filter { !["codex-app-server", "openai-api"].contains($0) })
        Text("発話統合・画像解析などは従来の LLM 設定を使います。下の接続・モデル選択はテキスト議事録の生成だけに適用し、文字起こしエンジンは変更しません。")
            .font(.caption).foregroundStyle(.secondary)
        Picker("外部送信ポリシー", selection: $form.externalSendPolicy) {
            Text(isProfile ? "（設定しない／既存値を維持）" : "（変更しない）").tag("")
            Text("local_only — ローカル完結").tag("local_only")
            Text("subscription_ok — サブスク LLM 可").tag("subscription_ok")
            Text("api_ok — 従量 API も可").tag("api_ok")
        }
        Text("OpenAI API・Anthropic API の議事録生成には api_ok、Codex には subscription_ok または api_ok の明示が必要です。Ollama は local_only で利用できます。プロバイダの選択だけでは送信許可を変更しません。")
            .font(.caption).foregroundStyle(.secondary)
        MinutesModelSelectionView(
            form: $form.minutesModel, catalog: catalog,
            externalSendPolicy: form.effectiveExternalSendPolicy, isProfile: isProfile)
        TextField("言語（ja / en など）", text: $form.language)
        TextField("自分の名前（マイク話者の表示名）", text: $form.selfName)
        VStack(alignment: .leading) {
            Text("語彙ヒント（1 行 1 語）")
            TextEditor(text: $form.vocabHintsText)
                .font(.body)
                .frame(minHeight: 70)
                .overlay(RoundedRectangle(cornerRadius: 4).stroke(Color.secondary.opacity(0.3)))
        }
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
