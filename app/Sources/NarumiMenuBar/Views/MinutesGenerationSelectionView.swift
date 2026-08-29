import NarumiMenuBarCore
import SwiftUI

struct MinutesGenerationSelectionView: View {
    @Binding var form: ProcessingConfigurationForm
    let capabilities: ServerCapabilities?
    let contractVersion: String?
    let catalog: MinutesModelCatalogStore
    let externalSendPolicy: String
    let isProfile: Bool
    @Binding var validationMessage: String?

    private var unavailableReason: String? {
        MinutesEnsembleExecutionAvailability.unavailableReason(
            capabilities: capabilities, contractVersion: contractVersion,
            supportedProviders: catalog.supportedProviders)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Picker("議事録の生成方法", selection: Binding(
                get: { form.minutesGenerationMode },
                set: { form.selectMinutesGenerationMode($0) })) {
                ForEach(MinutesGenerationMode.allCases, id: \.self) { mode in
                    Text(mode.title).tag(mode)
                        .disabled(mode == .ensemble && unavailableReason != nil
                            && form.minutesGenerationMode != .ensemble)
                }
            }
            if let unavailableReason, form.minutesGenerationMode != .ensemble {
                Label(unavailableReason, systemImage: "exclamationmark.triangle")
                    .font(.caption).foregroundStyle(.orange)
            }
            switch form.minutesGenerationMode {
            case .legacy:
                Text("議事録にも従来の LLM 設定を使います。保存すると個別のモデル選択を解除します。")
                    .font(.caption).foregroundStyle(.secondary)
            case .single:
                MinutesModelSelectionView(
                    form: $form.minutesModel, catalog: catalog,
                    externalSendPolicy: externalSendPolicy, isProfile: isProfile,
                    showsModePicker: false)
            case .ensemble:
                MinutesEnsembleSelectionView(
                    form: $form.minutesEnsemble, capabilities: capabilities, contractVersion: contractVersion,
                    sharedCatalog: catalog,
                    externalSendPolicy: externalSendPolicy, isProfile: isProfile,
                    validationMessage: $validationMessage)
            }
        }
        .onAppear { clearEnsembleValidationWhenUnused() }
        .onChange(of: form.minutesGenerationMode) { clearEnsembleValidationWhenUnused() }
    }

    private func clearEnsembleValidationWhenUnused() {
        if form.minutesGenerationMode != .ensemble { validationMessage = nil }
    }
}
