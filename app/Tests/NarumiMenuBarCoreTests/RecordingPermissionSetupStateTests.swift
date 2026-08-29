import XCTest

@testable import NarumiMenuBarCore

final class RecordingPermissionSetupStateTests: XCTestCase {
    private func info(
        microphone: String = "unknown", screen: String = "denied", helper: Bool = true,
        busy: Bool? = false, contract: String = "2.0.0",
        instanceID: String? = "00000000-0000-4000-8000-000000000001"
    ) throws -> ServerInfo {
        var capabilities: [String: Any] = [
            "recording": true,
            "permissions": ["screen_recording": screen, "microphone": microphone],
            "transports": ["streamable-http"], "transcription_engines": [],
            "diarization_engines": [], "llm_providers": [], "export_destinations": [],
        ]
        if let busy { capabilities["permission_setup_in_progress"] = busy }
        let recorderPath: Any = helper ? "/test/narumi-recorder" : NSNull()
        var object: [String: Any] = [
            "name": "narumi", "server_version": "0.1.3", "contract_version": contract,
            "capabilities": capabilities,
            "diagnostics": [
                "ffmpeg": NSNull(), "ffprobe": NSNull(), "data_root": "/test/data",
                "meetings_root": "/test/data/meetings", "catalog_path": "/test/data/catalog",
                "recorder_path": recorderPath, "contracts_dir": "/test/contracts",
            ],
        ]
        if let instanceID { object["server_instance_id"] = instanceID }
        return try JSONDecoder().decode(ServerInfo.self, from: JSONSerialization.data(withJSONObject: object))
    }

    private func connected(
        _ snapshot: ServerInfo? = nil, server: ServerState = .running(pid: 123)
    ) throws -> RecordingPermissionSetupState {
        var state = RecordingPermissionSetupState()
        state.connectionChanged(to: server)
        let token = try XCTUnwrap(state.beginSnapshot())
        state.finishSnapshot(token, info: try snapshot ?? info())
        return state
    }

    private func response(
        permission: String = "microphone", action: String = "request",
        microphone: String = "granted", screen: String = "denied", opened: Bool = false
    ) throws -> ConfigureRecordingPermissionResponse {
        let object: [String: Any] = [
            "permission": permission, "action": action, "settings_opened": opened,
            "permissions": ["screen_recording": screen, "microphone": microphone],
        ]
        return try JSONDecoder().decode(
            ConfigureRecordingPermissionResponse.self, from: JSONSerialization.data(withJSONObject: object))
    }

    func testGrantedControlsReadyAndSuppressesRequestButtons() throws {
        let state = try connected(info(microphone: "granted", screen: "granted"))
        XCTAssertTrue(state.ready)
        XCTAssertFalse(state.needsSetup)
        XCTAssertEqual(state.permissionState(.microphone), .granted)
        XCTAssertEqual(state.permissionState(.screenRecording), .granted)
        XCTAssertFalse(state.canRequest(.microphone))
        XCTAssertFalse(state.canRequest(.screenRecording))
        XCTAssertTrue(state.canOpenSettings(.microphone))
    }

    func testUnknownMissingHelperAndUnreachableAreNeverGranted() throws {
        var state = RecordingPermissionSetupState()
        XCTAssertTrue(state.needsSetup)
        XCTAssertEqual(state.permissionState(.microphone), .unreachable)
        XCTAssertNil(state.helperAvailable)
        state = try connected(info(microphone: "unknown", screen: "unknown"))
        XCTAssertEqual(state.permissionState(.microphone), .unknown)
        XCTAssertTrue(state.canRequest(.microphone))
        XCTAssertTrue(state.needsSetup)
        state = try connected(info(microphone: "unrecognized", screen: "granted"))
        XCTAssertEqual(state.permissionState(.microphone), .unknown)
        XCTAssertTrue(state.needsSetup)
        state = try connected(info(microphone: "granted", screen: "granted", helper: false))
        XCTAssertEqual(state.helperAvailable, false)
        XCTAssertEqual(state.permissionState(.microphone), .helperUnavailable)
        XCTAssertTrue(state.needsSetup)
        XCTAssertFalse(state.canRequest(.microphone))
        XCTAssertFalse(state.canOpenSettings(.screenRecording))
    }

    func testDeniedMicrophoneUsesSettingsWhileScreenStillAllowsRequest() throws {
        for version in ["2.0.0", "3.0.0", "4.0.0"] {
            let state = try connected(info(microphone: "denied", screen: "denied", contract: version))
            XCTAssertTrue(state.supportsSetup)
            XCTAssertEqual(state.permissionState(.microphone), .notGranted)
            XCTAssertEqual(state.permissionState(.screenRecording), .notGranted)
            XCTAssertFalse(state.canRequest(.microphone))
            XCTAssertTrue(state.canOpenSettings(.microphone))
            XCTAssertTrue(state.canRequest(.screenRecording))
            XCTAssertTrue(state.needsSetup)
            XCTAssertFalse(state.ready)
        }
    }

    func testOldAndUnknownMajorContractsKeepSetupUnavailable() throws {
        for version in ["1.0.0", "1.1.0", "4.0.0-rc.1", "5.0.0", "unrecognized"] {
            var state = try connected(info(contract: version))
            XCTAssertFalse(state.supportsSetup)
            XCTAssertTrue(state.needsSetup)
            XCTAssertFalse(state.canRequest(.microphone))
            XCTAssertFalse(state.canOpenSettings(.screenRecording))
            let token = try XCTUnwrap(state.beginSnapshot(refreshPermissions: true))
            XCTAssertFalse(token.refreshPermissions)
        }
    }

    func testUnknownConnectionUsesVersionProbeBeforeFreshRead() throws {
        var state = RecordingPermissionSetupState()
        state.connectionChanged(to: .running(pid: 123))
        let probe = try XCTUnwrap(state.beginSnapshot(refreshPermissions: true))
        XCTAssertFalse(probe.refreshPermissions)
        XCTAssertTrue(state.finishSnapshot(probe, info: try info()))
        let fresh = try XCTUnwrap(state.beginSnapshot(refreshPermissions: true))
        XCTAssertTrue(fresh.refreshPermissions)
        XCTAssertNil(state.beginSnapshot())
    }

    func testMissingServerInstanceIdentityDisablesSetupEvenWithNewContract() throws {
        var state = try connected(info(instanceID: nil))
        XCTAssertFalse(state.supportsSetup)
        XCTAssertFalse(state.canRequest(.microphone))
        XCTAssertFalse(state.canOpenSettings(.screenRecording))
        XCTAssertNil(state.beginAction(permission: .microphone, action: .request))
        let probe = try XCTUnwrap(state.beginSnapshot(refreshPermissions: true))
        XCTAssertFalse(probe.refreshPermissions)
    }

    func testActionInvalidatesEarlierSnapshotAndPreventsDuplicateRequests() throws {
        var state = try connected()
        let beforeAction = try XCTUnwrap(state.beginSnapshot(refreshPermissions: true))
        let action = try XCTUnwrap(state.beginAction(permission: .microphone, action: .request))
        XCTAssertTrue(state.blocked)
        XCTAssertTrue(state.isActionInFlight)
        XCTAssertFalse(state.canOpenSettings(.screenRecording))
        XCTAssertNil(state.beginAction(permission: .screenRecording, action: .request))
        XCTAssertFalse(state.finishSnapshot(beforeAction, info: try info()))
        XCTAssertTrue(state.finishAction(action, response: try response()))
        XCTAssertFalse(state.blocked)
        XCTAssertEqual(state.permissionState(.microphone), .granted)
        XCTAssertFalse(state.finishAction(action, response: try response()))
    }

    func testFalseSnapshotDuringFirstSendCannotClearPendingAction() throws {
        var state = try connected()
        let action = try XCTUnwrap(state.beginAction(permission: .microphone, action: .request))
        let during = try XCTUnwrap(state.beginSnapshot(refreshPermissions: true))
        XCTAssertTrue(state.finishSnapshot(during, info: try info(busy: false)))
        XCTAssertTrue(state.isActionInFlight)
        XCTAssertTrue(state.blocked)
        let beforeFailure = try XCTUnwrap(state.beginSnapshot(refreshPermissions: true))
        XCTAssertTrue(state.failAction(action))
        XCTAssertFalse(state.finishSnapshot(beforeFailure, info: try info(busy: false)))
        XCTAssertTrue(state.isAwaitingReconciliation)
        XCTAssertNotNil(state.pendingAction)
    }

    func testReplacementOrMissingIdentitySnapshotRejectsLateActionSuccessAndFailure() throws {
        let replacementIDs: [String?] = ["00000000-0000-4000-8000-000000000002", nil]
        for replacementID in replacementIDs {
            for refreshing in [false, true] {
                var state = try connected()
                let action = try XCTUnwrap(state.beginAction(permission: .microphone, action: .request))
                let originGeneration = state.recoveryConnectionGeneration
                let snapshot = try XCTUnwrap(state.beginSnapshot(refreshPermissions: refreshing))
                let replacement = try info(
                    microphone: "denied", screen: "granted", instanceID: replacementID)
                XCTAssertTrue(state.finishSnapshot(snapshot, info: replacement))
                XCTAssertFalse(state.isCurrentAction(action))
                XCTAssertFalse(state.isActionInFlight)
                XCTAssertTrue(state.blocked)
                XCTAssertNotNil(state.pendingAction)
                XCTAssertEqual(state.permissions, replacement.capabilities.permissions)
                XCTAssertEqual(state.recoveryConnectionGeneration, originGeneration)
                XCTAssertEqual(state.recoveryServerPID, 123)

                let adopted = state
                XCTAssertFalse(state.finishAction(action, response: try response()))
                XCTAssertEqual(state, adopted, "A queued success must not overwrite the replacement snapshot")
                XCTAssertFalse(state.failAction(action, message: "late failure"))
                XCTAssertEqual(state, adopted, "A queued failure must not alter the new snapshot or its pending origin")
            }
        }
    }

    func testSuccessfulActionStillReconcilesAnObservedBusyFlag() throws {
        var state = try connected()
        let action = try XCTUnwrap(state.beginAction(permission: .microphone, action: .request))
        let busy = try XCTUnwrap(state.beginSnapshot(refreshPermissions: true))
        state.finishSnapshot(busy, info: try info(busy: true))
        XCTAssertTrue(state.finishAction(action, response: try response()))
        XCTAssertNil(state.pendingAction)
        XCTAssertTrue(state.blocked)
        let fresh = try XCTUnwrap(state.beginSnapshot(refreshPermissions: true))
        state.finishSnapshot(fresh, info: try info(microphone: "granted"))
        XCTAssertFalse(state.blocked)
    }

    func testResponseLossNeedsFreshSupportedIdleSnapshotAndExplicitNewAction() throws {
        var state = try connected()
        let lost = try XCTUnwrap(state.beginAction(permission: .microphone, action: .request))
        XCTAssertTrue(state.failAction(lost))
        XCTAssertNil(state.beginAction(permission: .microphone, action: .request))
        let cached = try XCTUnwrap(state.beginSnapshot())
        state.finishSnapshot(cached, info: try info())
        XCTAssertTrue(state.blocked)
        let stillBusy = try XCTUnwrap(state.beginSnapshot(refreshPermissions: true))
        state.finishSnapshot(stillBusy, info: try info(busy: true))
        XCTAssertTrue(state.blocked)
        let idle = try XCTUnwrap(state.beginSnapshot(refreshPermissions: true))
        state.finishSnapshot(idle, info: try info())
        XCTAssertFalse(state.blocked)
        XCTAssertNil(state.pendingAction)
        let explicitRetry = try XCTUnwrap(state.beginAction(permission: .microphone, action: .request))
        XCTAssertNotEqual(lost, explicitRetry)
        XCTAssertFalse(state.finishAction(lost, response: try response()))
        XCTAssertTrue(state.isCurrentAction(explicitRetry))
    }

    func testOldServerFalseDefaultCannotReleaseUnknownPending() throws {
        var state = try connected()
        let action = try XCTUnwrap(state.beginAction(permission: .microphone, action: .request))
        state.failAction(action)
        let old = try XCTUnwrap(state.beginSnapshot(refreshPermissions: true))
        state.finishSnapshot(old, info: try info(busy: nil, contract: "1.0.0"))
        XCTAssertTrue(state.blocked)
        let probe = try XCTUnwrap(state.beginSnapshot(refreshPermissions: true))
        XCTAssertFalse(probe.refreshPermissions)
        state.finishSnapshot(probe, info: try info())
        XCTAssertTrue(state.blocked)
        let fresh = try XCTUnwrap(state.beginSnapshot(refreshPermissions: true))
        state.finishSnapshot(fresh, info: try info())
        XCTAssertFalse(state.blocked)
    }

    func testExternalPermissionWorkBlocksUntilFreshIdleAndCannotUseOwnedEvidence() throws {
        var state = try connected(
            info(busy: true), server: .external(URL(string: "http://127.0.0.1:8765/mcp")!))
        XCTAssertTrue(state.blocked)
        XCTAssertTrue(state.isAwaitingReconciliation)
        XCTAssertNil(state.pendingAction)
        XCTAssertFalse(state.canRequest(.microphone))
        XCTAssertNil(state.recoveryServerPID)
        XCTAssertFalse(state.confirmOwnedProcessTermination(.init(
            connectionGeneration: state.connectionGeneration, serverPID: 123,
            serverExited: true, allOwnedChildrenExited: true)))
        let ordinary = try XCTUnwrap(state.beginSnapshot())
        state.finishSnapshot(ordinary, info: try info())
        XCTAssertTrue(state.blocked)
        let fresh = try XCTUnwrap(state.beginSnapshot(refreshPermissions: true))
        state.finishSnapshot(fresh, info: try info())
        XCTAssertFalse(state.blocked)
    }

    func testReconnectPreservesPendingAndRejectsOldNetworkResults() throws {
        var state = try connected()
        let action = try XCTUnwrap(state.beginAction(permission: .microphone, action: .request))
        let oldPoll = try XCTUnwrap(state.beginSnapshot(refreshPermissions: true))
        state.connectionChanged(to: .failed("disconnected"))
        XCTAssertTrue(state.blocked)
        XCTAssertTrue(state.isAwaitingReconciliation)
        XCTAssertFalse(state.finishAction(action, response: try response()))
        XCTAssertFalse(state.failAction(action))
        XCTAssertFalse(state.finishSnapshot(oldPoll, info: try info()))
        state.connectionChanged(to: .running(pid: 123))
        let probe = try XCTUnwrap(state.beginSnapshot(refreshPermissions: true))
        XCTAssertFalse(probe.refreshPermissions)
        state.finishSnapshot(probe, info: try info())
        XCTAssertTrue(state.blocked)
        let fresh = try XCTUnwrap(state.beginSnapshot(refreshPermissions: true))
        state.finishSnapshot(fresh, info: try info())
        XCTAssertFalse(state.blocked)
    }

    func testDifferentInstanceIdleCannotResolveActionAfterPIDOrConnectionChanges() throws {
        let replacements: [ServerState] = [
            .running(pid: 456),
            .running(pid: 123),
            .external(URL(string: "http://127.0.0.1:8765/mcp")!),
            .external(URL(string: "http://127.0.0.1:8766/mcp")!),
        ]
        for replacement in replacements {
            var state = try connected()
            let action = try XCTUnwrap(state.beginAction(permission: .microphone, action: .request))
            state.failAction(action)
            state.connectionChanged(to: replacement)
            let probe = try XCTUnwrap(state.beginSnapshot(refreshPermissions: true))
            XCTAssertFalse(probe.refreshPermissions)
            state.finishSnapshot(probe, info: try info(instanceID: "00000000-0000-4000-8000-000000000002"))
            let fresh = try XCTUnwrap(state.beginSnapshot(refreshPermissions: true))
            XCTAssertTrue(fresh.refreshPermissions)
            state.finishSnapshot(fresh, info: try info(instanceID: "00000000-0000-4000-8000-000000000002"))
            XCTAssertTrue(state.blocked, "\(replacement)")
            XCTAssertNotNil(state.pendingAction)
            XCTAssertNil(state.beginAction(permission: .screenRecording, action: .request))
            var session = DesktopSessionState()
            session.connectionChanged(to: replacement)
            let poll = try XCTUnwrap(session.beginPoll())
            session.finishPoll(poll, info: .init(recordingCapable: true), recording: .init(active: false))
            session.setPermissionSetupState(blocked: state.blocked, needsSetup: false)
            XCTAssertFalse(session.canStart)
            XCTAssertNotNil(session.updateBlockReason(launcherBusy: false, knownJobsBusy: false))
        }
    }

    func testSameURLRestartWithoutLauncherTransitionCannotResolveOriginalAction() throws {
        var state = try connected()
        let action = try XCTUnwrap(state.beginAction(permission: .microphone, action: .request))
        state.failAction(action)
        let replacement = try XCTUnwrap(state.beginSnapshot(refreshPermissions: true))
        state.finishSnapshot(replacement, info: try info(instanceID: "00000000-0000-4000-8000-000000000002"))
        XCTAssertTrue(state.blocked)
        let replacementBusy = try XCTUnwrap(state.beginSnapshot(refreshPermissions: true))
        state.finishSnapshot(
            replacementBusy, info: try info(busy: true, instanceID: "00000000-0000-4000-8000-000000000002"))
        let replacementIdle = try XCTUnwrap(state.beginSnapshot(refreshPermissions: true))
        state.finishSnapshot(replacementIdle, info: try info(instanceID: "00000000-0000-4000-8000-000000000002"))
        XCTAssertTrue(state.blocked, "A second server cannot replace the unresolved action's origin")
    }

    func testMissingIdentityCannotResolvePendingButOriginalInstanceCanReconcileAfterReconnect() throws {
        var state = try connected()
        let action = try XCTUnwrap(state.beginAction(permission: .microphone, action: .request))
        state.failAction(action)
        let missingID = try XCTUnwrap(state.beginSnapshot(refreshPermissions: true))
        state.finishSnapshot(missingID, info: try info(instanceID: nil))
        XCTAssertTrue(state.blocked)
        XCTAssertFalse(state.supportsSetup)
        state.connectionChanged(to: .failed("session lost"))
        state.connectionChanged(to: .running(pid: 123))
        let probe = try XCTUnwrap(state.beginSnapshot(refreshPermissions: true))
        XCTAssertFalse(probe.refreshPermissions)
        state.finishSnapshot(probe, info: try info())
        XCTAssertTrue(state.blocked)
        let sameInstanceFresh = try XCTUnwrap(state.beginSnapshot(refreshPermissions: true))
        state.finishSnapshot(sameInstanceFresh, info: try info())
        XCTAssertFalse(state.blocked)
        XCTAssertNotNil(state.beginAction(permission: .microphone, action: .request))
    }

    func testBusyWithoutOriginalIdentityCannotAdoptALaterIdentityToClearItself() throws {
        var state = try connected(info(busy: true, instanceID: nil))
        let identified = try XCTUnwrap(state.beginSnapshot())
        state.finishSnapshot(identified, info: try info())
        let fresh = try XCTUnwrap(state.beginSnapshot(refreshPermissions: true))
        state.finishSnapshot(fresh, info: try info())
        XCTAssertTrue(state.blocked)
    }

    func testOwnedExitProofCanResolveOriginalActionWithoutMatchingLiveServerIdentity() throws {
        var state = try connected()
        let generation = state.connectionGeneration
        let action = try XCTUnwrap(state.beginAction(permission: .microphone, action: .request))
        state.failAction(action)
        let otherServer = try XCTUnwrap(state.beginSnapshot(refreshPermissions: true))
        state.finishSnapshot(otherServer, info: try info(instanceID: "00000000-0000-4000-8000-000000000002"))
        XCTAssertTrue(state.blocked)
        state.connectionChanged(to: .failed("original owned group exited"))
        XCTAssertTrue(state.confirmOwnedProcessTermination(.init(
            connectionGeneration: generation, serverPID: 123, serverExited: true, allOwnedChildrenExited: true)))
        XCTAssertFalse(state.blocked)
        XCTAssertTrue(state.needsSetup)
    }

    func testFailedReconciliationDoesNotClearPendingOrPretendPermissionsAreGranted() throws {
        var state = try connected()
        let action = try XCTUnwrap(state.beginAction(permission: .microphone, action: .request))
        state.failAction(action)
        let poll = try XCTUnwrap(state.beginSnapshot(refreshPermissions: true))
        XCTAssertTrue(state.failSnapshot(poll))
        XCTAssertFalse(state.failSnapshot(poll))
        XCTAssertTrue(state.blocked)
        XCTAssertTrue(state.needsSetup)
        XCTAssertEqual(state.permissionState(.microphone), .unreachable)
        XCTAssertNotNil(state.beginSnapshot(refreshPermissions: true))
    }

    func testSettingsOpenedIsNotPermissionGrantedAndWrongResponseStaysUnknown() throws {
        var state = try connected(info(microphone: "denied"))
        let settings = try XCTUnwrap(state.beginAction(permission: .microphone, action: .openSettings))
        state.finishAction(settings, response: try response(action: "open_settings", microphone: "denied", opened: true))
        XCTAssertTrue(state.settingsOpened)
        XCTAssertEqual(state.permissionState(.microphone), .notGranted)
        XCTAssertTrue(state.needsSetup)
        let request = try XCTUnwrap(state.beginAction(permission: .screenRecording, action: .request))
        XCTAssertFalse(state.finishAction(request, response: try response()))
        XCTAssertTrue(state.isAwaitingReconciliation)
        XCTAssertFalse(state.isActionInFlight)
    }

    func testRecordingAndOtherDesktopBusyStatePreventActionsButAllowReadOnlyRefresh() throws {
        var state = try connected()
        state.setRecordingBusy(true)
        XCTAssertFalse(state.canRequest(.microphone))
        XCTAssertFalse(state.canOpenSettings(.screenRecording))
        XCTAssertNil(state.beginAction(permission: .microphone, action: .request))
        XCTAssertNotNil(state.beginSnapshot(refreshPermissions: true))
        state.setRecordingBusy(false)
        XCTAssertTrue(state.canRequest(.microphone))
    }

    func testOwnedShutdownRequiresOriginalGenerationCorrectPIDAndCompleteChildrenEvidence() throws {
        var state = try connected()
        let originalGeneration = state.connectionGeneration
        let action = try XCTUnwrap(state.beginAction(permission: .microphone, action: .request))
        state.connectionChanged(to: .failed("disconnected"))
        state.connectionChanged(to: .stopped(exitCode: 1))
        XCTAssertTrue(state.blocked)
        XCTAssertEqual(state.recoveryConnectionGeneration, originalGeneration)
        for evidence in [
            RecordingPermissionSetupState.OwnedProcessTerminationEvidence(
                connectionGeneration: originalGeneration, serverPID: 123, serverExited: false, allOwnedChildrenExited: true),
            .init(connectionGeneration: originalGeneration, serverPID: 123, serverExited: true, allOwnedChildrenExited: false),
            .init(connectionGeneration: originalGeneration, serverPID: 456, serverExited: true, allOwnedChildrenExited: true),
            .init(connectionGeneration: state.connectionGeneration, serverPID: 123, serverExited: true, allOwnedChildrenExited: true),
        ] {
            XCTAssertFalse(state.confirmOwnedProcessTermination(evidence))
            XCTAssertTrue(state.blocked)
        }
        XCTAssertTrue(state.confirmOwnedProcessTermination(.init(
            connectionGeneration: originalGeneration, serverPID: 123, serverExited: true, allOwnedChildrenExited: true)))
        XCTAssertFalse(state.blocked)
        XCTAssertTrue(state.needsSetup)
        XCTAssertNil(state.pendingAction)
        XCTAssertFalse(state.finishAction(action, response: try response()))
        state.connectionChanged(to: .running(pid: 456))
        let probe = try XCTUnwrap(state.beginSnapshot())
        state.finishSnapshot(probe, info: try info())
        let next = try XCTUnwrap(state.beginAction(permission: .microphone, action: .request))
        XCTAssertNotEqual(next, action)
    }

    func testContextSwitchCannotUseOldOwnedProcessEvidence() throws {
        for target in [
            ServerState.external(URL(string: "http://127.0.0.1:8766/mcp")!),
            .notConfigured, .running(pid: 456), .preparing(step: "new runtime"),
        ] {
            var state = try connected()
            let generation = state.connectionGeneration
            _ = try XCTUnwrap(state.beginAction(permission: .microphone, action: .request))
            state.connectionChanged(to: target)
            XCTAssertFalse(state.confirmOwnedProcessTermination(.init(
                connectionGeneration: generation, serverPID: 123, serverExited: true, allOwnedChildrenExited: true)))
            XCTAssertTrue(state.blocked)
            XCTAssertNil(state.recoveryServerPID)
        }
    }

    func testOwnedBusyObservedWithoutLocalActionCanRecoverAfterVerifiedShutdown() throws {
        var state = try connected(info(busy: true))
        let generation = state.connectionGeneration
        XCTAssertNil(state.pendingAction)
        XCTAssertEqual(state.recoveryServerPID, 123)
        state.connectionChanged(to: .stopped(exitCode: 1))
        XCTAssertTrue(state.confirmOwnedProcessTermination(.init(
            connectionGeneration: generation, serverPID: 123, serverExited: true, allOwnedChildrenExited: true)))
        XCTAssertFalse(state.blocked)
    }

    func testSessionStaysBlockedAcrossLostPermissionResponseUntilFreshReconciliation() throws {
        var permissions = try connected(info(microphone: "granted", screen: "granted"))
        var session = DesktopSessionState()
        session.connectionChanged(to: .running(pid: 123))
        let poll = try XCTUnwrap(session.beginPoll())
        session.finishPoll(poll, info: .init(recordingCapable: true), recording: .init(active: false))
        XCTAssertTrue(session.canStart)
        let action = try XCTUnwrap(permissions.beginAction(permission: .microphone, action: .openSettings))
        session.setPermissionSetupState(blocked: permissions.blocked, needsSetup: permissions.needsSetup)
        XCTAssertFalse(session.canStart)
        XCTAssertNotNil(session.updateBlockReason(launcherBusy: false, knownJobsBusy: false))
        permissions.failAction(action)
        session.setPermissionSetupState(blocked: permissions.blocked, needsSetup: permissions.needsSetup)
        let recordingPoll = try XCTUnwrap(session.beginPoll())
        session.finishPoll(recordingPoll, info: .init(recordingCapable: true), recording: .init(active: false))
        XCTAssertFalse(session.canStart)
        XCTAssertNotNil(session.updateBlockReason(launcherBusy: false, knownJobsBusy: false))
        let fresh = try XCTUnwrap(permissions.beginSnapshot(refreshPermissions: true))
        permissions.finishSnapshot(fresh, info: try info(microphone: "granted", screen: "granted"))
        session.setPermissionSetupState(blocked: permissions.blocked, needsSetup: permissions.needsSetup)
        XCTAssertTrue(session.canStart)
        XCTAssertNil(session.updateBlockReason(launcherBusy: false, knownJobsBusy: false))
    }
}
