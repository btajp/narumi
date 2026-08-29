import NarumiMenuBarCore
import SwiftUI

struct MinutesEnsembleSelectionView: View {
    @Binding var form: MinutesEnsembleForm
    let capabilities: ServerCapabilities?
    let contractVersion: String?
    let sharedCatalog: MinutesModelCatalogStore
    let externalSendPolicy: String
    let isProfile: Bool
    @Binding var validationMessage: String?
    @State private var participantMessages: [String: String] = [:]

    private let synthesizerID = "synthesizer"
    private var unavailableReason: String? {
        MinutesEnsembleExecutionAvailability.unavailableReason(
            capabilities: capabilities, contractVersion: contractVersion,
            supportedProviders: sharedCatalog.supportedProviders)
    }
    private var executionAvailable: Bool { unavailableReason == nil }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let unavailableReason {
                Label(unavailableReason, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption).foregroundStyle(.orange)
            }
            Text("生成担当ごとに案を作り、統合担当が根拠付きで一つの議事録へまとめます。表示名と並び順だけの変更では新しい生成を要求しません。")
                .font(.caption).foregroundStyle(.secondary)
            ForEach($form.generators) { $generator in
                generatorCard(generator: $generator)
            }
            Button {
                _ = form.addGenerator()
            } label: {
                Label("生成担当を追加", systemImage: "plus")
            }
            .disabled(!executionAvailable || form.generators.count >= 4)

            GroupBox("統合担当") {
                participantEditor(
                    id: synthesizerID, form: $form.synthesizer)
            }
            .disabled(!executionAvailable)

            disclosureSummary
            if let message = combinedValidationMessage {
                Label(message, systemImage: "exclamationmark.triangle")
                    .font(.caption).foregroundStyle(.orange)
            }
            Text(isProfile
                 ? "保存だけでは会議内容を送信しません。新しい録画・取り込みで自動処理する場合も、外部送信を含む構成は処理前の確認対象です。"
                 : "保存だけでは会議内容を送信しません。実行時に全担当の接続・モデル・送信先・費用区分を改めて確認します。")
                .font(.caption).foregroundStyle(.secondary)
        }
        .onAppear { publishValidation() }
        .onChange(of: form) { publishValidation() }
        .onChange(of: participantMessages) { publishValidation() }
        .onChange(of: unavailableReason) { publishValidation() }
        .onDisappear { validationMessage = nil }
    }

    private func generatorCard(
        generator: Binding<MinutesEnsembleGeneratorForm>
    ) -> some View {
        let id = generator.wrappedValue.id
        return GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                TextField("表示名（1〜80文字）", text: generator.label)
                participantEditor(id: id, form: generator.model)
            }
        } label: {
            HStack {
                Text(generator.wrappedValue.label.isEmpty ? "生成担当" : generator.wrappedValue.label)
                Spacer()
                Button { move(id: id, offset: -1) } label: { Image(systemName: "arrow.up") }
                    .help("上へ移動").disabled(!executionAvailable || index(of: id) == 0)
                Button { move(id: id, offset: 1) } label: { Image(systemName: "arrow.down") }
                    .help("下へ移動")
                    .disabled(!executionAvailable || index(of: id) == form.generators.count - 1)
                Button(role: .destructive) { _ = form.removeGenerator(id: id) } label: {
                    Image(systemName: "minus.circle")
                }
                .help("生成担当を削除")
                .disabled(!executionAvailable || form.generators.count <= 2)
            }
        }
        .disabled(!executionAvailable)
    }

    private func participantEditor(id: String, form: Binding<MinutesModelForm>) -> some View {
        IndependentMinutesParticipantView(
            form: form, sharedCatalog: sharedCatalog, externalSendPolicy: externalSendPolicy,
            isProfile: isProfile
        ) { message in
            if let message { participantMessages[id] = message }
            else { participantMessages[id] = nil }
        }
        .id(id)
    }

    private var disclosureSummary: some View {
        GroupBox("保存前の送信先・費用区分") {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(form.generators) { generator in
                    disclosureRow(title: generator.label, model: generator.model)
                }
                disclosureRow(title: "統合担当", model: form.synthesizer)
            }
        }
    }

    private func disclosureRow(title: String, model: MinutesModelForm) -> some View {
        let connection = sharedCatalog.connection(model.connectionID)
        let matchesSavedConnection = connection?.revision == model.connectionRevision
            && connection?.providerID.rawValue == model.provider
        let connectionName = matchesSavedConnection ? connection?.displayName : nil
        let endpoint = matchesSavedConnection ? connection?.endpoint : nil
        let revision = model.connectionRevision.map { " / rev \($0)" } ?? ""
        return HStack(alignment: .top) {
            Text(title.isEmpty ? "生成担当" : title).frame(width: 90, alignment: .leading)
            if let disclosure = MinutesParticipantDisclosure.make(provider: model.provider, endpoint: endpoint) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("\(disclosure.providerName) / \(model.modelID.isEmpty ? "モデル未選択" : model.modelID)")
                    Text("接続: \(connectionName ?? (model.connectionID.isEmpty ? "未選択" : model.connectionID))\(revision)")
                    Text("送信先: \(disclosure.destination)")
                    Text("費用区分: \(ProviderDisplay.billing(disclosure.billing))")
                }
            } else {
                Text("接続・モデル未選択").foregroundStyle(.secondary)
            }
            Spacer()
        }
        .font(.caption)
    }

    private var combinedValidationMessage: String? {
        if let unavailableReason { return unavailableReason }
        for generator in form.generators {
            if let message = participantMessages[generator.id] {
                return "\(generator.label.isEmpty ? "生成担当" : generator.label): \(message)"
            }
        }
        if let message = participantMessages[synthesizerID] { return "統合担当: \(message)" }
        return form.structuralValidationMessage
    }

    private func publishValidation() {
        if validationMessage != combinedValidationMessage { validationMessage = combinedValidationMessage }
    }

    private func index(of id: String) -> Int { form.generators.firstIndex { $0.id == id } ?? -1 }
    private func move(id: String, offset: Int) {
        let source = index(of: id), destination = source + offset
        guard form.generators.indices.contains(source), form.generators.indices.contains(destination) else { return }
        form.generators.swapAt(source, destination)
    }
}

@MainActor
private struct IndependentMinutesParticipantView: View {
    @Binding var form: MinutesModelForm
    let sharedCatalog: MinutesModelCatalogStore
    let externalSendPolicy: String
    let isProfile: Bool
    let validationChanged: (String?) -> Void
    @State private var catalog: MinutesModelCatalogStore

    init(
        form: Binding<MinutesModelForm>, sharedCatalog: MinutesModelCatalogStore,
        externalSendPolicy: String, isProfile: Bool,
        validationChanged: @escaping (String?) -> Void
    ) {
        _form = form
        self.sharedCatalog = sharedCatalog
        self.externalSendPolicy = externalSendPolicy
        self.isProfile = isProfile
        self.validationChanged = validationChanged
        _catalog = State(initialValue: sharedCatalog.independentStore())
    }

    var body: some View {
        MinutesModelSelectionView(
            form: $form, catalog: catalog, externalSendPolicy: externalSendPolicy,
            isProfile: isProfile, showsModePicker: false, validationChanged: validationChanged)
            .onChange(of: sharedCatalog.supportedProviders) {
                catalog.setSupportedProviders(sharedCatalog.supportedProviders)
            }
    }
}
