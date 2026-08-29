import NarumiMenuBarCore
import SwiftUI

struct ProviderRuntimeSection: View {
    private struct Approval {
        let providerID: ProviderID
        let resource: ProviderRuntimeResource
    }

    @Bindable var store: ProviderSettingsStore
    @State private var pendingDownload: Approval?
    @State private var confirmDownload = false

    var body: some View {
        Section("実行環境") {
            if let provider = store.selectedProvider {
                LabeledContent("準備状況", value: ProviderDisplay.runtime(provider.runtime.state))
                LabeledContent("実行環境の版", value: provider.runtime.version ?? "未確認")
                LabeledContent("アダプタの対応", value: ProviderDisplay.availability(provider.availability))
                if provider.providerID == .openaiAPI {
                    Text("narumi 内蔵の HTTP アダプタを確認します。追加の SDK・CLI の導入は不要です。準備だけでは認証や議事録生成の成功を確認したことにはなりません。")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if let reason = ProviderDisplay.reason(provider.reason) {
                    Text(reason).font(.caption).foregroundStyle(.secondary)
                }
                ForEach(provider.runtime.resources, id: \.resourceID) { resource in
                    VStack(alignment: .leading, spacing: 5) {
                        HStack {
                            Text(resource.displayName).font(.subheadline.bold())
                            Spacer()
                            Button(provider.runtime.state == .ready ? "更新・再確認" : "確認・準備") {
                                requestPreparation(resource, provider: provider)
                            }
                            .disabled(!store.canPrepare(resource, provider: provider))
                        }
                        Text(resourceSummary(resource)).font(.caption).foregroundStyle(.secondary)
                        Text("ライセンス: \(resource.license)").font(.caption).foregroundStyle(.secondary)
                        if let host = resource.downloadHost {
                            Text("ダウンロード先: \(host)").font(.caption)
                        }
                    }
                }
                if provider.runtime.resources.isEmpty {
                    Text("この版で準備できる配布物はありません。準備済みの環境がある場合も、対応状態は別に確認します。")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if let setup = store.recovery.setups[provider.providerID] {
                    HStack {
                        Text(ProviderDisplay.setup(setup.state)).font(.callout)
                        Spacer()
                        Button("準備状態を確認") { Task { await store.refreshOperations() } }
                        if setup.unresolved, setup.jobID != nil {
                            Button("準備を取消") { Task { await store.cancelSetup(providerID: provider.providerID) } }
                        }
                    }
                    if let job = store.setupJobs[provider.providerID], job.jobID == setup.jobID,
                        job.status == setup.state.rawValue {
                        Text(NarumiFormat.jobText(kind: job.kind, status: job.status, progress: job.progress))
                            .font(.caption).foregroundStyle(.secondary)
                        if let error = job.error {
                            Text(ProviderSettingsFailure(code: error.code).message)
                                .font(.caption).foregroundStyle(.orange)
                        }
                    }
                    if setup.unresolved {
                        Text("受付済みの準備ジョブを確認します。応答が失われても、新しい準備は自動で開始しません。")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
            } else {
                Text("接続設定を読み込むと、対応する実行環境を表示します。")
                    .foregroundStyle(.secondary)
            }
        }
        .confirmationDialog("実行環境をダウンロードして準備しますか？", isPresented: $confirmDownload, titleVisibility: .visible) {
            Button("ダウンロードして準備") {
                if let pendingDownload {
                    Task { await store.prepare(pendingDownload.resource, providerID: pendingDownload.providerID) }
                }
                pendingDownload = nil
            }
            Button("キャンセル", role: .cancel) { pendingDownload = nil }
        } message: {
            if let approval = pendingDownload {
                Text("\(approval.resource.displayName) を \(approval.resource.downloadHost ?? "未確認") から取得します。配布物の SHA256 を検証し、Narumi 専用の場所に準備します。グローバルツールは変更しません。")
            }
        }
    }

    private func requestPreparation(_ resource: ProviderRuntimeResource, provider: ProviderDescriptor) {
        if resource.source == .approvedDownload {
            pendingDownload = Approval(providerID: provider.providerID, resource: resource)
            confirmDownload = true
        } else {
            Task { await store.prepare(resource, providerID: provider.providerID) }
        }
    }

    private func resourceSummary(_ resource: ProviderRuntimeResource) -> String {
        let version = resource.version ?? "版は未確認"
        switch resource.source {
        case .bundled: return "同梱の配布物を準備します。外部ダウンロードなし。\(version)"
        case .installed: return "インストール済みの実行環境を確認します。外部ダウンロードなし。\(version)"
        case .approvedDownload: return "承認済みの配布物をダウンロードします。\(version)"
        }
    }
}
