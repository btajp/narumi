import XCTest

@testable import NarumiMenuBarCore

final class DesktopUpdateGateTests: XCTestCase {
    func testIdleCannotValidateOrInstall() {
        var gate = DesktopUpdateGate()
        XCTAssertEqual(gate.phase, .idle)
        XCTAssertFalse(gate.installing)
        XCTAssertNil(gate.beginValidation(blocked: false))
        XCTAssertNil(gate.beginValidation(blocked: true))
        XCTAssertEqual(gate.phase, .idle)
    }

    func testRecordingKeepsDeferredInstallationWaiting() throws {
        var gate = DesktopUpdateGate()
        gate.deferInstallation()
        XCTAssertEqual(gate.phase, .waiting)
        XCTAssertNil(gate.beginValidation(blocked: true))
        XCTAssertEqual(gate.phase, .waiting)
        XCTAssertFalse(gate.installing)
        let token = try XCTUnwrap(gate.beginValidation(blocked: false))
        XCTAssertEqual(gate.phase, .validating)
        XCTAssertFalse(gate.installing)
        XCTAssertTrue(gate.finishValidation(token, blocked: false))
        XCTAssertEqual(gate.phase, .installing)
        XCTAssertTrue(gate.installing)
    }

    func testWorkStartingDuringValidationReturnsToWaiting() throws {
        var gate = DesktopUpdateGate()
        gate.deferInstallation()
        let first = try XCTUnwrap(gate.beginValidation(blocked: false))
        XCTAssertFalse(gate.finishValidation(first, blocked: true))
        XCTAssertEqual(gate.phase, .waiting)
        XCTAssertFalse(gate.installing)
        XCTAssertNil(gate.beginValidation(blocked: true))
        let retry = try XCTUnwrap(gate.beginValidation(blocked: false))
        XCTAssertNotEqual(first, retry)
        XCTAssertTrue(gate.finishValidation(retry, blocked: false))
        XCTAssertTrue(gate.installing)
    }

    func testTerminationDenialUnlocksAndAllowsAnotherValidation() throws {
        var gate = DesktopUpdateGate()
        gate.deferInstallation()
        let first = try XCTUnwrap(gate.beginValidation(blocked: false))
        XCTAssertTrue(gate.finishValidation(first, blocked: false))
        gate.installationTerminationDenied()
        XCTAssertEqual(gate.phase, .waiting)
        XCTAssertFalse(gate.installing)
        XCTAssertFalse(gate.finishValidation(first, blocked: false))
        XCTAssertNil(gate.beginValidation(blocked: true))
        let retry = try XCTUnwrap(gate.beginValidation(blocked: false))
        XCTAssertNotEqual(first, retry)
        XCTAssertTrue(gate.finishValidation(retry, blocked: false))
        XCTAssertTrue(gate.installing)
    }

    func testFinishCycleIgnoresAnOldValidationCallback() throws {
        var gate = DesktopUpdateGate()
        gate.deferInstallation()
        let cancelled = try XCTUnwrap(gate.beginValidation(blocked: false))
        gate.finishCycle()
        let before = gate
        XCTAssertFalse(gate.finishValidation(cancelled, blocked: false))
        XCTAssertEqual(gate, before)
        XCTAssertEqual(gate.phase, .idle)
        XCTAssertFalse(gate.installing)
    }

    func testOldCycleCannotFinishTheNextCycleValidation() throws {
        var gate = DesktopUpdateGate()
        gate.deferInstallation()
        let old = try XCTUnwrap(gate.beginValidation(blocked: false))
        gate.finishCycle()
        gate.deferInstallation()
        let current = try XCTUnwrap(gate.beginValidation(blocked: false))
        let before = gate
        XCTAssertNotEqual(old, current)
        XCTAssertFalse(gate.finishValidation(old, blocked: false))
        XCTAssertEqual(gate, before)
        XCTAssertTrue(gate.finishValidation(current, blocked: false))
    }

    func testDeferralInvalidatesAnInFlightValidation() throws {
        var gate = DesktopUpdateGate()
        gate.deferInstallation()
        let old = try XCTUnwrap(gate.beginValidation(blocked: false))
        gate.deferInstallation()
        XCTAssertEqual(gate.phase, .waiting)
        let current = try XCTUnwrap(gate.beginValidation(blocked: false))
        let before = gate
        XCTAssertFalse(gate.finishValidation(old, blocked: true))
        XCTAssertEqual(gate, before)
        XCTAssertTrue(gate.finishValidation(current, blocked: false))
    }

    func testDuplicateValidationAndDuplicateCallbackAreRejected() throws {
        var gate = DesktopUpdateGate()
        gate.deferInstallation()
        let token = try XCTUnwrap(gate.beginValidation(blocked: false))
        XCTAssertNil(gate.beginValidation(blocked: false))
        XCTAssertNil(gate.beginValidation(blocked: true))
        XCTAssertTrue(gate.finishValidation(token, blocked: false))
        let installing = gate
        XCTAssertFalse(gate.finishValidation(token, blocked: true))
        XCTAssertEqual(gate, installing)
        XCTAssertNil(gate.beginValidation(blocked: false))
    }

    func testFinishCycleClearsInstallingAndRequiresNewDeferral() throws {
        var gate = DesktopUpdateGate()
        gate.deferInstallation()
        let token = try XCTUnwrap(gate.beginValidation(blocked: false))
        gate.finishValidation(token, blocked: false)
        gate.finishCycle()
        XCTAssertEqual(gate.phase, .idle)
        XCTAssertFalse(gate.installing)
        XCTAssertNil(gate.beginValidation(blocked: false))
    }

    func testTerminationDenialOutsideInstallationDoesNotChangeTheCycle() throws {
        var gate = DesktopUpdateGate()
        let idle = gate
        gate.installationTerminationDenied()
        XCTAssertEqual(gate, idle)
        gate.deferInstallation()
        let waiting = gate
        gate.installationTerminationDenied()
        XCTAssertEqual(gate, waiting)
        let token = try XCTUnwrap(gate.beginValidation(blocked: false))
        let validating = gate
        gate.installationTerminationDenied()
        XCTAssertEqual(gate, validating)
        XCTAssertTrue(gate.finishValidation(token, blocked: false))
    }
}
