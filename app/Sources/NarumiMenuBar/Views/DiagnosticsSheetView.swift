import NarumiMenuBarCore
import SwiftUI

/// 診断 sheet: `get_server_info` (diagnostics + capabilities), `rebuild_catalog`, and the
/// app-specific process actions injected by the AppDelegate (server restart / log / Sparkle).
struct DiagnosticsSheetView: View {
    @ObservedObject var model: MainWindowModel
    @State private var rebuilding = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("診断").font(.title3.bold())
                .padding(12)
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    RecordingPermissionsSection(model: model)
                    serverSection
                    diagnosticsSection
                    catalogSection
                    processSection
                }
                .padding(12)
            }
            Divider()
            HStack {
                Spacer()
                Button("閉じる") {
                    model.showDiagnosticsSheet = false
                }
                .keyboardShortcut(.cancelAction)
            }
            .padding(12)
        }
        .frame(width: 660, height: 680)
        .task {
            await model.refreshRecordingPermissions()
            await model.refreshServerWideData()
        }
    }

    @ViewBuilder
    private var serverSection: some View {
        GroupBox("サーバー") {
            if let info = model.serverInfo {
                grid([
                    ("名前", info.name),
                    ("サーバー版", info.serverVersion),
                    ("契約版", info.contractVersion),
                    ("録画", info.capabilities.recording ? "可能" : "不可"),
                    ("文字起こしエンジン", info.capabilities.transcriptionEngines.joined(separator: ", ")),
                    ("話者分離エンジン", info.capabilities.diarizationEngines.joined(separator: ", ")),
                    ("LLM プロバイダ", info.capabilities.llmProviders.joined(separator: ", ")),
                    ("エクスポート先", info.capabilities.exportDestinations.joined(separator: ", ")),
                ])
            } else {
                Text("サーバーに接続できていません")
                    .foregroundStyle(.orange)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    @ViewBuilder
    private var diagnosticsSection: some View {
        if let diag = model.serverInfo?.diagnostics {
            GroupBox("環境") {
                grid([
                    ("ffmpeg", diag.ffmpeg.map { "\($0.path)（\($0.version)）" } ?? "見つかりません"),
                    ("ffprobe", diag.ffprobe.map { "\($0.path)（\($0.version)）" } ?? "見つかりません"),
                    ("データルート", diag.dataRoot),
                    ("会議バンドル", diag.meetingsRoot),
                    ("カタログ DB", diag.catalogPath),
                    ("recorder", diag.recorderPath ?? "見つかりません"),
                    ("契約ディレクトリ", diag.contractsDir),
                ])
            }
        }
    }

    private var catalogSection: some View {
        GroupBox("カタログ") {
            VStack(alignment: .leading, spacing: 6) {
                Text("narumi.db はバンドルから再構築できる索引です。検索結果がおかしいときに再構築してください。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                HStack {
                    Button("カタログを再構築") {
                        rebuilding = true
                        Task {
                            await model.rebuildCatalog()
                            rebuilding = false
                        }
                    }
                    .disabled(rebuilding)
                    if rebuilding {
                        ProgressView().controlSize(.small)
                    }
                }
                if let result = model.rebuildResult {
                    Text("会議 \(result.meetings) 件 / セグメント \(result.segments) 件")
                        .font(.caption)
                    ForEach(Array(result.errors.enumerated()), id: \.offset) { _, error in
                        Text(error)
                            .font(.caption)
                            .foregroundStyle(.red)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var processSection: some View {
        GroupBox("アプリ・プロセス（MCP 外の操作）") {
            HStack {
                Button("サーバーを再起動") {
                    model.hostActions.restartServer()
                }
                .disabled(model.permissionProcessControlsBlocked || !model.desktopSession.serverState.canRestart)
                Button("サーバーログを開く") {
                    model.hostActions.openServerLog()
                }
                Button("アップデートを確認…") {
                    model.hostActions.checkForUpdates()
                }
                .disabled(model.permissionProcessControlsBlocked || model.desktopSession.recording.active)
                Spacer()
            }
        }
    }

    private func grid(_ rows: [(String, String)]) -> some View {
        Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 12, verticalSpacing: 4) {
            ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                GridRow {
                    Text(row.0)
                        .foregroundStyle(.secondary)
                        .gridColumnAlignment(.trailing)
                    Text(row.1)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .font(.callout)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
