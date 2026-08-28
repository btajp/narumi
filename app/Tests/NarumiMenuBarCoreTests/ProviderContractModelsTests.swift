import XCTest

@testable import NarumiMenuBarCore

final class ProviderContractModelsTests: XCTestCase {
    private func outputExamples(_ tool: String) throws -> [[String: Any]] {
        var directory = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        while directory.path != directory.deletingLastPathComponent().path {
            let candidate = directory.appendingPathComponent("contracts/tools/\(tool).json")
            if FileManager.default.fileExists(atPath: candidate.path) {
                let root = try XCTUnwrap(
                    JSONSerialization.jsonObject(with: Data(contentsOf: candidate)) as? [String: Any])
                let examples = try XCTUnwrap(root["examples"] as? [String: Any])
                let outputs = try XCTUnwrap(examples["output"] as? [[String: Any]])
                XCTAssertFalse(outputs.isEmpty)
                return outputs
            }
            directory = directory.deletingLastPathComponent()
        }
        throw NSError(domain: "ProviderContractModelsTests", code: 1)
    }

    private func decode<T: Decodable>(_ type: T.Type, _ object: Any) throws -> T {
        try JSONDecoder().decode(
            type, from: JSONSerialization.data(withJSONObject: object, options: [.fragmentsAllowed]))
    }

    private func all<T: Decodable>(_ type: T.Type, _ tool: String) throws -> [T] {
        try outputExamples(tool).map { try decode(type, $0) }
    }

    private func first(_ tool: String) throws -> [String: Any] {
        try XCTUnwrap(outputExamples(tool).first)
    }

    private func firstArrayItem(_ tool: String, _ key: String, index: Int = 0) throws -> [String: Any] {
        let values = try XCTUnwrap(first(tool)[key] as? [[String: Any]])
        return values[index]
    }

    private func encoded<T: Encodable>(_ value: T) throws -> [String: Any] {
        let data = try JSONEncoder().encode(value)
        let object = try JSONSerialization.jsonObject(with: data)
        return try XCTUnwrap(object as? [String: Any])
    }

    private func requireNullableKeys<T: Decodable>(
        _ type: T.Type, _ object: [String: Any], _ keys: [String],
        file: StaticString = #filePath, line: UInt = #line
    ) throws {
        for key in keys {
            var changed = object
            changed[key] = NSNull()
            XCTAssertNoThrow(try decode(type, changed), "\(key) accepts null", file: file, line: line)
            changed.removeValue(forKey: key)
            XCTAssertThrowsError(try decode(type, changed), "\(key) is required", file: file, line: line)
            changed[key] = [String]()
            XCTAssertThrowsError(try decode(type, changed), "\(key) rejects an array", file: file, line: line)
        }
    }

    func testAllNineProviderToolResponseContracts() throws {
        let providers = try all(ListProvidersResponse.self, "list_providers")
        XCTAssertEqual(providers[0].providers.map(\.providerID), [.anthropicAPI, .claudeAgentSDK, .ollama, .codexAppServer])
        XCTAssertEqual(providers[0].providers[1].authMethods, [.apiKey])
        XCTAssertEqual(providers[0].providers[1].runtime.state, .notPrepared)
        let connections = try all(ListProviderConnectionsResponse.self, "list_provider_connections")
        XCTAssertEqual(connections[0].connections[1].authMethod, .none)
        XCTAssertTrue(connections[1].connections.isEmpty)
        let saved = try all(ProviderConnectionResponse.self, "set_provider_connection")
        XCTAssertFalse(saved[0].connection.credentialPresent)
        XCTAssertTrue(saved[1].connection.credentialPresent)
        XCTAssertEqual(saved[1].connection.authState, .unverified)
        XCTAssertTrue(try all(DeleteProviderConnectionResponse.self, "delete_provider_connection")[0].deleted)
        let authentication = try all(ProviderAuthResponse.self, "authenticate_provider_connection")
        XCTAssertEqual(authentication[1].operation.state, .unknown)
        let recovered = try all(ProviderAuthResponse.self, "get_provider_auth_status")
        XCTAssertEqual(recovered[1].operation.startRequestID, authentication[1].operation.startRequestID)
        XCTAssertEqual(recovered[1].operation.state, .unknown)
        let tested = try all(ProviderConnectionTestResult.self, "test_provider_connection")
        XCTAssertTrue(tested[0].connected)
        XCTAssertEqual(tested[0].connection.lastGenerationState, .never)
        XCTAssertFalse(tested[1].connected)
        let models = try all(ListProviderModelsResponse.self, "list_provider_models")
        XCTAssertEqual(models[0].models[0].availability, .unverified)
        XCTAssertNil(models[0].models[0].contextWindow)
        XCTAssertNil(models[0].models[0].billing.inputUSDPerMillionTokens)
        XCTAssertEqual(models[0].models[0].parameterSchema.properties["max_tokens"]?.type, .integer)
        XCTAssertTrue(models[1].models.isEmpty)
        XCTAssertEqual(
            try all(PrepareProviderRuntimeResponse.self, "prepare_provider_runtime")[0].jobID,
            "job-0123456789ab")
    }

    func testRequiredNullableFieldsDoNotSilentlyBecomeMissing() throws {
        try requireNullableKeys(
            ProviderConnection.self, firstArrayItem("list_provider_connections", "connections"),
            ["endpoint", "checked_at", "active_auth"])
        let descriptor = try firstArrayItem("list_providers", "providers", index: 1)
        try requireNullableKeys(ProviderDescriptor.self, descriptor, ["reason"])
        let runtime = try XCTUnwrap(descriptor["runtime"] as? [String: Any])
        try requireNullableKeys(
            ProviderRuntime.self, runtime, ["version", "catalog_revision", "active_setup", "last_setup"])
        let resource = try XCTUnwrap((runtime["resources"] as? [[String: Any]])?.first)
        try requireNullableKeys(ProviderRuntimeResource.self, resource, ["version", "download_host", "sha256"])
        let operation = try XCTUnwrap(first("get_provider_auth_status")["operation"] as? [String: Any])
        try requireNullableKeys(ProviderAuthOperation.self, operation, ["authorization_url", "user_code", "reason"])
        try requireNullableKeys(ProviderConnectionTestResult.self, first("test_provider_connection"), ["reason"])
        let model = try firstArrayItem("list_provider_models", "models")
        try requireNullableKeys(
            ProviderModelDescriptor.self, model,
            ["resolved_revision", "context_window", "max_output_tokens", "reason", "fetched_at"])
        try requireNullableKeys(
            ProviderModelBilling.self, XCTUnwrap(model["billing"] as? [String: Any]),
            ["input_usd_per_million_tokens", "output_usd_per_million_tokens", "audio_usd_per_minute", "fetched_at"])
        try requireNullableKeys(
            ListProviderModelsResponse.self, first("list_provider_models"), ["next_cursor", "fetched_at"])
    }

    func testUnknownCapabilitiesAndWrongBooleanTypesAreRejected() throws {
        XCTAssertThrowsError(try decode(ProviderID.self, "openai-api"))
        XCTAssertThrowsError(try decode(ProviderRole.self, "future_role"))
        XCTAssertThrowsError(try decode(ProviderAuthMethod.self, "subscription"))
        XCTAssertThrowsError(try decode(ProviderAvailability.self, "future_state"))
        XCTAssertThrowsError(try decode(ProviderAuthState.self, "ready"))
        XCTAssertThrowsError(try decode(ProviderCatalogState.self, "available"))
        XCTAssertThrowsError(try decode(ProviderRuntimeState.self, "installed"))
        let connection = try firstArrayItem("list_provider_connections", "connections")
        for key in ["enabled", "credential_present"] {
            for value: Any in [1, "true", NSNull()] {
                var changed = connection
                changed[key] = value
                XCTAssertThrowsError(try decode(ProviderConnection.self, changed))
            }
        }
        var changed = connection
        changed["revision"] = true
        XCTAssertThrowsError(try decode(ProviderConnection.self, changed))
        changed["revision"] = 0
        XCTAssertThrowsError(try decode(ProviderConnection.self, changed))
        var model = try firstArrayItem("list_provider_models", "models")
        model["context_window"] = true
        XCTAssertThrowsError(try decode(ProviderModelDescriptor.self, model))
        var receipt = try first("delete_provider_connection")
        receipt["deleted"] = false
        XCTAssertThrowsError(try decode(DeleteProviderConnectionResponse.self, receipt))
    }

    func testUnsupportedAuthenticationAndUnverifiedDownloadAreRejected() throws {
        var operation = try XCTUnwrap(first("get_provider_auth_status")["operation"] as? [String: Any])
        operation["authorization_url"] = "https://example.invalid/login"
        XCTAssertThrowsError(try decode(ProviderAuthOperation.self, operation))
        var connection = try firstArrayItem("list_provider_connections", "connections", index: 1)
        connection["credential_present"] = true
        XCTAssertThrowsError(try decode(ProviderConnection.self, connection))
        let descriptor = try firstArrayItem("list_providers", "providers", index: 1)
        let runtime = try XCTUnwrap(descriptor["runtime"] as? [String: Any])
        var resource = try XCTUnwrap((runtime["resources"] as? [[String: Any]])?.first)
        resource["source"] = "approved_download"
        resource["version"] = "1.0"
        resource["download_host"] = "example.invalid"
        XCTAssertThrowsError(try decode(ProviderRuntimeResource.self, resource))
        resource["sha256"] = String(repeating: "a", count: 64)
        XCTAssertNoThrow(try decode(ProviderRuntimeResource.self, resource))
    }

    func testParameterSchemaRemainsClosedAndBillingUnknownIsNotZero() throws {
        let model = try firstArrayItem("list_provider_models", "models")
        var schema = try XCTUnwrap(model["parameter_schema"] as? [String: Any])
        schema["additionalProperties"] = true
        XCTAssertThrowsError(try decode(ProviderParameterSchema.self, schema))
        schema["additionalProperties"] = false
        schema["properties"] = ["api_key": ["type": "string"]]
        XCTAssertThrowsError(try decode(ProviderParameterSchema.self, schema))
        XCTAssertThrowsError(try decode(ProviderModelParameter.self, ["type": "string", "default": NSNull()]))
        XCTAssertEqual(try decode(ProviderScalarValue.self, true), .boolean(true))
        XCTAssertEqual(try decode(ProviderScalarValue.self, 1), .number(1))
        XCTAssertThrowsError(try decode(ProviderScalarValue.self, ["nested": true]))
        var billing = try XCTUnwrap(model["billing"] as? [String: Any])
        XCTAssertNil(try decode(ProviderModelBilling.self, billing).inputUSDPerMillionTokens)
        billing["input_usd_per_million_tokens"] = "0.125"
        XCTAssertEqual(try decode(ProviderModelBilling.self, billing).inputUSDPerMillionTokens, "0.125")
        for value: Any in ["free", "-1", 0, true] {
            billing["input_usd_per_million_tokens"] = value
            XCTAssertThrowsError(try decode(ProviderModelBilling.self, billing))
        }
    }

    func testCredentialMutationUsesOmissionNullOrReplacement() throws {
        let unchanged = SetProviderConnectionRequest(
            connectionID: "conn-0123456789ab", expectedRevision: 2, enabled: false)
        let unchangedJSON = try encoded(unchanged)
        XCTAssertNil(unchangedJSON["api_key"])
        XCTAssertNil(unchangedJSON["provider_id"])
        XCTAssertEqual(unchangedJSON["enabled"] as? Bool, false)
        let cleared = try encoded(SetProviderConnectionRequest(
            connectionID: "conn-0123456789ab", expectedRevision: 2, apiKey: .clear))
        XCTAssertTrue(cleared["api_key"] is NSNull)
        let replaced = try encoded(SetProviderConnectionRequest(
            connectionID: "conn-0123456789ab", expectedRevision: 2, apiKey: .replace("fixture-key")))
        XCTAssertEqual(replaced["api_key"] as? String, "fixture-key")
        XCTAssertEqual(replaced["expected_revision"] as? Int, 2)
        let created = try encoded(SetProviderConnectionRequest(
            providerID: .ollama, displayName: "Local", authMethod: .none))
        XCTAssertEqual(created["auth_method"] as? String, "none")
        XCTAssertNil(created["connection_id"])
        XCTAssertNil(created["expected_revision"])
        XCTAssertNotNil(UUID(uuidString: try XCTUnwrap(created["request_id"] as? String)))
    }

    func testSecretDiagnosticsAndInvalidRequestsDoNotExposeTheKey() throws {
        let secret = "fixture-not-a-real-key-unique"
        let request = SetProviderConnectionRequest(
            providerID: .ollama, displayName: "Local", authMethod: .none, apiKey: .replace(secret))
        XCTAssertFalse(String(describing: request).contains(secret))
        XCTAssertFalse(String(reflecting: request).contains(secret))
        var dumped = ""
        dump(request, to: &dumped)
        XCTAssertFalse(dumped.contains(secret))
        XCTAssertThrowsError(try JSONEncoder().encode(request)) { error in
            XCTAssertFalse(String(describing: error).contains(secret))
        }
        XCTAssertThrowsError(try encoded(SetProviderConnectionRequest(
            connectionID: "conn-0123456789ab", expectedRevision: 1)))
        XCTAssertThrowsError(try encoded(SetProviderConnectionRequest(
            connectionID: "conn-0123456789ab", expectedRevision: 0, apiKey: .clear)))
        XCTAssertThrowsError(try encoded(DeleteProviderConnectionRequest(
            connectionID: "conn-0123456789ab", expectedRevision: 1, confirm: false)))
    }

    func testAuthRecoveryRequestsRetainOriginalIdentityWithoutRestarting() throws {
        let start = try encoded(AuthenticateProviderConnectionRequest(
            connectionID: "conn-0123456789ab", expectedRevision: 2, action: .start, requestID: "original-request"))
        XCTAssertEqual(start["action"] as? String, "start")
        XCTAssertNil(start["operation_id"])
        let recovered = try encoded(GetProviderAuthStatusRequest(
            connectionID: "conn-0123456789ab", startRequestID: "original-request"))
        XCTAssertEqual(recovered["start_request_id"] as? String, "original-request")
        XCTAssertNil(recovered["operation_id"])
        let byOperation = try encoded(GetProviderAuthStatusRequest(
            connectionID: "conn-0123456789ab", operationID: "auth-0123456789ab"))
        XCTAssertNil(byOperation["start_request_id"])
        let cancel = try encoded(AuthenticateProviderConnectionRequest(
            connectionID: "conn-0123456789ab", expectedRevision: 2, action: .cancel,
            operationID: "auth-0123456789ab", requestID: "cancel-request"))
        XCTAssertEqual(cancel["operation_id"] as? String, "auth-0123456789ab")
        XCTAssertEqual(cancel["request_id"] as? String, "cancel-request")
        XCTAssertThrowsError(try encoded(AuthenticateProviderConnectionRequest(
            connectionID: "conn-0123456789ab", expectedRevision: 2, action: .cancel)))
        XCTAssertThrowsError(try encoded(AuthenticateProviderConnectionRequest(
            connectionID: "conn-0123456789ab", expectedRevision: 2, action: .logout,
            operationID: "auth-0123456789ab")))
    }

    func testObservationAndRuntimeRequestsPreserveExplicitUserChoices() throws {
        let tested = try encoded(TestProviderConnectionRequest(connectionID: "conn-0123456789ab", expectedRevision: 2))
        XCTAssertEqual(tested["expected_revision"] as? Int, 2)
        let cached = try encoded(ListProviderModelsRequest(connectionID: "conn-0123456789ab"))
        XCTAssertEqual(cached["refresh"] as? Bool, false)
        let next = try encoded(ListProviderModelsRequest(
            connectionID: "conn-0123456789ab", role: .llm, cursor: "opaque-cursor", refresh: false))
        XCTAssertEqual(next["cursor"] as? String, "opaque-cursor")
        let setup = try encoded(PrepareProviderRuntimeRequest(
            providerID: .claudeAgentSDK, resourceID: "claude-sdk", expectedCatalogRevision: "bundled-v1",
            action: .update, requestID: "runtime-request"))
        XCTAssertEqual(setup["expected_catalog_revision"] as? String, "bundled-v1")
        XCTAssertEqual(setup["action"] as? String, "update")
        XCTAssertEqual(setup["request_id"] as? String, "runtime-request")
    }

    func testCodexConnectionAcceptsChatGPTLoginOnlyAtItsFixedEndpoint() throws {
        var connection = try firstArrayItem("list_provider_connections", "connections")
        connection["provider_id"] = "codex-app-server"
        connection["auth_method"] = "chatgpt"
        connection["endpoint"] = "https://chatgpt.com"
        connection["credential_present"] = false
        XCTAssertEqual(try decode(ProviderConnection.self, connection).authMethod, .chatgpt)
        for endpoint: Any in ["https://api.openai.com", "https://example.invalid", NSNull()] {
            var changed = connection
            changed["endpoint"] = endpoint
            XCTAssertThrowsError(try decode(ProviderConnection.self, changed))
        }
        connection["auth_method"] = "api_key"
        XCTAssertThrowsError(try decode(ProviderConnection.self, connection))

        let valid = try encoded(SetProviderConnectionRequest(
            providerID: .codexAppServer, displayName: "Meeting Codex", authMethod: .chatgpt,
            endpoint: ProviderConnectionSettings.codexEndpoint))
        XCTAssertEqual(valid["provider_id"] as? String, "codex-app-server")
        XCTAssertEqual(valid["auth_method"] as? String, "chatgpt")
        XCTAssertNil(valid["api_key"])
        for mutation in [ProviderCredentialUpdate.clear, .replace("fixture-key")] {
            XCTAssertThrowsError(try encoded(SetProviderConnectionRequest(
                providerID: .codexAppServer, displayName: "Meeting Codex", authMethod: .chatgpt, apiKey: mutation)))
        }
        XCTAssertThrowsError(try encoded(SetProviderConnectionRequest(
            providerID: .codexAppServer, displayName: "Meeting Codex", authMethod: .apiKey)))
        XCTAssertThrowsError(try encoded(SetProviderConnectionRequest(
            providerID: .codexAppServer, displayName: "Meeting Codex", authMethod: .chatgpt,
            endpoint: "https://example.invalid")))
    }

    func testDeviceAuthorizationChallengeIsAcceptedOnlyWhileLoginIsPending() throws {
        var operation = try XCTUnwrap(first("get_provider_auth_status")["operation"] as? [String: Any])
        operation["action"] = "start"
        operation["state"] = "pending"
        operation["authorization_url"] = ProviderSettingsFixtures.authorizationURL()
        operation["user_code"] = ProviderSettingsFixtures.userCode
        let decoded = try decode(ProviderAuthOperation.self, operation)
        XCTAssertEqual(decoded.authorizationURL?.browserURL.absoluteString, ProviderSettingsFixtures.authorizationURL())
        XCTAssertEqual(decoded.userCode?.displayValue, ProviderSettingsFixtures.userCode)
        for field in ["authorization_url", "user_code"] {
            var incomplete = operation
            incomplete[field] = NSNull()
            XCTAssertThrowsError(try decode(ProviderAuthOperation.self, incomplete))
        }
        for state in ["succeeded", "failed", "cancelled", "unknown"] {
            operation["state"] = state
            XCTAssertThrowsError(try decode(ProviderAuthOperation.self, operation))
        }
        operation["state"] = "pending"
        for action in ["cancel", "logout"] {
            operation["action"] = action
            XCTAssertThrowsError(try decode(ProviderAuthOperation.self, operation))
        }
    }

    func testDeviceAuthorizationURLRejectsOtherDestinationsAndBrowserOAuth() {
        let valid = ProviderSettingsFixtures.authorizationURL()
        let replacements = [
            ("https://auth.openai.com", "http://auth.openai.com"),
            ("https://auth.openai.com", "file://auth.openai.com"),
            ("https://auth.openai.com", "https://auth.openai.com.evil.invalid"),
            ("https://auth.openai.com", "https://auth.openai.com@evil.invalid"),
            ("https://auth.openai.com", "https://user@auth.openai.com"),
            ("https://auth.openai.com", "https://auth.openai.com:443"),
            ("/codex/device", "/oauth/authorize?response_type=code"),
            ("/codex/device", "/oauth/token"),
            ("/codex/device", "/codex/other"),
            ("/codex/device", "/codex/%64evice"),
        ]
        for (original, replacement) in replacements {
            XCTAssertNil(ProviderAuthorizationURL(valid.replacingOccurrences(of: original, with: replacement)), replacement)
        }
        for suffix in ["/", "#fragment", "?user_code=ABCD-EFGH", "?redirect_uri=http://localhost:1455/auth/callback", "\n"] {
            XCTAssertNil(ProviderAuthorizationURL(valid + suffix), suffix)
        }
        XCTAssertNil(ProviderAuthorizationURL("javascript:alert(1)"))
        XCTAssertNil(ProviderAuthorizationURL(valid + String(repeating: "x", count: 8192)))
    }

    func testDeviceUserCodeAcceptsOnlyBoundedASCIIAndRemainsRequired() throws {
        for value in ["ABCD-EFGH", "abcd1234", String(repeating: "A", count: 32)] {
            XCTAssertEqual(try decode(ProviderUserCode.self, value).displayValue, value)
        }
        for value in ["", String(repeating: "A", count: 33), " ABCD-EFGH", "ABCD EFGH", "ABCD_EFGH", "ABCD/EFGH", "ABCD\nEFGH", "ＡＢＣＤ-ＥＦＧＨ", "ABCD-ＥFGH"] {
            XCTAssertNil(ProviderUserCode(value))
            XCTAssertThrowsError(try decode(ProviderUserCode.self, value))
        }
        var operation = try XCTUnwrap(first("get_provider_auth_status")["operation"] as? [String: Any])
        operation.removeValue(forKey: "user_code")
        XCTAssertThrowsError(try decode(ProviderAuthOperation.self, operation))
    }

    func testDeviceAuthorizationChallengeNeverAppearsInDiagnosticsOrReflection() throws {
        let url = try XCTUnwrap(ProviderAuthorizationURL(ProviderSettingsFixtures.authorizationURL()))
        let code = try XCTUnwrap(ProviderUserCode(ProviderSettingsFixtures.userCode))
        let operation = ProviderSettingsFixtures.auth(authorizationURL: url)
        for output in [String(describing: url), String(reflecting: url), String(reflecting: code), String(reflecting: operation)] {
            XCTAssertFalse(output.contains(ProviderSettingsFixtures.userCode))
            XCTAssertFalse(output.contains(ProviderSettingsFixtures.authorizationURL()))
            XCTAssertTrue(output.contains("<redacted>"))
        }
        var dumped = ""
        dump(operation, to: &dumped)
        XCTAssertFalse(dumped.contains(ProviderSettingsFixtures.userCode))
        XCTAssertFalse(dumped.contains(ProviderSettingsFixtures.authorizationURL()))
        XCTAssertTrue(dumped.contains("<redacted>"))
    }
}
