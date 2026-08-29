import Foundation
import XCTest
@testable import NarumiMenuBarCore

@MainActor
final class ProviderSettingsRecoveryFlowsTests: XCTestCase {
    func testAuthenticationRejectedBeforeAcceptanceDoesNotCreatePermanentUnknownWork() async {
        for code in [ProviderSettingsErrorCode.configurationConflict, .busy, .invalidArgument, .notFound, .authenticationRequired] {
            let client = FakeProviderSettingsClient(scenario: .init(authRejection: ProviderSettingsFailure(code)))
            let store = ProviderSettingsStore(client: client)
            await store.load()
            await store.startAuthentication()
            XCTAssertNil(store.pendingAuthentication, code.rawValue)
            let lookups = await client.authLookups
            XCTAssertTrue(lookups.isEmpty, code.rawValue)
            await store.load(discardEdits: true)
            XCTAssertTrue(store.canTest, code.rawValue)
            store.editor.displayName = "Can edit again"
            XCTAssertTrue(store.canSave, code.rawValue)
        }
    }

    func testRuntimeRejectedBeforeAcceptanceCanRecoverAfterCatalogReload() async {
        let client = FakeProviderSettingsClient(scenario: .init(setupRejection: ProviderSettingsFailure(.configurationConflict)))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        await store.prepare(ProviderSettingsFixtures.resource(), providerID: .anthropicAPI)
        XCTAssertNil(store.recovery.setups[.anthropicAPI])
        XCTAssertFalse(store.canPrepare(ProviderSettingsFixtures.resource(), provider: store.providers[0]))
        await store.load(discardEdits: true)
        XCTAssertTrue(store.canPrepare(ProviderSettingsFixtures.resource(), provider: store.providers[0]))
        let requests = await client.setupRequests
        XCTAssertEqual(requests.count, 1)
    }

    func testAmbiguousEngineFailureKeepsSetupReceiptUnresolved() async {
        let client = FakeProviderSettingsClient(scenario: .init(setupRejection: ProviderSettingsFailure(.engineUnavailable)))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        await store.prepare(ProviderSettingsFixtures.resource(), providerID: .anthropicAPI)
        XCTAssertEqual(store.recovery.setups[.anthropicAPI]?.state, .unknown)
        XCTAssertFalse(store.canPrepare(ProviderSettingsFixtures.resource(), provider: store.providers[0]))
    }

    func testLostCreateResponseRequiresExplicitSnapshotReviewInsteadOfAnotherUUID() async throws {
        let client = FakeProviderSettingsClient(connections: [], scenario: .init(loseSaveResponse: true))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        store.editor.displayName = "Created once"
        store.editor.apiKey = "not-a-real-key"
        await store.save()
        XCTAssertEqual(store.editor.apiKey, "")
        XCTAssertTrue(store.saveNeedsReconciliation)
        XCTAssertFalse(store.canSave)
        XCTAssertFalse(store.canAddConnection)
        await store.save()
        store.newConnection()
        await store.load(discardEdits: true)
        XCTAssertTrue(store.saveNeedsReconciliation)
        XCTAssertFalse(store.canAddConnection)
        let reviewed = try XCTUnwrap(store.selectedConnection)
        store.adoptSavedConnectionAfterReview(connectionID: reviewed.connectionID, expectedRevision: reviewed.revision)
        XCTAssertFalse(store.saveNeedsReconciliation)
        XCTAssertFalse(store.editor.isCreating)
        XCTAssertEqual(store.selectedConnection?.displayName, "Created once")
        XCTAssertTrue(store.selectedConnection?.credentialPresent ?? false)
        XCTAssertEqual(store.editor.apiKey, "")
        let requests = await client.saveRequests
        let connections = await client.connections
        XCTAssertEqual(requests.count, 1)
        XCTAssertEqual(connections.count, 1)
        XCTAssertEqual(store.selectedConnectionID, try XCTUnwrap(connections.first).connectionID)
    }

    func testEmptySnapshotDoesNotTreatAmbiguousCreateAsNeverAccepted() async {
        let client = FakeProviderSettingsClient(connections: [], scenario: .init(saveFailure: ProviderSettingsFailure(.transport)))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        store.editor.displayName = "Unknown create"
        await store.save()
        await store.load(discardEdits: true)
        XCTAssertTrue(store.saveNeedsReconciliation)
        XCTAssertFalse(store.canAddConnection)
        XCTAssertFalse(store.canSave)
        let requests = await client.saveRequests
        XCTAssertEqual(requests.count, 1)
    }

    func testDirtyDraftSurvivesSameRevisionAuthenticationCompletionAndCanSave() async {
        let client = FakeProviderSettingsClient(connections: [
            ProviderSettingsFixtures.connection(activeAuth: ProviderSettingsFixtures.activeAuth())
        ])
        let store = ProviderSettingsStore(client: client)
        await store.load()
        store.editor.displayName = "Unsaved draft"
        XCTAssertFalse(store.canSave)
        await store.refreshOperations()
        XCTAssertEqual(store.pendingAuthentication?.state, .succeeded)
        XCTAssertNil(store.selectedConnection?.activeAuth)
        XCTAssertNil(store.editor.connection?.activeAuth)
        XCTAssertEqual(store.editor.displayName, "Unsaved draft")
        XCTAssertTrue(store.canSave)
    }

    func testExternalDeletionCannotMoveOldModelAndConnectionSuccessToAnotherConnection() async {
        let client = FakeProviderSettingsClient()
        let store = ProviderSettingsStore(client: client)
        await store.load()
        await store.loadModels()
        await store.testConnection()
        XCTAssertFalse(store.models.isEmpty)
        XCTAssertNotNil(store.lastTest)
        await client.replaceConnections([ProviderSettingsFixtures.connection(connectionID: "conn-abcdef012345", name: "Another connection")])
        await store.load()
        XCTAssertEqual(store.selectedConnectionID, "conn-abcdef012345")
        XCTAssertTrue(store.models.isEmpty)
        XCTAssertNil(store.lastTest)
    }

    func testNewRuntimeSetupDoesNotDisplayPreviousFailedJobAfterFastCompletion() async {
        let client = FakeProviderSettingsClient(scenario: .init(setupCompletesBeforeResponse: true))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        var oldJob = ProviderSettingsFixtures.job(state: "failed")
        oldJob.jobID = "job-abcdef012345"
        store.setupJobs[.anthropicAPI] = oldJob
        await store.prepare(ProviderSettingsFixtures.resource(), providerID: .anthropicAPI)
        XCTAssertEqual(store.recovery.setups[.anthropicAPI]?.state, .succeeded)
        XCTAssertNil(store.setupJobs[.anthropicAPI])
    }

    func testOpenAICannotExposeACodexDeviceChallenge() async throws {
        let url = try XCTUnwrap(ProviderAuthorizationURL(ProviderSettingsFixtures.authorizationURL()))
        let client = FakeProviderSettingsClient(
            connections: [ProviderSettingsFixtures.connection(providerID: .openaiAPI)],
            providers: [ProviderSettingsFixtures.provider(providerID: .openaiAPI)],
            scenario: .init(authLookupState: .pending, authorizationURL: url))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        await store.startAuthentication()
        XCTAssertNil(store.deviceAuthorization)
        XCTAssertNil(store.pendingAuthentication?.userCode)
        XCTAssertNil(store.pendingAuthentication?.authorizationURL)
        var copies: [String] = []
        store.copyAuthorizationUserCode { code in
            copies.append(code)
            return true
        }
        XCTAssertTrue(copies.isEmpty)
        XCTAssertFalse(store.errorMessage?.contains(ProviderSettingsFixtures.userCode) ?? true)
        let requests = await client.authRequests
        XCTAssertEqual(requests.count, 1)
    }

    func testLostCodexLoginResponseRecoversBrowserURLWithoutStartingAgain() async throws {
        let url = try XCTUnwrap(ProviderAuthorizationURL(ProviderSettingsFixtures.authorizationURL()))
        let client = FakeProviderSettingsClient(
            connections: [ProviderSettingsFixtures.connection(providerID: .codexAppServer, credential: false)],
            providers: [ProviderSettingsFixtures.provider(providerID: .codexAppServer)],
            scenario: .init(loseAuthResponse: true, authLookupState: .pending, authorizationURL: url))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        await store.startAuthentication()
        XCTAssertEqual(store.browserAuthorizationURL, url.browserURL)
        XCTAssertEqual(store.deviceAuthorization?.userCode.displayValue, ProviderSettingsFixtures.userCode)
        XCTAssertEqual(store.pendingAuthentication?.state, .pending)
        await store.startAuthentication()
        await store.refreshOperations()
        let starts = await client.authRequests
        let lookups = await client.authLookups
        XCTAssertEqual(starts.count, 1)
        XCTAssertEqual(lookups.count, 2)
        XCTAssertTrue(lookups.allSatisfy { $0.startRequestID == starts[0].requestID })
    }

    func testClosingSettingsClearsLoginURLAndANewStoreRecoversOnlyByStatusLookup() async throws {
        let url = try XCTUnwrap(ProviderAuthorizationURL(ProviderSettingsFixtures.authorizationURL()))
        let client = FakeProviderSettingsClient(
            connections: [ProviderSettingsFixtures.connection(providerID: .codexAppServer, credential: false)],
            providers: [ProviderSettingsFixtures.provider(providerID: .codexAppServer)],
            scenario: .init(authLookupState: .pending, authorizationURL: url))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        await store.startAuthentication()
        XCTAssertNotNil(store.browserAuthorizationURL)
        let requestID = try XCTUnwrap(store.pendingAuthentication?.startRequestID)
        store.dismiss()
        XCTAssertNil(store.browserAuthorizationURL)
        XCTAssertNil(store.pendingAuthentication?.authorizationURL)
        XCTAssertNil(store.pendingAuthentication?.userCode)
        XCTAssertEqual(store.pendingAuthentication?.startRequestID, requestID)

        let reopened = ProviderSettingsStore(client: client)
        await reopened.load()
        XCTAssertNil(reopened.browserAuthorizationURL)
        XCTAssertTrue(reopened.needsPolling)
        XCTAssertFalse(reopened.canAuthenticate)
        await reopened.refreshOperations()
        XCTAssertEqual(reopened.browserAuthorizationURL, url.browserURL)
        XCTAssertEqual(reopened.deviceAuthorization?.userCode.displayValue, ProviderSettingsFixtures.userCode)
        let starts = await client.authRequests
        let lookups = await client.authLookups
        XCTAssertEqual(starts.count, 1)
        XCTAssertEqual(lookups.map(\.startRequestID), [requestID])
    }

    func testRestartedCodexLoginStaysUnknownUntilExplicitCancellation() async throws {
        let url = try XCTUnwrap(ProviderAuthorizationURL(ProviderSettingsFixtures.authorizationURL()))
        let client = FakeProviderSettingsClient(
            connections: [ProviderSettingsFixtures.connection(
                providerID: .codexAppServer, credential: false, authState: .unknown,
                activeAuth: ProviderSettingsFixtures.activeAuth(state: .unknown))],
            providers: [ProviderSettingsFixtures.provider(providerID: .codexAppServer)],
            scenario: .init(authLookupState: .unknown, authorizationURL: url))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        await store.refreshOperations()
        XCTAssertNil(store.browserAuthorizationURL)
        XCTAssertNil(store.pendingAuthentication?.userCode)
        XCTAssertFalse(store.needsPolling)
        XCTAssertFalse(store.canAuthenticate)
        await store.startAuthentication()
        let starts = await client.authRequests
        XCTAssertTrue(starts.isEmpty)
        await store.cancelAuthentication()
        XCTAssertEqual(store.pendingAuthentication?.state, .cancelled)
        XCTAssertTrue(store.canAuthenticate)
        XCTAssertNil(store.browserAuthorizationURL)
        let actions = await client.authRequests
        XCTAssertEqual(actions.map(\.action), [.cancel])
    }

    func testLoginURLCannotMoveToAnotherConnectionOrSurviveARevisionChange() async throws {
        let url = try XCTUnwrap(ProviderAuthorizationURL(ProviderSettingsFixtures.authorizationURL()))
        let connection = ProviderSettingsFixtures.connection(providerID: .codexAppServer, credential: false)
        let other = ProviderSettingsFixtures.connection(connectionID: "conn-abcdef012345", providerID: .codexAppServer)
        let client = FakeProviderSettingsClient(
            connections: [connection, other], providers: [ProviderSettingsFixtures.provider(providerID: .codexAppServer)],
            scenario: .init(authLookupState: .pending, authorizationURL: url))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        await store.startAuthentication()
        XCTAssertNotNil(store.browserAuthorizationURL)
        store.selectConnection(other.connectionID)
        XCTAssertNil(store.browserAuthorizationURL)
        store.selectConnection(connection.connectionID)
        XCTAssertNotNil(store.browserAuthorizationURL)
        let active = try XCTUnwrap(store.selectedConnection?.activeAuth)
        await client.replaceConnections([
            ProviderSettingsFixtures.changed(connection, revision: 2, activeAuth: active), other,
        ])
        await store.load()
        XCTAssertEqual(store.pendingAuthentication?.state, .unknown)
        XCTAssertNil(store.browserAuthorizationURL)
        XCTAssertNil(store.pendingAuthentication?.authorizationURL)
        XCTAssertNil(store.pendingAuthentication?.userCode)
        let starts = await client.authRequests
        XCTAssertEqual(starts.count, 1)
    }

    func testFailedOrCancelledCodexLogoutCanBeExplicitlyRetriedAfterReopeningWithoutCredentialPresence() async {
        for state in [ProviderAuthOperationState.failed, .cancelled] {
            let client = FakeProviderSettingsClient(
                connections: [ProviderSettingsFixtures.connection(providerID: .codexAppServer, authState: .authenticated)],
                providers: [ProviderSettingsFixtures.provider(providerID: .codexAppServer)],
                scenario: .init(authLookupState: state))
            let store = ProviderSettingsStore(client: client)
            await store.load()
            await store.logout()
            XCTAssertFalse(store.selectedConnection?.credentialPresent ?? true)
            XCTAssertFalse(store.canLogout)
            await store.refreshOperations()
            XCTAssertEqual(store.pendingAuthentication?.state, state)
            XCTAssertTrue(store.canLogout)

            let reopened = ProviderSettingsStore(client: client)
            await reopened.load()
            XCTAssertFalse(reopened.selectedConnection?.credentialPresent ?? true)
            XCTAssertTrue(reopened.canLogout)
            let beforeRetry = await client.authRequests
            XCTAssertEqual(beforeRetry.count, 1)
            await reopened.logout()
            let afterRetry = await client.authRequests
            XCTAssertEqual(afterRetry.map(\.action), [.logout, .logout])
            XCTAssertEqual(afterRetry.map(\.expectedRevision), [1, 2])
            XCTAssertNotEqual(afterRetry[0].requestID, afterRetry[1].requestID)
        }
    }

    func testDisabledCodexConnectionCanStillExplicitlyRemoveItsOwnLogin() async {
        let client = FakeProviderSettingsClient(
            connections: [ProviderSettingsFixtures.connection(providerID: .codexAppServer, enabled: false)],
            providers: [ProviderSettingsFixtures.provider(providerID: .codexAppServer)])
        let store = ProviderSettingsStore(client: client)
        await store.load()
        XCTAssertFalse(store.canTest)
        XCTAssertFalse(store.canAuthenticate)
        XCTAssertTrue(store.canLogout)
        await store.logout()
        let requests = await client.authRequests
        let models = await client.modelRequests
        XCTAssertEqual(requests.map(\.action), [.logout])
        XCTAssertTrue(models.isEmpty)
    }

    func testDeviceCodeCopyIsExplicitAndRequiresTheCurrentPendingChallenge() async throws {
        let url = try XCTUnwrap(ProviderAuthorizationURL(ProviderSettingsFixtures.authorizationURL()))
        let client = FakeProviderSettingsClient(
            connections: [ProviderSettingsFixtures.connection(providerID: .codexAppServer, credential: false)],
            providers: [ProviderSettingsFixtures.provider(providerID: .codexAppServer)],
            scenario: .init(authLookupState: .pending, authorizationURL: url))
        let store = ProviderSettingsStore(client: client)
        var copies: [String] = []
        let copy: (String) -> Bool = { copies.append($0); return true }
        await store.load()
        store.copyAuthorizationUserCode(copy)
        XCTAssertTrue(copies.isEmpty)
        await store.startAuthentication()
        XCTAssertTrue(copies.isEmpty)
        store.copyAuthorizationUserCode(copy)
        XCTAssertEqual(copies, [ProviderSettingsFixtures.userCode])
        XCTAssertFalse(store.notice?.contains(ProviderSettingsFixtures.userCode) ?? true)
        await store.refreshOperations()
        XCTAssertEqual(copies.count, 1)
        store.copyAuthorizationUserCode { _ in false }
        XCTAssertFalse(store.errorMessage?.contains(ProviderSettingsFixtures.userCode) ?? true)
        await client.configure(.init(authLookupState: .succeeded))
        await store.refreshOperations()
        XCTAssertNil(store.deviceAuthorization)
        store.copyAuthorizationUserCode(copy)
        XCTAssertEqual(copies.count, 1)
        store.dismiss()
        store.copyAuthorizationUserCode(copy)
        XCTAssertEqual(copies.count, 1)
    }

    func testUnavailableDeviceLoginIsExplicitWithoutFallbackOrAutomaticRetry() async {
        let client = FakeProviderSettingsClient(
            connections: [ProviderSettingsFixtures.connection(providerID: .codexAppServer, credential: false)],
            providers: [ProviderSettingsFixtures.provider(providerID: .codexAppServer)],
            scenario: .init(authLookupState: .failed, authLookupReason: "device_code_login_unavailable"))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        await store.startAuthentication()
        await store.refreshOperations()
        XCTAssertEqual(store.pendingAuthentication?.state, .failed)
        XCTAssertTrue(store.pendingAuthentication?.reasonMessage?.contains("デバイスコード認証を開始できませんでした") ?? false)
        XCTAssertTrue(store.pendingAuthentication?.reasonMessage?.contains("他方式への自動切替は行いません") ?? false)
        XCTAssertNil(store.deviceAuthorization)
        await store.refreshOperations()
        let starts = await client.authRequests
        XCTAssertEqual(starts.map(\.action), [.start])
    }

    func testSnapshotWithoutMatchingActiveLoginCannotKeepOrCopyStaleDeviceCode() async throws {
        let url = try XCTUnwrap(ProviderAuthorizationURL(ProviderSettingsFixtures.authorizationURL()))
        let snapshots = [
            ProviderSettingsFixtures.connection(providerID: .codexAppServer, credential: false),
            ProviderSettingsFixtures.connection(
                providerID: .codexAppServer, credential: false,
                activeAuth: ProviderSettingsFixtures.activeAuth(requestID: "another-start")),
        ]
        for snapshot in snapshots {
            let client = FakeProviderSettingsClient(
                connections: [ProviderSettingsFixtures.connection(providerID: .codexAppServer, credential: false)],
                providers: [ProviderSettingsFixtures.provider(providerID: .codexAppServer)],
                scenario: .init(authLookupState: .pending, authorizationURL: url))
            let store = ProviderSettingsStore(client: client)
            await store.load()
            await store.startAuthentication()
            let requestID = try XCTUnwrap(store.pendingAuthentication?.startRequestID)
            XCTAssertNotNil(store.deviceAuthorization)
            await client.replaceConnections([snapshot])
            await store.load()
            XCTAssertNil(store.deviceAuthorization)
            XCTAssertNil(store.pendingAuthentication?.userCode)
            XCTAssertEqual(store.pendingAuthentication?.startRequestID, requestID)
            var writes = 0
            store.copyAuthorizationUserCode { _ in writes += 1; return true }
            XCTAssertEqual(writes, 0)
        }
    }
}
