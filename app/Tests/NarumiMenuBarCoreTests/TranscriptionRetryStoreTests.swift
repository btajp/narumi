import Foundation
import XCTest
@testable import NarumiMenuBarCore

@MainActor
final class TranscriptionRetryStoreTests: XCTestCase {
    private struct Fixture {
        let meeting: MeetingDetail
        let job: Job
        let client: FakeTranscriptionRetryClient
        let store: TranscriptionRetryStore
        let confirmation: TranscriptionRetryConfirmation
    }

    func testPreparingConfirmationDoesNotReadSaveGenerateOrModifyStoredEpoch() async throws {
        let fixture = try makeFixture()
        let events = await fixture.client.events
        let current = await fixture.client.currentMeeting
        XCTAssertTrue(events.isEmpty)
        XCTAssertEqual(current.config, fixture.meeting.config)
        XCTAssertEqual(fixture.confirmation.config.transcriptionModel?.cacheEpoch, 0)
        XCTAssertEqual(fixture.confirmation.updatedConfig.transcriptionModel?.cacheEpoch, 1)
        XCTAssertEqual(fixture.confirmation.recording, fixture.meeting.recording)
        XCTAssertTrue(fixture.store.canConfirm)
    }

    func testConfirmationRechecksThenSavesCASAndForwardsOneExactRetry() async throws {
        let fixture = try makeFixture()
        let response = try await fixture.store.confirm(id: fixture.confirmation.id)
        let events = await fixture.client.events
        let saves = await fixture.client.saves
        let requests = await fixture.client.generations
        XCTAssertEqual(events, [.meeting, .job, .save, .regenerate])
        XCTAssertEqual(saves.count, 1)
        XCTAssertEqual(requests.count, 1)
        let saved = try XCTUnwrap(saves.first)
        let request = try XCTUnwrap(requests.first)
        XCTAssertEqual(saved.meetingID, fixture.confirmation.meetingID)
        XCTAssertEqual(saved.scope, fixture.confirmation.scope)
        XCTAssertEqual(saved.expectedConfig, fixture.confirmation.config)
        XCTAssertEqual(saved.selection, fixture.confirmation.updatedConfig.transcriptionModel)
        XCTAssertEqual(request.expectedConfig, fixture.confirmation.updatedConfig)
        XCTAssertEqual(request.retry, fixture.confirmation.details.retry)
        XCTAssertNotNil(UUID(uuidString: saved.requestID))
        XCTAssertNotNil(UUID(uuidString: request.requestID))
        XCTAssertNotEqual(saved.requestID, request.requestID)
        XCTAssertEqual(response?.meetingID, fixture.meeting.meeting.meetingID)
        XCTAssertEqual(fixture.store.startedJob, response)
        XCTAssertEqual(fixture.store.state, .started)
        let duplicate = try await fixture.store.confirm(id: fixture.confirmation.id)
        XCTAssertNil(duplicate)
        let finalRequests = await fixture.client.generations
        XCTAssertEqual(finalRequests.count, 1)
    }

    func testRetryEpochAdvancesPastBothCurrentAndBlockedEpochWithoutChangingOtherFields() throws {
        for (current, blocked, next) in [(0, 3, 4), (9, 3, 10), (3, 3, 4)] {
            let fixture = try makeFixture(epoch: current, details: ["blocked_epoch": blocked])
            var expected = fixture.meeting.config
            expected.transcriptionModel?.cacheEpoch = next
            XCTAssertEqual(fixture.confirmation.updatedConfig, expected)
            XCTAssertEqual(fixture.confirmation.retry.blockedEpoch, blocked)
        }
    }

    func testEpochOverflowIsRejectedBeforeAnyRequests() async throws {
        for (current, blocked) in [(Int.max, 0), (0, Int.max)] {
            let meeting = try TranscriptionRetryFixtures.meeting(config: TranscriptionRetryFixtures.config(epoch: current))
            let job = TranscriptionRetryFixtures.job(details: try TranscriptionRetryFixtures.details(["blocked_epoch": blocked]))
            let client = FakeTranscriptionRetryClient(meeting: meeting, job: job)
            let store = TranscriptionRetryStore(client: client)
            XCTAssertThrowsError(try store.prepare(meeting: meeting, job: job)) { error in
                XCTAssertEqual(error as? TranscriptionRetryFailure, .epochExhausted)
            }
            let events = await client.events
            XCTAssertTrue(events.isEmpty)
        }
    }

    func testOnlyFailedASRJobForThisMeetingCanBeConfirmed() async throws {
        let fixture = try makeFixture()
        let mutations: [(inout Job) -> Void] = [
            { $0.error = nil }, { $0.error = ToolErrorInfo(code: "internal", message: "Unknown minutes result") },
            { $0.meetingID = "20260829T000001Z-11223344" }, { $0.jobID = "invalid-job" },
            { $0.kind = "export" }, { $0.status = "running" }, { $0.status = "succeeded" },
        ]
        for mutate in mutations {
            var job = fixture.job
            mutate(&job)
            XCTAssertThrowsError(try fixture.store.prepare(meeting: fixture.meeting, job: job))
            XCTAssertFalse(fixture.store.canConfirm)
        }
        let events = await fixture.client.events
        XCTAssertTrue(events.isEmpty)
    }

    func testBusyMeetingUnapprovedPolicyAndClearedAPISelectionCannotBeConfirmed() async throws {
        let fixture = try makeFixture()
        let mutations: [(inout MeetingDetail) -> Void] = [
            { $0.meeting.status = "recording" }, { $0.meeting.status = "processing" },
            { $0.meeting.activeJob = ActiveJob(jobID: "job-222233334444", kind: "process", status: "queued") },
            { $0.config.externalSendPolicy = "local_only" }, { $0.config.externalSendPolicy = "subscription_ok" },
            { $0.config.transcriptionModel = nil },
        ]
        for mutate in mutations {
            var meeting = fixture.meeting
            mutate(&meeting)
            XCTAssertThrowsError(try fixture.store.prepare(meeting: meeting, job: fixture.job))
        }
        let events = await fixture.client.events
        XCTAssertTrue(events.isEmpty)
    }

    func testOptionalEvidenceProvenanceCannotPointAtAnotherSelection() throws {
        for update in [
            ["model_id": "gpt-4o-transcribe-diarize"] as [String: Any],
            ["connection_id": "conn-111122223333"], ["connection_revision": 4],
        ] {
            let meeting = try TranscriptionRetryFixtures.meeting()
            let job = TranscriptionRetryFixtures.job(details: try TranscriptionRetryFixtures.details(update))
            let store = TranscriptionRetryStore(client: FakeTranscriptionRetryClient(meeting: meeting, job: job))
            XCTAssertThrowsError(try store.prepare(meeting: meeting, job: job)) { error in
                XCTAssertEqual(error as? TranscriptionRetryFailure, .evidenceChanged)
            }
        }
    }

    func testChangedMeetingIdentityScopeOrFullConfigStopsBeforeSaving() async throws {
        let mutations: [(inout MeetingDetail) -> Void] = [
            { $0.meeting.meetingID = "20260829T000001Z-11223344" }, { $0.meeting.scope = "other-scope" },
            { $0.config.language = "en" }, { $0.config.vocabHints = ["changed"] },
            { $0.config.selfName = "Changed speaker" }, { $0.config.diarizationEngine = "fake" },
            { $0.config.minutesModel?.parameters.maxTokens = 4096 },
            { $0.config.transcriptionModel?.cacheEpoch = 7 },
        ]
        for mutate in mutations {
            let fixture = try makeFixture()
            var current = fixture.meeting
            mutate(&current)
            await fixture.client.replaceMeeting(current)
            await expectFailure(fixture, .configurationChanged)
            await assertNoWrites(fixture)
        }
    }

    func testChangedRecordingTrackHashPathRangeOrAvailabilityStopsBeforeSaving() async throws {
        let mutations: [(inout MeetingDetail) -> Void] = [
            { $0.recording.tracks["system"]?.sha256 = String(repeating: "e", count: 64) },
            { $0.recording.tracks["system"]?.path = "tracks/replaced.wav" },
            { $0.recording.tracks["system"]?.durationSec = 900 },
            { $0.recording.tracks["system"]?.discarded = true },
            { $0.recording.tracks.removeValue(forKey: "mic") },
        ]
        for mutate in mutations {
            let fixture = try makeFixture()
            var current = fixture.meeting
            mutate(&current)
            await fixture.client.replaceMeeting(current)
            await expectFailure(fixture, .evidenceChanged)
            await assertNoWrites(fixture)
        }
    }

    func testChangedInputChunkEpochAndProgressEvidenceRequireNewConfirmation() async throws {
        let changes: [[String: Any]] = [
            ["input_fingerprint": String(repeating: "c", count: 64)],
            ["chunk_fingerprint": String(repeating: "d", count: 64)], ["blocked_epoch": 1],
            ["track": "mic"], ["chunk_index": 2], ["completed_chunks": 0],
            ["start_sample": 16_000, "end_sample": 32_000],
        ]
        for change in changes {
            let fixture = try makeFixture()
            let changed = TranscriptionRetryFixtures.job(details: try TranscriptionRetryFixtures.details(change))
            await fixture.client.replaceJob(changed)
            await expectFailure(fixture, .evidenceChanged)
            await assertNoWrites(fixture)
        }
    }

    func testChangedJobIDCannotSupplyTheConfirmedEvidence() async throws {
        let fixture = try makeFixture()
        var changed = fixture.job
        changed.jobID = "job-333344445555"
        await fixture.client.replaceJob(changed)
        await expectFailure(fixture, .evidenceChanged)
        await assertNoWrites(fixture)
    }

    func testSaveReceiptRequiresExactMeetingScopeAndConfigBeforeGeneration() async throws {
        let mutations: [(inout SetMeetingConfigResponse) -> Void] = [
            { $0.meetingID = "20260829T000001Z-11223344" }, { $0.scope = nil },
            { $0.config.transcriptionModel?.cacheEpoch = 0 }, { $0.config.language = "en" },
            { $0.config.minutesModel = nil }, { $0.config.vocabHints = [] }, { $0.config.selfName = nil },
        ]
        for mutate in mutations {
            let fixture = try makeFixture()
            var receipt = SetMeetingConfigResponse(
                meetingID: fixture.confirmation.meetingID, config: fixture.confirmation.updatedConfig,
                scope: fixture.confirmation.scope)
            mutate(&receipt)
            await fixture.client.returnSave(receipt)
            await expectFailure(fixture, .saveResponseMismatch)
            let requests = await fixture.client.generations
            XCTAssertTrue(requests.isEmpty)
            XCTAssertEqual(fixture.store.state, .requiresReview)
        }
    }

    func testReadAndSaveFailuresStopWithoutReplayingAndDoNotExposeRemoteDetails() async throws {
        for operation in [FakeTranscriptionRetryClient.Operation.meeting, .job, .save] {
            let fixture = try makeFixture()
            await fixture.client.fail(at: operation)
            await expectFailure(fixture, operation == .save ? .saveOutcomeUnknown : .stateUnavailable)
            let duplicate = try await fixture.store.confirm(id: fixture.confirmation.id)
            XCTAssertNil(duplicate)
            let requests = await fixture.client.generations
            let saves = await fixture.client.saves
            XCTAssertTrue(requests.isEmpty)
            XCTAssertLessThanOrEqual(saves.count, 1)
            XCTAssertFalse(fixture.store.feedback?.contains("fixturePrivateUpstreamBody") ?? true)
        }
    }

    func testLostGenerationReceiptDoesNotReplayAnAcceptedRetry() async throws {
        let fixture = try makeFixture()
        await fixture.client.fail(at: .regenerate)
        await expectFailure(fixture, .generationOutcomeUnknown)
        let repeated = try await fixture.store.confirm(id: fixture.confirmation.id)
        let requests = await fixture.client.generations
        let accepted = await fixture.client.acceptedRetries
        XCTAssertNil(repeated)
        XCTAssertEqual(requests.count, 1)
        XCTAssertEqual(accepted, [fixture.confirmation.retry])
        XCTAssertNil(fixture.store.startedJob)
        XCTAssertTrue(fixture.store.feedback?.contains("API 課金") ?? false)
    }

    func testWrongGenerationReceiptRemainsUnknownInsteadOfTrackingAnotherJob() async throws {
        for receipt in [
            RegenerateResponse(jobID: "job-111122223333", meetingID: "20260829T000001Z-11223344"),
            RegenerateResponse(jobID: "invalid-job", meetingID: TranscriptionRetryFixtures.meetingID),
        ] {
            let fixture = try makeFixture()
            await fixture.client.returnGeneration(receipt)
            await expectFailure(fixture, .generationOutcomeUnknown)
            XCTAssertNil(fixture.store.startedJob)
        }
    }

    func testConcurrentSaveAfterFreshReadCannotOverwriteChangedConfig() async throws {
        let fixture = try makeFixture(hold: .job)
        let task = Task { try await fixture.store.confirm(id: fixture.confirmation.id) }
        await fixture.client.waitFor(.job)
        var current = fixture.meeting
        current.config.language = "en"
        await fixture.client.replaceMeeting(current)
        await fixture.client.release()
        do {
            _ = try await task.value
            XCTFail("A stale CAS must not be accepted")
        } catch { XCTAssertEqual(error as? TranscriptionRetryFailure, .saveOutcomeUnknown) }
        let saved = await fixture.client.currentMeeting
        let requests = await fixture.client.generations
        XCTAssertEqual(saved.config, current.config)
        XCTAssertTrue(requests.isEmpty)
    }

    func testContextChangeDuringReadDropsLateResponseWithoutWriting() async throws {
        for operation in [FakeTranscriptionRetryClient.Operation.meeting, .job] {
            let fixture = try makeFixture(hold: operation)
            let task = Task { try await fixture.store.confirm(id: fixture.confirmation.id) }
            await fixture.client.waitFor(operation)
            fixture.store.invalidate()
            await fixture.client.release()
            let response = try await task.value
            XCTAssertNil(response)
            XCTAssertEqual(fixture.store.state, .idle)
            await assertNoWrites(fixture)
        }
    }

    func testCancelBeforeConfirmationDoesNotStartRequests() async throws {
        let fixture = try makeFixture()
        fixture.store.cancel()
        let result = try await fixture.store.confirm(id: fixture.confirmation.id)
        let events = await fixture.client.events
        XCTAssertNil(result)
        XCTAssertTrue(events.isEmpty)
        XCTAssertEqual(fixture.store.state, .cancelled)
    }

    func testCancelOrTaskCancellationDuringSaveStopsGenerationButPreservesUncertainty() async throws {
        for cancelTask in [false, true] {
            let fixture = try makeFixture(hold: .save)
            let task = Task { try await fixture.store.confirm(id: fixture.confirmation.id) }
            await fixture.client.waitFor(.save)
            if cancelTask { task.cancel() } else { fixture.store.cancel() }
            await fixture.client.release()
            let response = try await task.value
            let current = await fixture.client.currentMeeting
            let requests = await fixture.client.generations
            XCTAssertNil(response)
            XCTAssertEqual(current.config.transcriptionModel?.cacheEpoch, 1)
            XCTAssertTrue(requests.isEmpty)
            XCTAssertEqual(fixture.store.failure, .saveOutcomeUnknown)
            fixture.store.invalidate()
            XCTAssertEqual(fixture.store.state, .requiresReview)
            XCTAssertEqual(fixture.store.failure, .saveOutcomeUnknown)
            XCTAssertNotNil(fixture.store.feedback)
        }
    }

    func testContextChangeDuringSubmissionKeepsPossiblyStartedJobWarning() async throws {
        let fixture = try makeFixture(hold: .regenerate)
        await fixture.client.fail(at: .regenerate)
        let task = Task { try await fixture.store.confirm(id: fixture.confirmation.id) }
        await fixture.client.waitFor(.regenerate)
        fixture.store.invalidate()
        await fixture.client.release()
        let response = try await task.value
        let requests = await fixture.client.generations
        XCTAssertNil(response)
        XCTAssertEqual(requests.count, 1)
        XCTAssertEqual(fixture.store.failure, .generationOutcomeUnknown)
        XCTAssertNil(fixture.store.startedJob)
        XCTAssertNil(fixture.store.confirmation)
    }

    func testDoubleConfirmationAndParallelPrepareCannotDuplicateWrites() async throws {
        let fixture = try makeFixture(hold: .save)
        let first = Task { try await fixture.store.confirm(id: fixture.confirmation.id) }
        await fixture.client.waitFor(.save)
        let second = try await fixture.store.confirm(id: fixture.confirmation.id)
        XCTAssertNil(second)
        XCTAssertThrowsError(try fixture.store.prepare(meeting: fixture.meeting, job: fixture.job)) { error in
            XCTAssertEqual(error as? TranscriptionRetryFailure, .operationInProgress)
        }
        await fixture.client.release()
        let result = try await first.value
        let saves = await fixture.client.saves
        let requests = await fixture.client.generations
        XCTAssertNotNil(result)
        XCTAssertEqual(saves.count, 1)
        XCTAssertEqual(requests.count, 1)
    }

    func testReplacingConfirmationRejectsItsOldIDWithoutRequests() async throws {
        let fixture = try makeFixture()
        let replacement = try fixture.store.prepare(meeting: fixture.meeting, job: fixture.job)
        let old = try await fixture.store.confirm(id: fixture.confirmation.id)
        let arbitrary = try await fixture.store.confirm(id: UUID())
        let events = await fixture.client.events
        XCTAssertNil(old)
        XCTAssertNil(arbitrary)
        XCTAssertTrue(events.isEmpty)
        XCTAssertNotEqual(replacement.id, fixture.confirmation.id)
        XCTAssertTrue(fixture.store.canConfirm)
    }

    func testASecondUnknownChunkRequiresItsOwnConfirmationAndDoesNotAuthorizeSuccesses() async throws {
        let fixture = try makeFixture()
        _ = try await fixture.store.confirm(id: fixture.confirmation.id)
        let nextDetails = try TranscriptionRetryFixtures.details([
            "chunk_fingerprint": String(repeating: "c", count: 64), "chunk_index": 2, "completed_chunks": 2,
            "start_sample": 9_600_000, "end_sample": 19_200_000,
        ])
        var nextJob = TranscriptionRetryFixtures.job(details: nextDetails)
        nextJob.jobID = "job-222233334444"
        await fixture.client.replaceJob(nextJob)
        await fixture.client.replaceActualRetry(nextDetails.retry)
        let repeated = try await fixture.store.confirm(id: fixture.confirmation.id)
        let beforeReview = await fixture.client.generations
        XCTAssertNil(repeated)
        XCTAssertEqual(beforeReview.count, 1)
        let current = await fixture.client.currentMeeting
        let next = try fixture.store.prepare(meeting: current, job: nextJob)
        XCTAssertEqual(next.details.completedChunks, 2)
        _ = try await fixture.store.confirm(id: next.id)
        let accepted = await fixture.client.acceptedRetries
        let requests = await fixture.client.generations
        XCTAssertEqual(accepted, [fixture.confirmation.retry, nextDetails.retry])
        XCTAssertEqual(requests.count, 2)
        XCTAssertEqual(requests.map(\.retry.inputFingerprint), [nextDetails.inputFingerprint, nextDetails.inputFingerprint])
        XCTAssertEqual(requests.map { $0.expectedConfig.transcriptionModel?.cacheEpoch }, [1, 2])
    }

    func testServerPlanMismatchStillCannotAcceptTheOldFingerprintAfterSavingEpoch() async throws {
        let fixture = try makeFixture()
        await fixture.client.replaceActualRetry(try TranscriptionRetry(
            inputFingerprint: String(repeating: "f", count: 64),
            chunkFingerprint: fixture.confirmation.retry.chunkFingerprint, blockedEpoch: 0))
        await expectFailure(fixture, .generationOutcomeUnknown)
        let accepted = await fixture.client.acceptedRetries
        let requests = await fixture.client.generations
        XCTAssertTrue(accepted.isEmpty)
        XCTAssertEqual(requests.count, 1)
        XCTAssertEqual(requests[0].retry, fixture.confirmation.retry)
    }

    func testUncertainSubmissionSurvivesBothValidAndInvalidLaterConfirmations() async throws {
        for valid in [false, true] {
            let fixture = try makeFixture(hold: .regenerate)
            await fixture.client.fail(at: .regenerate)
            let task = Task { try await fixture.store.confirm(id: fixture.confirmation.id) }
            await fixture.client.waitFor(.regenerate)
            fixture.store.invalidate()
            let requests = await fixture.client.generations
            let request = try XCTUnwrap(requests.first)
            let current = await fixture.client.currentMeeting
            if valid {
                _ = try fixture.store.prepare(meeting: current, job: fixture.job)
                XCTAssertNil(fixture.store.failure)
                XCTAssertTrue(fixture.store.canConfirm)
            } else {
                var invalidJob = fixture.job
                invalidJob.error = nil
                XCTAssertThrowsError(try fixture.store.prepare(meeting: current, job: invalidJob))
                XCTAssertEqual(fixture.store.failure, .invalidEvidence)
            }
            await fixture.client.release()
            let late = try await task.value
            XCTAssertNil(late)
            XCTAssertEqual(fixture.store.unresolvedOperations.count, 1)
            XCTAssertEqual(fixture.store.unresolvedOperations.first?.requestID, request.requestID)
            XCTAssertEqual(fixture.store.unresolvedOperations.first?.meetingID, fixture.confirmation.meetingID)
            XCTAssertTrue(fixture.store.feedback?.contains("API 課金") ?? false)
        }
    }

    func testRecoveredRequestAcknowledgesOnlyItsGenerationWarningAndNeverAnUncertainSave() async throws {
        let fixture = try makeFixture(hold: .save)
        let first = Task { try await fixture.store.confirm(id: fixture.confirmation.id) }
        await fixture.client.waitFor(.save)
        fixture.store.cancel()
        await fixture.client.release()
        let cancelled = try await first.value
        XCTAssertNil(cancelled)
        let current = await fixture.client.currentMeeting
        let next = try fixture.store.prepare(meeting: current, job: fixture.job)
        await fixture.client.fail(at: .regenerate)
        do {
            _ = try await fixture.store.confirm(id: next.id)
            XCTFail("The lost recovery receipt must remain unknown")
        } catch { XCTAssertEqual(error as? TranscriptionRetryFailure, .generationOutcomeUnknown) }
        let writes = await fixture.client.saves
        let requests = await fixture.client.generations
        let save = try XCTUnwrap(writes.first)
        let request = try XCTUnwrap(requests.first)
        XCTAssertEqual(fixture.store.unresolvedOperations.count, 2)
        fixture.store.acknowledgeResolvedRequest(requestID: UUID().uuidString)
        fixture.store.acknowledgeResolvedRequest(requestID: save.requestID)
        fixture.store.acknowledgeResolvedRequest(
            requestID: request.requestID, meetingID: "20260829T000001Z-11223344")
        XCTAssertEqual(fixture.store.unresolvedOperations.count, 2)
        fixture.store.acknowledgeResolvedRequest(requestID: request.requestID, meetingID: request.meetingID)
        XCTAssertEqual(fixture.store.unresolvedOperations.count, 1)
        XCTAssertEqual(fixture.store.unresolvedOperations.first?.requestID, save.requestID)
        XCTAssertEqual(fixture.store.unresolvedOperations.first?.failure, .saveOutcomeUnknown)
        XCTAssertNil(fixture.store.failure)
        XCTAssertTrue(fixture.store.feedback?.contains("保存結果を確認できません") ?? false)
    }

    func testLateValidReceiptResolvesOnlyItsWarningWithoutRestoringTheOldSelection() async throws {
        let fixture = try makeFixture(hold: .regenerate)
        let task = Task { try await fixture.store.confirm(id: fixture.confirmation.id) }
        await fixture.client.waitFor(.regenerate)
        fixture.store.invalidate()
        XCTAssertEqual(fixture.store.unresolvedOperations.count, 1)
        var nextMeeting = await fixture.client.currentMeeting
        nextMeeting.meeting.meetingID = "20260829T000001Z-11223344"
        var nextJob = fixture.job
        nextJob.meetingID = nextMeeting.meeting.meetingID
        nextJob.jobID = "job-333344445555"
        let next = try fixture.store.prepare(meeting: nextMeeting, job: nextJob)
        await fixture.client.release()
        let result = try await task.value
        XCTAssertNil(result)
        XCTAssertTrue(fixture.store.unresolvedOperations.isEmpty)
        XCTAssertNil(fixture.store.failure)
        XCTAssertNil(fixture.store.startedJob)
        XCTAssertEqual(fixture.store.state, .awaitingConfirmation)
        XCTAssertEqual(fixture.store.confirmation?.id, next.id)
        XCTAssertEqual(fixture.store.confirmation?.meetingID, nextMeeting.meeting.meetingID)
    }

    func testInvalidLateReceiptCannotResolveTheSubmissionWarning() async throws {
        for receipt in [
            RegenerateResponse(jobID: "job-111122223333", meetingID: "20260829T000001Z-11223344"),
            RegenerateResponse(jobID: "invalid-job", meetingID: TranscriptionRetryFixtures.meetingID),
        ] {
            let fixture = try makeFixture(hold: .regenerate)
            await fixture.client.returnGeneration(receipt)
            let task = Task { try await fixture.store.confirm(id: fixture.confirmation.id) }
            await fixture.client.waitFor(.regenerate)
            fixture.store.invalidate()
            await fixture.client.release()
            let result = try await task.value
            XCTAssertNil(result)
            XCTAssertEqual(fixture.store.unresolvedOperations.count, 1)
            XCTAssertEqual(fixture.store.failure, .generationOutcomeUnknown)
            XCTAssertNil(fixture.store.startedJob)
        }
    }

    func testTaskCancellationWithAValidLateReceiptDoesNotLeaveAnUnrecoverableWarning() async throws {
        let fixture = try makeFixture(hold: .regenerate)
        let task = Task { try await fixture.store.confirm(id: fixture.confirmation.id) }
        await fixture.client.waitFor(.regenerate)
        task.cancel()
        await fixture.client.release()
        let result = try await task.value
        XCTAssertNil(result)
        XCTAssertNil(fixture.store.startedJob)
        XCTAssertTrue(fixture.store.unresolvedOperations.isEmpty)
        XCTAssertNil(fixture.store.failure)
        XCTAssertEqual(fixture.store.state, .requiresReview)
    }

    private func makeFixture(
        epoch: Int = 0, details: [String: Any] = [:], hold: FakeTranscriptionRetryClient.Operation? = nil
    ) throws -> Fixture {
        let meeting = try TranscriptionRetryFixtures.meeting(config: TranscriptionRetryFixtures.config(epoch: epoch))
        let job = TranscriptionRetryFixtures.job(details: try TranscriptionRetryFixtures.details(details))
        let client = FakeTranscriptionRetryClient(meeting: meeting, job: job, hold: hold)
        let store = TranscriptionRetryStore(client: client)
        let confirmation = try store.prepare(meeting: meeting, job: job)
        return Fixture(meeting: meeting, job: job, client: client, store: store, confirmation: confirmation)
    }

    private func expectFailure(_ fixture: Fixture, _ expected: TranscriptionRetryFailure) async {
        do {
            _ = try await fixture.store.confirm(id: fixture.confirmation.id)
            XCTFail("Expected a guarded retry failure")
        } catch { XCTAssertEqual(error as? TranscriptionRetryFailure, expected) }
        XCTAssertEqual(fixture.store.state, .requiresReview)
        XCTAssertEqual(fixture.store.failure, expected)
    }

    private func assertNoWrites(_ fixture: Fixture) async {
        let saves = await fixture.client.saves
        let requests = await fixture.client.generations
        XCTAssertTrue(saves.isEmpty)
        XCTAssertTrue(requests.isEmpty)
    }
}
