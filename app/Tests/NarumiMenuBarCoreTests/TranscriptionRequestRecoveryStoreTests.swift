import Foundation
import XCTest
@testable import NarumiMenuBarCore

@MainActor
final class TranscriptionRequestRecoveryStoreTests: XCTestCase {
    private struct Fixture {
        let request: TranscriptionRequestRecovery
        let client: FakeTranscriptionRequestRecoveryClient
        let store: TranscriptionRequestRecoveryStore
        let confirmation: TranscriptionRequestRecoveryConfirmation
    }

    func testReloadAndPrepareOnlyReadRAMWithoutSendingOrChangingTheOriginalRequest() async throws {
        let fixture = try await makeFixture()
        let reads = await fixture.client.readCount
        let calls = await fixture.client.recoveryCalls
        XCTAssertEqual(reads, 1)
        XCTAssertTrue(calls.isEmpty)
        XCTAssertEqual(fixture.store.requests, [fixture.request])
        XCTAssertEqual(fixture.confirmation.request, fixture.request)
        XCTAssertEqual(fixture.confirmation.request.arguments, fixture.request.arguments)
        XCTAssertTrue(fixture.store.canConfirm)
        let replacement = try XCTUnwrap(fixture.store.prepare(fixture.request))
        XCTAssertNotEqual(replacement.id, fixture.confirmation.id)
        let stale = try await fixture.store.confirm(id: fixture.confirmation.id)
        XCTAssertNil(stale)
        let after = await fixture.client.readCount
        XCTAssertEqual(after, 1)
    }

    func testConfirmRechecksRAMAndPostsExactlyOnceWithTheSameIDBodyEpochAndProof() async throws {
        let fixture = try await makeFixture()
        let response = try await fixture.store.confirm(id: fixture.confirmation.id)
        let calls = await fixture.client.recoveryCalls
        let posts = await fixture.client.postedRequests
        let reads = await fixture.client.readCount
        XCTAssertEqual(reads, 2)
        XCTAssertEqual(calls, [fixture.request])
        XCTAssertEqual(posts, [fixture.request])
        XCTAssertEqual(posts.first?.arguments, fixture.request.arguments)
        XCTAssertEqual(posts.first?.requestID, fixture.request.requestID)
        XCTAssertEqual(posts.first?.expectedConfig.transcriptionModel?.cacheEpoch, 1)
        XCTAssertEqual(posts.first?.transcriptionRetry, fixture.request.transcriptionRetry)
        XCTAssertEqual(response?.meetingID, fixture.request.meetingID)
        XCTAssertEqual(fixture.store.startedJob, response)
        XCTAssertEqual(fixture.store.resolvedReceipts.first?.requestID, fixture.request.requestID)
        XCTAssertEqual(fixture.store.resolvedReceipts.first?.response, response)
        XCTAssertEqual(fixture.store.state, .started)
        XCTAssertTrue(fixture.store.requests.isEmpty)
        let duplicate = try await fixture.store.confirm(id: fixture.confirmation.id)
        XCTAssertNil(duplicate)
        await fixture.store.reload()
        XCTAssertEqual(fixture.store.state, .started)
        XCTAssertNil(fixture.store.failure)
        let finalPosts = await fixture.client.postedRequests
        XCTAssertEqual(finalPosts.count, 1)
    }

    func testFreshRAMMustMatchEveryFieldAndTheExactRawBytes() async throws {
        var config = try TranscriptionRetryFixtures.config(epoch: 1)
        config.language = "en"
        let changes: [TranscriptionRequestRecovery] = [
            try TranscriptionRequestRecoveryFixtures.request(trailingWhitespace: true),
            try TranscriptionRequestRecoveryFixtures.request(changes: ["reason": "Changed explanation"]),
            try TranscriptionRequestRecoveryFixtures.request(changes: ["scope": "other-scope"]),
            try TranscriptionRequestRecoveryFixtures.request(changes: [
                "expected_config": try JSONSerialization.jsonObject(with: JSONEncoder().encode(config)),
            ]),
            try TranscriptionRequestRecoveryFixtures.request(requestID: "different-original-request"),
        ]
        for changed in changes {
            let fixture = try await makeFixture()
            await fixture.client.replacePending([changed])
            await expectUnknown(fixture, expected: .requestChanged)
            let posts = await fixture.client.postedRequests
            let calls = await fixture.client.recoveryCalls
            XCTAssertTrue(posts.isEmpty)
            XCTAssertTrue(calls.isEmpty)
        }
    }

    func testMissingOrAmbiguousRAMRecordCannotBeSentOrDisplayedAsRecoverable() async throws {
        for duplicates in [false, true] {
            let fixture = try await makeFixture()
            await fixture.client.replacePending(duplicates ? [fixture.request, fixture.request] : [])
            await expectUnknown(fixture, expected: .requestChanged)
            await fixture.store.reload()
            XCTAssertTrue(fixture.store.requests.isEmpty)
            XCTAssertNil(fixture.store.prepare(fixture.request))
            let posts = await fixture.client.postedRequests
            XCTAssertTrue(posts.isEmpty)
        }
    }

    func testReloadPreservesAnUnchangedConfirmationButInvalidatesAChangedRecord() async throws {
        let fixture = try await makeFixture()
        await fixture.store.reload()
        XCTAssertEqual(fixture.store.confirmation?.id, fixture.confirmation.id)
        XCTAssertTrue(fixture.store.canConfirm)
        await fixture.client.replacePending([])
        await fixture.store.reload()
        XCTAssertNil(fixture.store.confirmation)
        XCTAssertFalse(fixture.store.canConfirm)
        XCTAssertEqual(fixture.store.failure, .requestChanged)
    }

    func testObserverReloadWhileCheckingOrSubmittingDoesNotInvalidateAnActiveRecovery() async throws {
        for duringPost in [false, true] {
            let fixture = try await makeFixture(heldRead: duringPost ? nil : 2, holdPost: duringPost)
            let task = Task { try await fixture.store.confirm(id: fixture.confirmation.id) }
            if duringPost { await fixture.client.waitForPost() } else { await fixture.client.waitForRead(2) }
            let before = await fixture.client.readCount
            await fixture.store.reload()
            let after = await fixture.client.readCount
            XCTAssertEqual(before, after)
            XCTAssertEqual(fixture.store.confirmation?.id, fixture.confirmation.id)
            await fixture.client.release()
            let response = try await task.value
            XCTAssertNotNil(response)
            let posts = await fixture.client.postedRequests
            XCTAssertEqual(posts.count, 1)
        }
    }

    func testCancelBeforeConfirmAndAnAlreadyCancelledTaskNeverSend() async throws {
        for taskCancellation in [false, true] {
            let fixture = try await makeFixture()
            let result: RegenerateResponse?
            if taskCancellation {
                let task = Task { try await fixture.store.confirm(id: fixture.confirmation.id) }
                task.cancel()
                result = try await task.value
            } else {
                fixture.store.cancel()
                result = try await fixture.store.confirm(id: fixture.confirmation.id)
            }
            XCTAssertNil(result)
            let calls = await fixture.client.recoveryCalls
            let reads = await fixture.client.readCount
            XCTAssertTrue(calls.isEmpty)
            XCTAssertEqual(reads, 1)
            XCTAssertEqual(fixture.store.state, .cancelled)
        }
    }

    func testContextChangeOrCancellationDuringRAMCheckDropsLateResultsWithoutPOST() async throws {
        for taskCancellation in [false, true] {
            let fixture = try await makeFixture(heldRead: 2)
            let task = Task { try await fixture.store.confirm(id: fixture.confirmation.id) }
            await fixture.client.waitForRead(2)
            if taskCancellation { task.cancel() } else { fixture.store.invalidate() }
            await fixture.client.release()
            let result = try await task.value
            XCTAssertNil(result)
            let calls = await fixture.client.recoveryCalls
            XCTAssertTrue(calls.isEmpty)
            XCTAssertTrue(fixture.store.unresolvedRequests.isEmpty)
        }
    }

    func testInvalidationDuringReloadDoesNotRestoreOldSessionRecords() async throws {
        let request = try TranscriptionRequestRecoveryFixtures.request()
        let client = FakeTranscriptionRequestRecoveryClient(pending: [request], heldRead: 1)
        let store = TranscriptionRequestRecoveryStore(client: client)
        let task = Task { await store.reload() }
        await client.waitForRead(1)
        store.invalidate()
        await client.release()
        await task.value
        XCTAssertTrue(store.requests.isEmpty)
        XCTAssertNil(store.prepare(request))
        let posts = await client.postedRequests
        XCTAssertTrue(posts.isEmpty)
    }

    func testCancelOrContextChangeDuringPOSTKeepsUnknownWarningThroughLaterPrepare() async throws {
        for mode in 0...2 {
            let fixture = try await makeFixture(holdPost: true)
            await fixture.client.fail()
            let task = Task { try await fixture.store.confirm(id: fixture.confirmation.id) }
            await fixture.client.waitForPost()
            if mode == 0 { fixture.store.cancel() }
            else if mode == 1 { fixture.store.invalidate() }
            else { task.cancel() }
            await fixture.client.release()
            let result = try await task.value
            XCTAssertNil(result)
            XCTAssertEqual(fixture.store.unresolvedRequests, [fixture.request])
            XCTAssertEqual(fixture.store.failure, .outcomeUnknown)
            let next = try TranscriptionRequestRecoveryFixtures.request(requestID: "other-recovery-request")
            await fixture.client.replacePending([next])
            await fixture.store.reload()
            XCTAssertNotNil(fixture.store.prepare(next))
            XCTAssertNil(fixture.store.failure)
            XCTAssertEqual(fixture.store.unresolvedRequests, [fixture.request])
            XCTAssertTrue(fixture.store.feedback?.contains(fixture.request.requestID) ?? false)
            XCTAssertNil(fixture.store.prepare(fixture.request))
            XCTAssertTrue(fixture.store.feedback?.contains("API 課金") ?? false)
        }
    }

    func testDoublePressAndParallelPrepareCannotDuplicatePOST() async throws {
        let fixture = try await makeFixture(holdPost: true)
        let first = Task { try await fixture.store.confirm(id: fixture.confirmation.id) }
        await fixture.client.waitForPost()
        let duplicate = try await fixture.store.confirm(id: fixture.confirmation.id)
        XCTAssertNil(duplicate)
        XCTAssertNil(fixture.store.prepare(fixture.request))
        await fixture.client.release()
        let result = try await first.value
        XCTAssertNotNil(result)
        let calls = await fixture.client.recoveryCalls
        let posts = await fixture.client.postedRequests
        XCTAssertEqual(calls.count, 1)
        XCTAssertEqual(posts.count, 1)
    }

    func testLateValidReceiptIsRetainedWithoutChangingTheNewSelectionOrReturningItsJob() async throws {
        let fixture = try await makeFixture(holdPost: true)
        let task = Task { try await fixture.store.confirm(id: fixture.confirmation.id) }
        await fixture.client.waitForPost()
        fixture.store.invalidate()
        let other = try TranscriptionRequestRecoveryFixtures.request(
            requestID: "other-recovery-request", changes: ["meeting_id": "20260829T000001Z-11223344"])
        await fixture.client.replacePending([fixture.request, other])
        await fixture.store.reload()
        let next = try XCTUnwrap(fixture.store.prepare(other))
        await fixture.client.release()
        let result = try await task.value
        XCTAssertNil(result)
        XCTAssertTrue(fixture.store.unresolvedRequests.isEmpty)
        XCTAssertNil(fixture.store.failure)
        XCTAssertNil(fixture.store.startedJob)
        XCTAssertEqual(fixture.store.state, .awaitingConfirmation)
        XCTAssertEqual(fixture.store.confirmation?.id, next.id)
        XCTAssertEqual(fixture.store.requests, [other])
        XCTAssertEqual(fixture.store.resolvedReceipts.count, 1)
        XCTAssertEqual(fixture.store.resolvedReceipts.first?.requestID, fixture.request.requestID)
        XCTAssertEqual(fixture.store.resolvedReceipts.first?.response.meetingID, fixture.request.meetingID)
    }

    func testTaskCancellationWithAValidLateReceiptResolvesTheWarningButDoesNotTrackTheJob() async throws {
        let fixture = try await makeFixture(holdPost: true)
        let task = Task { try await fixture.store.confirm(id: fixture.confirmation.id) }
        await fixture.client.waitForPost()
        task.cancel()
        await fixture.client.release()
        let result = try await task.value
        XCTAssertNil(result)
        XCTAssertNil(fixture.store.startedJob)
        XCTAssertTrue(fixture.store.unresolvedRequests.isEmpty)
        XCTAssertNil(fixture.store.failure)
        XCTAssertEqual(fixture.store.resolvedReceipts.first?.requestID, fixture.request.requestID)
        await fixture.store.reload()
        XCTAssertTrue(fixture.store.requests.isEmpty)
    }

    func testInvalidLateReceiptNeverResolvesTheUnknownRequest() async throws {
        for receipt in [
            RegenerateResponse(jobID: "job-111122223333", meetingID: "20260829T000001Z-11223344"),
            RegenerateResponse(jobID: "invalid-job", meetingID: TranscriptionRetryFixtures.meetingID),
        ] {
            let fixture = try await makeFixture(holdPost: true)
            await fixture.client.returnReceipt(receipt)
            let task = Task { try await fixture.store.confirm(id: fixture.confirmation.id) }
            await fixture.client.waitForPost()
            fixture.store.invalidate()
            await fixture.client.release()
            let result = try await task.value
            XCTAssertNil(result)
            XCTAssertEqual(fixture.store.unresolvedRequests, [fixture.request])
            XCTAssertEqual(fixture.store.failure, .outcomeUnknown)
            XCTAssertTrue(fixture.store.resolvedReceipts.isEmpty)
            XCTAssertNil(fixture.store.startedJob)
        }
    }

    func testUnknownReceiptIsNotAutomaticallyReplayedOrRearmedByReload() async throws {
        let fixture = try await makeFixture()
        await fixture.client.fail()
        await expectUnknown(fixture)
        await fixture.store.reload()
        XCTAssertFalse(fixture.store.canConfirm)
        let duplicate = try await fixture.store.confirm(id: fixture.confirmation.id)
        XCTAssertNil(duplicate)
        let posts = await fixture.client.postedRequests
        XCTAssertEqual(posts, [fixture.request])
        XCTAssertEqual(fixture.store.unresolvedRequests, [fixture.request])
        XCTAssertFalse(fixture.store.feedback?.contains("fixturePrivateTransportDetails") ?? true)
    }

    func testOnlyAValidatedMatchingRecoveryReceiptResolvesItsWarning() async throws {
        let fixture = try await makeFixture()
        await fixture.client.fail()
        await expectUnknown(fixture)
        let other = try TranscriptionRequestRecoveryFixtures.request(requestID: "other-recovery-request")
        await fixture.client.replacePending([fixture.request, other])
        await fixture.client.fail(false)
        await fixture.store.reload()
        let next = try XCTUnwrap(fixture.store.prepare(other))
        _ = try await fixture.store.confirm(id: next.id)
        XCTAssertEqual(fixture.store.unresolvedRequests, [fixture.request])
        await fixture.store.reload()
        let original = try XCTUnwrap(fixture.store.prepare(fixture.request))
        _ = try await fixture.store.confirm(id: original.id)
        XCTAssertTrue(fixture.store.unresolvedRequests.isEmpty)
        XCTAssertNil(fixture.store.failure)
        let posts = await fixture.client.postedRequests
        XCTAssertEqual(posts, [fixture.request, other, fixture.request])
        XCTAssertEqual(posts.first?.arguments, posts.last?.arguments)
    }

    func testMismatchedMeetingOrMalformedJobReceiptDoesNotClaimRecovery() async throws {
        for receipt in [
            RegenerateResponse(jobID: "job-111122223333", meetingID: "20260829T000001Z-11223344"),
            RegenerateResponse(jobID: "bad-job", meetingID: TranscriptionRetryFixtures.meetingID),
        ] {
            let fixture = try await makeFixture()
            await fixture.client.returnReceipt(receipt)
            await expectUnknown(fixture)
            XCTAssertNil(fixture.store.startedJob)
            XCTAssertEqual(fixture.store.unresolvedRequests, [fixture.request])
        }
    }

    func testClientRechecksTheRecordBeforePOSTIfRAMChangesAfterTheStoreSnapshot() async throws {
        let fixture = try await makeFixture(heldRead: 2)
        let task = Task { try await fixture.store.confirm(id: fixture.confirmation.id) }
        await fixture.client.waitForRead(2)
        await fixture.client.replacePending([])
        await fixture.client.release()
        do {
            _ = try await task.value
            XCTFail("The client must reject the stale pending record")
        } catch { XCTAssertEqual(error as? TranscriptionRequestRecoveryFailure, .outcomeUnknown) }
        let calls = await fixture.client.recoveryCalls
        let posts = await fixture.client.postedRequests
        XCTAssertEqual(calls, [fixture.request])
        XCTAssertTrue(posts.isEmpty)
    }

    private func makeFixture(heldRead: Int? = nil, holdPost: Bool = false) async throws -> Fixture {
        let request = try TranscriptionRequestRecoveryFixtures.request()
        let client = FakeTranscriptionRequestRecoveryClient(pending: [request], heldRead: heldRead, holdPost: holdPost)
        let store = TranscriptionRequestRecoveryStore(client: client)
        await store.reload()
        let confirmation = try XCTUnwrap(store.prepare(request))
        return Fixture(request: request, client: client, store: store, confirmation: confirmation)
    }

    private func expectUnknown(_ fixture: Fixture, expected: TranscriptionRequestRecoveryFailure = .outcomeUnknown) async {
        do {
            _ = try await fixture.store.confirm(id: fixture.confirmation.id)
            XCTFail("Expected guarded recovery failure")
        } catch { XCTAssertEqual(error as? TranscriptionRequestRecoveryFailure, expected) }
        XCTAssertEqual(fixture.store.state, .requiresReview)
        XCTAssertEqual(fixture.store.failure, expected)
    }
}
