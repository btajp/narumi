import Foundation
import XCTest

/// Executes the real app actor with injected bootstrap and HTTP implementations. The Core
/// transport tests separately exercise real TLS; this fixture never launches the GUI or a provider.
final class MCPClientSecureSessionTests: XCTestCase {
    func testRealClientAuthenticatesPreservesProviderSelectionsAndNeverReplaysSecrets() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("narumi-client-test-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let source = root.appendingPathComponent("Fixture.swift")
        let fixtureSource = MCPClientSessionFixtureSource.text
            + MCPClientSessionFixtureSourceASR.text + MCPClientSessionFixtureSourceASRRecovery.text
            + MCPClientSessionFixtureSourceASRHTTP.text
        try Data(fixtureSource.utf8).write(to: source)
        let app = URL(fileURLWithPath: #filePath).resolvingSymlinksInPath()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let core = app.appendingPathComponent("Sources/NarumiMenuBarCore")
        let coreFiles = try FileManager.default.contentsOfDirectory(at: core, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "swift" }.sorted { $0.path < $1.path }
        let executable = root.appendingPathComponent("fixture")
        let appSources = [
            "MCPClient.swift", "MCPClient+Connection.swift", "MCPClient+JobRecovery.swift", "JSONNode.swift",
            "NarumiClient.swift", "NarumiClient+ProcessingRuns.swift", "NarumiClient+Providers.swift",
            "NarumiClient+TranscriptionRetry.swift",
        ]
            .map { app.appendingPathComponent("Sources/NarumiMenuBar/\($0)").path }
        let compiler = Process()
        compiler.executableURL = URL(fileURLWithPath: "/usr/bin/xcrun")
        compiler.arguments = ["swiftc", "-swift-version", "6", "-parse-as-library", "-module-name", "NarumiMenuBarCore",
            "-o", executable.path, source.path,
            app.appendingPathComponent("Tests/NarumiMenuBarCoreTests/LoopbackTLSCertificate.swift").path]
            + appSources + coreFiles.map(\.path)
        let compileOutput = try run(compiler)
        XCTAssertEqual(compiler.terminationStatus, 0, compileOutput)
        guard compiler.terminationStatus == 0 else { return }
        let fixture = Process()
        fixture.executableURL = executable
        fixture.arguments = [root.path, app.deletingLastPathComponent().path]
        let result = try run(fixture)
        XCTAssertEqual(fixture.terminationStatus, 0, result)
        let checks = try JSONDecoder().decode([String: Bool].self, from: Data(result.utf8))
        var expectedChecks: Set<String> = [
            "bootstrap_before_http", "discovery_before_tools", "v1_rejected", "future_contract_rejected", "missing_tls_rejected",
            "false_client_auth_rejected", "wrong_instance_rejected", "secret_not_replayed",
            "setup_reconciled_without_replay", "readonly_reconnect", "rpc_error_redacted",
            "malformed_response_redacted", "invalid_config_rejected",
            "generation_unknown_localized_without_details",
            "legacy_generation_omits_new_config_field", "legacy_meeting_config_omits_new_field",
            "legacy_profile_omits_new_field", "legacy_save_does_not_regenerate",
            "v4_meeting_omits_transcription", "v4_profile_omits_transcription",
            "v4_transcription_selection_rejected", "v4_save_does_not_generate",
            "transcription_unknown_job_typed", "transcription_unknown_failure_typed",
            "transcription_unknown_malformed_job_blocked", "transcription_unknown_malformed_failure_blocked",
            "profile_save_unknown_localized_no_replay", "profile_save_unknown_details_redacted",
            "asr_retry_preflight_configuration_conflict", "asr_retry_preflight_authentication_required",
            "asr_retry_preflight_model_unavailable", "ordinary_asr_response_loss_recovers", "minutes_response_loss_recovers",
            "asr_manual_success_stale_record_rejected",
            "v5_ensemble_field_fully_omitted", "v6_ensemble_field_preserved",
            "processing_history_public_tools",
        ]
        for prefix in ["v4_codex", "v4_openai", "v4_anthropic", "v4_ollama", "v3_codex"] {
            for suffix in [
                "meeting_config_round_trip", "profile_round_trip", "save_does_not_regenerate",
                "expected_config_sent", "force_rejected", "context_expected_config_sent",
                "non_generating_context_omits_config",
            ] {
                expectedChecks.insert(prefix + "_" + suffix)
            }
        }
        for prefix in ["v5_whisper", "v5_diarize"] {
            for suffix in [
                "meeting_config_round_trip", "profile_round_trip", "meeting_cas_sent", "profile_cas_sent",
                "meeting_cas_conflict_preserves_config", "profile_cas_conflict_preserves_config",
                "model_listing_roles_separate", "save_and_list_do_not_generate",
                "expected_config_sent", "force_rejected", "context_expected_config_sent",
                "non_generating_context_omits_config", "retry_epoch_save_cas_sent",
                "ordinary_epoch_change_has_no_retry", "explicit_retry_payload_sent",
                "retry_without_config_rejected", "retry_without_transcription_rejected",
                "retry_without_increased_epoch_rejected", "retry_with_force_rejected",
                "meeting_clear_preserves_local_engine", "profile_clear_preserves_local_engine",
            ] {
                expectedChecks.insert(prefix + "_" + suffix)
            }
        }
        for prefix in ["asr_retry_unreached", "asr_retry_response_lost", "asr_retry_cancelled", "asr_retry_wrong_meeting", "asr_retry_invalid_receipt"] {
            for suffix in ["sent_once", "pending_retained", "manual_message", "duplicate_same_body_blocked", "duplicate_changed_body_blocked"] {
                expectedChecks.insert(prefix + "_" + suffix)
            }
        }
        for prefix in ["asr_manual_success", "asr_manual_configuration_conflict", "asr_manual_authentication_required",
            "asr_manual_model_unavailable", "asr_manual_invalid_receipt", "asr_manual_wrong_meeting"] {
            for suffix in ["pending_read_local", "wrong_body_rejected", "one_manual_send", "result_state", "no_automatic_followup"] {
                expectedChecks.insert(prefix + "_" + suffix)
            }
        }
        XCTAssertEqual(Set(checks.keys), expectedChecks)
        for (name, passed) in checks { XCTAssertTrue(passed, name) }
    }

    private func run(_ process: Process) throws -> String {
        let output = Pipe()
        process.standardOutput = output
        process.standardError = output
        process.standardInput = FileHandle.nullDevice
        try process.run()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return String(decoding: data, as: UTF8.self)
    }
}
