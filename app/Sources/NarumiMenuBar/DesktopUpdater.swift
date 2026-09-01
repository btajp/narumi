import AppKit
import Foundation
import NarumiMenuBarCore
import Sparkle

/// Sparkle remains responsible for the feed, signatures and scheduled checks. This adapter
/// postpones only installation until the app has a fresh, idle recording/job state.
@MainActor
final class DesktopUpdater: NSObject, SPUUpdaterDelegate {
    private let blockReason: () -> String?
    private let refreshSafety: () async -> Void
    private let installationChanged: (Bool) -> Void
    private var deferredInstall: (() -> Void)?
    private var validationTask: Task<Void, Never>?
    private var validationToken: DesktopUpdateGate.Token?
    private var gate = DesktopUpdateGate()
    private var cycleGeneration: UInt64 = 0
    private(set) var ownsTermination = false
    var installing: Bool { gate.installing }
    private(set) var controller: SPUStandardUpdaterController!

    init(
        blockReason: @escaping () -> String?,
        refreshSafety: @escaping () async -> Void,
        installationChanged: @escaping (Bool) -> Void
    ) {
        self.blockReason = blockReason
        self.refreshSafety = refreshSafety
        self.installationChanged = installationChanged
        super.init()
        controller = SPUStandardUpdaterController(
            startingUpdater: true, updaterDelegate: self, userDriverDelegate: nil)
    }

    var canCheckForUpdates: Bool {
        gate.canCheckForUpdates(updaterCanCheck: controller.updater.canCheckForUpdates)
    }

    @discardableResult
    func checkForUpdates() -> Bool {
        guard canCheckForUpdates else { return false }
        controller.checkForUpdates(nil)
        return true
    }

    func stateDidChange() {
        guard deferredInstall != nil, validationTask == nil,
            let token = gate.beginValidation(blocked: blockReason() != nil)
        else {
            return
        }
        validationToken = token
        validationTask = Task { [weak self] in
            guard let self else { return }
            await refreshSafety()
            guard validationToken == token else { return }
            validationToken = nil
            validationTask = nil
            guard gate.finishValidation(token, blocked: Task.isCancelled || blockReason() != nil),
                let install = deferredInstall
            else { return }
            deferredInstall = nil
            // No await between this lock and the install handler: a main-window/menu start
            // cannot slip between validation and Sparkle's application termination request.
            ownsTermination = true
            installationChanged(true)
            install()
        }
    }

    /// Sparkle is already waiting for the app to exit. Release the UI lock so the recording
    /// can be stopped normally, then retry only Quit after a fresh idle-state check.
    func installationTerminationDenied() {
        guard ownsTermination else { return }
        gate.installationTerminationDenied()
        deferredInstall = { [weak self] in self?.retryTermination() }
        installationChanged(false)
        stateDidChange()
    }

    private func retryTermination() {
        let generation = cycleGeneration
        RunLoop.main.perform { [weak self] in
            MainActor.assumeIsolated {
                guard let self, self.cycleGeneration == generation, self.ownsTermination else { return }
                guard self.installing, self.blockReason() == nil else {
                    self.installationTerminationDenied()
                    return
                }
                // The app's async shutdown pumps the default run loop; do not call
                // terminate directly from the validation Task/main-queue job.
                NSApp.terminate(nil)
            }
        }
    }

    func updater(
        _ updater: SPUUpdater, shouldPostponeRelaunchForUpdate item: SUAppcastItem,
        untilInvokingBlock installHandler: @escaping () -> Void
    ) -> Bool {
        // Sparkle calls this at most once per installation. Always postpone once so even a
        // previously idle app rechecks the server before allowing a relaunch.
        cycleGeneration &+= 1
        validationTask?.cancel()
        validationTask = nil
        validationToken = nil
        gate.deferInstallation()
        deferredInstall = installHandler
        stateDidChange()
        return true
    }

    func updater(
        _ updater: SPUUpdater, didFinishUpdateCycleFor updateCheck: SPUUpdateCheck,
        error: (any Error)?
    ) {
        cycleGeneration &+= 1
        validationTask?.cancel()
        validationTask = nil
        validationToken = nil
        deferredInstall = nil
        gate.finishCycle()
        ownsTermination = false
        installationChanged(false)
    }

    nonisolated func feedURLString(for updater: SPUUpdater) -> String? {
        guard let raw = ProcessInfo.processInfo.environment[ServerConfig.Env.sparkleFeedURL], !raw.isEmpty else {
            return nil
        }
        return raw
    }
}
