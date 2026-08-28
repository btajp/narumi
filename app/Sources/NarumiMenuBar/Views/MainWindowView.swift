import NarumiMenuBarCore
import SwiftUI

/// The main window: 会議一覧 sidebar + meeting detail tabs, recording banner, toolbar with
/// 取り込み / プロファイル / AI 接続 / Gaia 接続 / 診断 sheets and the jobs indicator. Pure MCP client —
/// every data operation goes through `NarumiClient`.
struct MainWindowView: View {
    @ObservedObject var model: MainWindowModel
    @State private var showGaiaConnectionSheet = false
    @State private var showProviderConnectionsSheet = false

    var body: some View {
        VStack(spacing: 0) {
            RecordingBannerView(model: model)
            NavigationSplitView {
                SidebarView(model: model)
                    .navigationSplitViewColumnWidth(min: 240, ideal: 300)
            } detail: {
                if model.selectedMeetingID != nil {
                    MeetingDetailView(model: model)
                } else {
                    ContentUnavailableView(
                        "会議を録画して議事録をつくる", systemImage: "waveform",
                        description: Text("上の「録画開始」で会議を録画できます。保存済みの会議は左の一覧から、既存の録画は「取り込み」から開けます。"))
                }
            }
        }
        .frame(minWidth: 860, minHeight: 520)
        .disabled(model.desktopSession.installingUpdate || model.desktopSession.terminating)
        .toolbar {
            ToolbarItemGroup(placement: .primaryAction) {
                JobsToolbarButton(model: model)
                Button {
                    model.showImportSheet = true
                } label: {
                    Label("取り込み", systemImage: "square.and.arrow.down")
                }
                .help("既存録画ファイルの取り込み（import_recording）")
                Button {
                    model.showProfilesSheet = true
                } label: {
                    Label("プロファイル", systemImage: "person.crop.circle.badge.checkmark")
                }
                .help("既定プロファイルの管理")
                Button {
                    showProviderConnectionsSheet = true
                } label: {
                    Label("AI 接続", systemImage: "slider.horizontal.3")
                }
                .labelStyle(.titleAndIcon)
                .help("AI プロバイダの接続・認証・実行環境・モデル候補の確認")
                Button {
                    showGaiaConnectionSheet = true
                } label: {
                    Label("Gaia 接続", systemImage: "link")
                }
                .labelStyle(.titleAndIcon)
                .help("Gaia の接続 URL・API キー設定・接続テスト")
                Button {
                    model.showDiagnosticsSheet = true
                } label: {
                    Label("診断", systemImage: "stethoscope")
                }
                .labelStyle(.titleAndIcon)
                .help("録画の権限・サーバー診断・カタログ再構築")
            }
        }
        .sheet(isPresented: $model.showImportSheet) {
            ImportSheetView(model: model)
        }
        .sheet(isPresented: $model.showProfilesSheet) {
            ProfilesSheetView(model: model)
        }
        .sheet(isPresented: $showGaiaConnectionSheet) {
            GaiaConnectionSheetView(client: model.client)
        }
        .sheet(isPresented: $showProviderConnectionsSheet) {
            ProviderConnectionsSheetView(store: model.providerSettings)
        }
        .sheet(isPresented: $model.showDiagnosticsSheet) {
            DiagnosticsSheetView(model: model)
        }
        .alert(item: $model.alert) { content in
            Alert(
                title: Text(content.title), message: Text(content.message),
                dismissButton: .default(Text("OK")))
        }
        .overlay(alignment: .bottom) {
            if let toast = model.toast {
                Text(toast)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 8)
                    .background(.thinMaterial, in: Capsule())
                    .padding(.bottom, 12)
                    .transition(.opacity)
            }
        }
    }
}

/// Jobs indicator: badge with the active-job count; popover lists tracked jobs with cancel.
struct JobsToolbarButton: View {
    @ObservedObject var model: MainWindowModel
    @State private var showPopover = false

    var body: some View {
        Button {
            showPopover.toggle()
        } label: {
            Label("ジョブ", systemImage: "gearshape.2")
        }
        .badge(model.activeJobCount)
        .overlay(alignment: .topTrailing) {
            if model.activeJobCount > 0 {
                Text("\(model.activeJobCount)")
                    .font(.caption2.bold())
                    .foregroundStyle(.white)
                    .padding(3)
                    .background(.blue, in: Circle())
                    .offset(x: 6, y: -6)
            }
        }
        .help("実行中のジョブ")
        .popover(isPresented: $showPopover, arrowEdge: .bottom) {
            JobsListView(model: model)
        }
    }
}

struct JobsListView: View {
    @ObservedObject var model: MainWindowModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("ジョブ").font(.headline)
                Spacer()
                Button("完了分をクリア") {
                    model.clearFinishedJobs()
                }
                .disabled(model.jobs.allSatisfy(\.isActive))
            }
            if model.unresolvedJobRequestCount > 0 {
                Text("操作結果を再確認中です。確認が終わるまでアップデートを延期します。")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
            if model.jobs.isEmpty {
                Text(model.activeJobCount > 0 ? "ジョブの状態を確認中…" : "ジョブはありません")
                    .foregroundStyle(.secondary)
            } else {
                ForEach(model.jobs) { job in
                    HStack(alignment: .firstTextBaseline) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(NarumiFormat.jobText(kind: job.kind, status: job.status, progress: job.progress))
                                .font(.subheadline)
                            HStack(spacing: 6) {
                                Text(job.jobID)
                                if let meetingID = job.meetingID {
                                    Text(meetingID)
                                }
                            }
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            if let error = job.error {
                                Text("\(error.code): \(error.message)")
                                    .font(.caption)
                                    .foregroundStyle(.red)
                                    .lineLimit(2)
                            }
                        }
                        Spacer()
                        if job.isActive {
                            Button("取消") {
                                Task { await model.cancel(jobID: job.jobID) }
                            }
                        }
                    }
                }
            }
        }
        .padding(12)
        .frame(width: 380)
    }
}
