import NarumiMenuBarCore
import SwiftUI

/// Contract 6 local read-only history. This view has no generation, retry, or export action.
struct ProcessingRunHistoryView: View {
    @ObservedObject var model: MainWindowModel
    @Environment(\.dismiss) private var dismiss

    private var pollContext: ProcessingRunHistoryPollContext {
        .init(
            meetingID: model.selectedMeetingID,
            scope: model.selectedMeetingScope,
            connectionGeneration: model.desktopSession.connectionGeneration,
            supported: model.supportsProcessingRunHistory)
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text("生成履歴").font(.title2.bold())
                    Text("複数案生成のrun・部分案・統合結果をローカルから読み取ります。")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Button {
                    Task { await refreshCurrentContext() }
                } label: {
                    Label("再読み込み", systemImage: "arrow.clockwise")
                }
                .disabled(model.processingRunHistory.isLoadingList)
                Button("閉じる") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            .padding(12)
            Divider()
            if model.supportsProcessingRunHistory {
                HSplitView {
                    runList.frame(minWidth: 250, idealWidth: 285, maxWidth: 340)
                    runDetail.frame(minWidth: 520, maxWidth: .infinity)
                }
            } else {
                ContentUnavailableView(
                    "生成履歴を利用できません", systemImage: "clock.badge.exclamationmark",
                    description: Text("このサーバー契約では複数案の保存済みrunを読み取れません。"))
            }
            Divider()
            Label("この画面は保存済みの公開成果をローカルで読むだけです。生成・再試行・外部送信は行いません。",
                  systemImage: "lock.shield")
                .font(.caption).foregroundStyle(.secondary).padding(8)
        }
        .frame(minWidth: 860, minHeight: 620)
        .task(id: pollContext) {
            guard pollContext.supported, pollContext.meetingID != nil else { return }
            await refreshCurrentContext()
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(5)) } catch { return }
                guard !Task.isCancelled, pollContext == self.pollContext else { return }
                await refreshCurrentContext()
            }
        }
        .onDisappear { model.processingRunHistory.invalidate() }
    }

    private var runList: some View {
        VStack(spacing: 0) {
            if let error = model.processingRunHistory.listErrorMessage,
                !model.processingRunHistory.runs.isEmpty {
                Label(error, systemImage: "exclamationmark.triangle")
                    .font(.caption).foregroundStyle(.orange)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(8)
                Divider()
            }
            if model.processingRunHistory.isLoadingList && model.processingRunHistory.runs.isEmpty {
                ProgressView("生成履歴を読み込み中…").frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let error = model.processingRunHistory.listErrorMessage,
                model.processingRunHistory.runs.isEmpty {
                ContentUnavailableView(
                    "生成履歴を読み込めません", systemImage: "exclamationmark.triangle",
                    description: Text(error))
            } else if model.processingRunHistory.runs.isEmpty {
                ContentUnavailableView(
                    "複数案の生成履歴はありません", systemImage: "clock",
                    description: Text("単独モデルの議事録に架空のrunは作りません。"))
            } else {
                List {
                    ForEach(model.processingRunHistory.runs) { run in
                        Button {
                            Task { await model.processingRunHistory.selectRun(run.runID) }
                        } label: {
                            ProcessingRunSummaryRow(
                                run: run,
                                selected: model.processingRunHistory.selectedRunID == run.runID)
                        }
                        .buttonStyle(.plain)
                        .listRowBackground(
                            model.processingRunHistory.selectedRunID == run.runID
                                ? Color.accentColor.opacity(0.14) : Color.clear)
                    }
                    if model.processingRunHistory.nextCursor != nil {
                        Button(model.processingRunHistory.isLoadingList ? "続きを読み込み中…" : "さらに読み込む") {
                            Task { await model.processingRunHistory.loadMore() }
                        }
                        .disabled(model.processingRunHistory.isLoadingList)
                    }
                }
                .listStyle(.sidebar)
            }
        }
    }

    @ViewBuilder private var runDetail: some View {
        if model.processingRunHistory.isLoadingRun && model.processingRunHistory.run == nil {
            ProgressView("runの詳細を読み込み中…").frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if let error = model.processingRunHistory.runErrorMessage,
            model.processingRunHistory.run == nil {
            ContentUnavailableView(
                "runを読み込めません", systemImage: "exclamationmark.triangle",
                description: Text(error))
        } else if let run = model.processingRunHistory.run {
            VStack(spacing: 0) {
                if let error = model.processingRunHistory.runErrorMessage {
                    Label(error + " 前回確認できた状態を表示しています。", systemImage: "exclamationmark.triangle")
                        .font(.caption).foregroundStyle(.orange)
                        .frame(maxWidth: .infinity, alignment: .leading).padding(8)
                    Divider()
                }
                ProcessingRunDetailContent(run: run, store: model.processingRunHistory)
                    .id(run.runID)
            }
        } else {
            ContentUnavailableView("runを選択してください", systemImage: "list.bullet.rectangle")
        }
    }

    private func refreshCurrentContext() async {
        guard model.supportsProcessingRunHistory,
            let meetingID = model.selectedMeetingID else { return }
        let scope = model.selectedMeetingScope
        let generation = model.desktopSession.connectionGeneration
        await model.processingRunHistory.refresh(
            meetingID: meetingID, scope: scope, connectionGeneration: generation)
    }
}

private struct ProcessingRunHistoryPollContext: Hashable {
    let meetingID: String?
    let scope: String?
    let connectionGeneration: UInt64
    let supported: Bool
}

private struct ProcessingRunSummaryRow: View {
    let run: ProcessingRunSummary
    let selected: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                Label(run.status.japaneseLabel, systemImage: run.status.symbolName)
                    .foregroundStyle(run.status.tint)
                Spacer()
                if let version = run.publishedVersion {
                    Text("議事録 v\(version)").font(.caption2)
                }
            }
            Text(NarumiFormat.jstDateTime(run.createdAt)).font(.caption).foregroundStyle(.secondary)
            HStack(spacing: 10) {
                Text("生成担当 \(run.completedGenerators)/\(run.generatorCount)")
                Text("試行 \(run.attemptsUsed)/\(run.attemptLimit)")
                if run.blockedCalls > 0 { Text("結果不明 \(run.blockedCalls)").foregroundStyle(.orange) }
            }
            .font(.caption2)
        }
        .padding(.vertical, 4)
        .contentShape(Rectangle())
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

private struct ProcessingRunDetailContent: View {
    let run: ProcessingRun
    let store: ProcessingRunHistoryStore
    @State private var expandedDrafts: Set<String> = []

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                runHeader
                Divider()
                nodeProgress
                Divider()
                drafts
                Divider()
                synthesis
            }
            .padding(14)
        }
    }

    private var runHeader: some View {
        GroupBox("runの状態") {
            VStack(alignment: .leading, spacing: 6) {
                LabeledContent("状態") {
                    Label(run.status.japaneseLabel, systemImage: run.status.symbolName)
                        .foregroundStyle(run.status.tint)
                }
                LabeledContent("生成試行", value: "\(run.attemptsUsed) / \(run.attemptLimit)")
                LabeledContent("結果不明", value: "\(run.blocked.count) 件")
                LabeledContent("最終更新", value: NarumiFormat.jstDateTime(run.updatedAt))
                if let version = run.publishedVersion {
                    LabeledContent("公開済み議事録", value: "v\(version)")
                } else {
                    LabeledContent("公開済み議事録", value: "なし")
                }
                if let error = run.error {
                    Label(error.message, systemImage: "exclamationmark.triangle")
                        .font(.caption).foregroundStyle(.orange)
                }
                if !run.blocked.isEmpty {
                    Text("結果不明の呼び出しは自動で再送しません。再試行操作はこの画面にはありません。")
                        .font(.caption).foregroundStyle(.orange)
                }
            }
        }
    }

    private var nodeProgress: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("工程の進捗").font(.headline)
            if run.nodes.isEmpty {
                Text("まだ工程は開始されていません。")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                LazyVStack(alignment: .leading, spacing: 8) {
                    ForEach(run.nodes) { node in
                        HStack(alignment: .top, spacing: 8) {
                            Image(systemName: node.status.symbolName)
                                .foregroundStyle(node.status.tint).frame(width: 16)
                            VStack(alignment: .leading, spacing: 2) {
                                HStack {
                                    Text(nodeTitle(node)).bold()
                                    Text(node.phase.japaneseLabel).foregroundStyle(.secondary)
                                    if node.reused { Text("再利用").foregroundStyle(.blue) }
                                }
                                .font(.caption)
                                if let error = node.error {
                                    Text(error.message).font(.caption2).foregroundStyle(.orange)
                                }
                            }
                            Spacer()
                            Text(node.status.japaneseLabel).font(.caption).foregroundStyle(node.status.tint)
                        }
                    }
                }
            }
        }
    }

    private var drafts: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("生成担当ごとの部分案").font(.headline)
            ForEach(run.ensemble.generators) { generator in
                draftCard(generator)
            }
        }
    }

    @ViewBuilder private func draftCard(_ generator: MinutesEnsembleGenerator) -> some View {
        let slot = run.canonicalSlots.first { $0.generatorID == generator.id }
        let artifactID = slot?.draftArtifactID
        GroupBox(generator.label) {
            VStack(alignment: .leading, spacing: 7) {
                SelectionSummary(selection: generator.selection)
                if let artifactID {
                    let sharingCount = run.canonicalSlots.filter { $0.draftArtifactID == artifactID }.count
                    if sharingCount > 1 {
                        Label("\(sharingCount)担当で成果を共有（独立した別実行ではありません）", systemImage: "link")
                            .font(.caption).foregroundStyle(.blue)
                    }
                    if store.artifactFailures.contains(artifactID) {
                        Label("部分案を読み込めません。runを再読み込みしてください。", systemImage: "exclamationmark.triangle")
                            .font(.caption).foregroundStyle(.orange)
                    } else if store.artifacts[artifactID] == nil {
                        ProgressView("部分案の索引を読み込み中…").font(.caption)
                    } else {
                        DisclosureGroup("案の内容", isExpanded: draftExpansion(artifactID)) {
                            draftDocuments(artifactID)
                        }
                    }
                } else {
                    Text("この担当の検証済み部分案はまだありません。")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }

    @ViewBuilder private func draftDocuments(_ artifactID: String) -> some View {
        let documents = store.draftDocumentArtifacts(artifactID: artifactID)
        let loading = store.loadingDraftDocumentCount(artifactID: artifactID)
        let failures = store.failedDraftDocumentCount(artifactID: artifactID)
        if loading > 0 {
            ProgressView("案の本文を読み込み中…").font(.caption).padding(.vertical, 4)
        }
        if failures > 0 {
            Label("本文の一部（\(failures)件）を読み込めません。閉じてから再度開くと再確認できます。",
                  systemImage: "exclamationmark.triangle")
                .font(.caption).foregroundStyle(.orange)
        }
        ForEach(Array(documents.enumerated()), id: \.element.id) { index, artifact in
            VStack(alignment: .leading, spacing: 6) {
                if documents.count > 1 { Text("部分 \(index + 1)").font(.caption.bold()) }
                ArtifactGenerationSummary(artifact: artifact)
                if case .draftChunk(let document) = artifact.payload {
                    EnsembleDocumentView(document: document)
                }
            }
            if index < documents.count - 1 { Divider() }
        }
        if documents.isEmpty && loading == 0 && failures == 0
            && store.missingDraftDocumentCount(artifactID: artifactID) == 0 {
            Text("表示できる本文はありません。").font(.caption).foregroundStyle(.secondary)
        }
    }

    private var synthesis: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("最終統合成果").font(.headline)
            SelectionSummary(selection: run.ensemble.synthesizer)
            if let artifactID = run.synthesisArtifactID {
                if let artifact = store.artifacts[artifactID],
                    case .synthesis(let document) = artifact.payload {
                    ArtifactGenerationSummary(artifact: artifact)
                    EnsembleDocumentView(document: document)
                } else if store.artifactFailures.contains(artifactID) {
                    Label("最終統合成果を読み込めません。runを再読み込みしてください。", systemImage: "exclamationmark.triangle")
                        .font(.caption).foregroundStyle(.orange)
                } else {
                    ProgressView("最終統合成果を読み込み中…").font(.caption)
                }
            } else {
                Text("最終統合成果はまだありません。部分案は上で確認できます。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private func draftExpansion(_ artifactID: String) -> Binding<Bool> {
        Binding(
            get: { expandedDrafts.contains(artifactID) },
            set: { expanded in
                if expanded {
                    expandedDrafts.insert(artifactID)
                    Task { await store.loadDraftContents(artifactID: artifactID) }
                } else {
                    expandedDrafts.remove(artifactID)
                }
            })
    }

    private func nodeTitle(_ node: ProcessingNode) -> String {
        if let generatorID = node.generatorID,
            let generator = run.ensemble.generators.first(where: { $0.id == generatorID }) {
            return generator.label
        }
        return "統合担当"
    }
}

private struct SelectionSummary: View {
    let selection: MinutesModelSelection
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("\(ProcessingRunProviderLabel.name(selection.provider)) / \(selection.modelID)")
            Text("接続: \(selection.connectionID) / rev \(selection.connectionRevision) · 試行番号 \(selection.cacheEpoch)")
            Text("設定: \(selection.parameters.presentationSummary)")
        }
        .font(.caption).foregroundStyle(.secondary)
    }
}

private struct ArtifactGenerationSummary: View {
    let artifact: ProcessingArtifactPresentation

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 8) {
                Text(artifact.reused ? "再利用" : "実行済み")
                if let generation = artifact.generation {
                    Text("\(ProcessingRunProviderLabel.name(generation.provider)) / \(generation.modelID)")
                    Text("送信先: \(generation.dataDestination.japaneseLabel)")
                    Text("費用: \(generation.costClass.japaneseLabel)")
                }
            }
            .font(.caption.bold())
            if let generation = artifact.generation {
                Text("実行接続: \(generation.connectionID) / rev \(generation.connectionRevision) · 試行番号 \(generation.cacheEpoch)")
                    .font(.caption).foregroundStyle(.secondary)
                Text("実効設定: \(generation.effectiveParameters.presentationSummary) · 応答モデル: \(generation.returnedModel ?? "未提供")")
                    .font(.caption).foregroundStyle(.secondary)
                Text("観測usage: \(generation.usageSummary)")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                Text("生成メタデータは未提供です。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

private struct EnsembleDocumentView: View {
    let document: EnsembleDocument

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(document.claims) { claim in
                VStack(alignment: .leading, spacing: 2) {
                    Text(claim.kind.japaneseLabel).font(.caption2.bold()).foregroundStyle(.secondary)
                    Text(claim.text).textSelection(.enabled)
                    HStack(spacing: 8) {
                        if let owner = claim.owner { Text("担当: \(owner)") }
                        if let due = claim.due { Text("期限: \(due)") }
                        Text("根拠 \(claim.evidence.count)件")
                    }
                    .font(.caption2).foregroundStyle(.secondary)
                }
            }
            ForEach(document.questions) { question in
                VStack(alignment: .leading, spacing: 3) {
                    Label(question.kind.japaneseLabel, systemImage: "questionmark.bubble")
                        .font(.caption.bold()).foregroundStyle(.orange)
                    Text(question.text).textSelection(.enabled)
                    ForEach(Array(question.alternatives.enumerated()), id: \.offset) { _, alternative in
                        Text("• \(alternative.text)").font(.caption).textSelection(.enabled)
                    }
                }
            }
            if document.claims.isEmpty && document.questions.isEmpty {
                Text("主張・確認事項はありません。").font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(.leading, 6)
    }
}

private enum ProcessingRunProviderLabel {
    static func name(_ value: String) -> String {
        ProviderID(rawValue: value).map(ProviderDisplay.name) ?? value
    }
}

private extension ProcessingRunStatus {
    var japaneseLabel: String {
        switch self {
        case .prepared: "準備済み"
        case .running: "実行中"
        case .blocked: "結果不明で停止"
        case .succeeded: "完了"
        case .failed: "失敗"
        case .cancelled: "取消済み"
        case .interrupted: "中断"
        }
    }
    var symbolName: String {
        switch self {
        case .prepared: "clock"
        case .running: "progress.indicator"
        case .blocked: "exclamationmark.triangle"
        case .succeeded: "checkmark.circle.fill"
        case .failed: "xmark.circle.fill"
        case .cancelled: "slash.circle"
        case .interrupted: "pause.circle"
        }
    }
    var tint: Color {
        switch self {
        case .succeeded: .green
        case .blocked, .interrupted: .orange
        case .failed: .red
        default: .secondary
        }
    }
}

private extension ProcessingNodeStatus {
    var japaneseLabel: String {
        switch self {
        case .prepared: "待機"
        case .submitted: "送信済み"
        case .succeeded: "完了"
        case .reused: "再利用"
        case .failed: "失敗"
        case .unknown: "結果不明"
        case .cancelled: "取消済み"
        }
    }
    var symbolName: String {
        switch self {
        case .prepared: "circle"
        case .submitted: "arrow.up.circle"
        case .succeeded, .reused: "checkmark.circle.fill"
        case .failed: "xmark.circle.fill"
        case .unknown: "exclamationmark.triangle.fill"
        case .cancelled: "slash.circle"
        }
    }
    var tint: Color {
        switch self {
        case .succeeded, .reused: .green
        case .unknown: .orange
        case .failed: .red
        default: .secondary
        }
    }
}

private extension ProcessingNodePhase {
    var japaneseLabel: String {
        switch self { case .chunk: "分割"; case .reduce: "縮約"; case .final: "最終" }
    }
}

private extension ProcessingDataDestination {
    var japaneseLabel: String {
        switch self { case .local: "ローカル"; case .openai: "OpenAI"; case .anthropic: "Anthropic" }
    }
}

private extension ProcessingCostClass {
    var japaneseLabel: String {
        switch self { case .local: "ローカル"; case .subscription: "契約枠"; case .api: "API課金" }
    }
}

private extension EnsembleClaimKind {
    var japaneseLabel: String {
        switch self { case .agenda: "議題"; case .discussion: "議論"; case .decision: "決定"; case .action: "アクション" }
    }
}

private extension EnsembleQuestionKind {
    var japaneseLabel: String {
        switch self { case .conflict: "案の不一致"; case .missingContext: "確認が必要" }
    }
}

private extension MinutesModelSelection.Parameters {
    var presentationSummary: String {
        var values: [String] = []
        if let effort = reasoningEffort { values.append("推論量 \(effort)") }
        if let maximum = maxTokens { values.append("最大出力 \(maximum)") }
        return values.isEmpty ? "設定なし" : values.joined(separator: " / ")
    }
}

private extension ProcessingArtifactGenerationPresentation {
    var usageSummary: String {
        guard let usage else { return "未提供" }
        var values: [String] = []
        if let value = usage.inputTokens { values.append("入力 \(value)") }
        if let value = usage.outputTokens { values.append("出力 \(value)") }
        if let value = usage.totalTokens { values.append("合計 \(value)") }
        if let value = usage.cachedInputTokens { values.append("cache読取 \(value)") }
        if let value = usage.cacheWriteInputTokens { values.append("cache書込 \(value)") }
        if let value = usage.reasoningOutputTokens { values.append("推論出力 \(value)") }
        return values.isEmpty ? "未提供" : values.joined(separator: " / ")
    }
}
