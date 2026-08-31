import Foundation
import XCTest
@testable import NarumiMenuBarCore

final class TranscriptionModelFormTests: XCTestCase {
    private func connection(
        revision: Int = 1, enabled: Bool = true, credential: Bool = true,
        authState: ProviderAuthState = .authenticated, providerID: ProviderID = .openaiAPI,
        activeAuth: ProviderActiveAuth? = nil
    ) -> ProviderConnection {
        ProviderSettingsFixtures.connection(
            revision: revision, providerID: providerID, enabled: enabled, credential: credential,
            authState: authState, catalogState: .ready, activeAuth: activeAuth)
    }

    private func provider(
        state: ProviderRuntimeState = .ready, roles: [ProviderRole] = [.llm, .transcription]
    ) -> ProviderDescriptor {
        ProviderDescriptor(
            providerID: .openaiAPI, displayName: "Fixture OpenAI", roles: roles, authMethods: [.apiKey],
            availability: .available, reason: nil, runtime: ProviderSettingsFixtures.provider(state: state).runtime)
    }

    private func model(
        id: String = "whisper-1", availability: ProviderAvailability = .available,
        roles: [ProviderRole] = [.transcription], inputs: [ProviderModality] = [.audio],
        outputs: [ProviderModality] = [.text], timestamp: ProviderTimestampSupport? = nil,
        source: ProviderModelSource = .providerAPI, billing: ProviderBillingKind = .api,
        schema: ProviderParameterSchema = ProviderParameterSchema(), expires: String? = nil
    ) -> ProviderModelDescriptor {
        ProviderModelDescriptor(
            modelID: id, displayName: id, resolvedRevision: nil, inputModalities: inputs, outputModalities: outputs,
            roles: roles, timestampSupport: timestamp ?? (id == "whisper-1" ? .word : .diarizedSegment),
            contextWindow: nil, maxOutputTokens: nil, parameterSchema: schema,
            availability: availability, reason: nil, source: source, fetchedAt: ProviderSettingsFixtures.timestamp,
            billing: ProviderModelBilling(
                kind: billing, inputUSDPerMillionTokens: nil, outputUSDPerMillionTokens: nil,
                audioUSDPerMinute: nil, fetchedAt: nil), availabilityExpiresOn: expires)
    }

    private func catalog(
        revision: Int = 1, connectionID: String = ProviderSettingsFixtures.connectionID,
        models: [ProviderModelDescriptor]? = nil, state: ProviderCatalogState = .ready
    ) -> ListProviderModelsResponse {
        ListProviderModelsResponse(
            connectionID: connectionID, connectionRevision: revision, models: models ?? [model()],
            nextCursor: nil, catalogState: state, fetchedAt: ProviderSettingsFixtures.timestamp)
    }

    private func form(modelID: String = "whisper-1", epoch: Int = 0) -> TranscriptionModelForm {
        TranscriptionModelForm(selection: TranscriptionModelSelection(
            connectionID: ProviderSettingsFixtures.connectionID, connectionRevision: 1,
            modelID: modelID, cacheEpoch: epoch))
    }

    private func validate(
        _ form: TranscriptionModelForm, connections: [ProviderConnection]? = nil,
        models: [ProviderModelDescriptor]? = nil, policy: String = "api_ok", language: String = "ja",
        supportedProviders: [String] = ["openai-api"], providers: [ProviderDescriptor]? = nil
    ) -> String? {
        form.validationMessage(
            connections: connections ?? [connection()], catalog: catalog(models: models),
            externalSendPolicy: policy, language: language, supportedProviders: supportedProviders,
            providers: providers ?? [provider()])
    }

    func testLocalModeClearsOverrideWithoutLosingTheDraft() {
        var form = form(epoch: 4)
        let saved = form.selection
        form.mode = .local
        XCTAssertNil(form.selection)
        XCTAssertNil(validate(form, connections: [], policy: "local_only", language: "", supportedProviders: []))
        form.mode = .selected
        XCTAssertEqual(form.selection, saved)
    }

    func testDraftRequiresExplicitConnectionAndKnownModel() {
        var form = TranscriptionModelForm()
        form.mode = .selected
        XCTAssertNil(form.selection)
        form.selectConnection(connection())
        XCTAssertNil(form.selection)
        form.selectModel(model())
        XCTAssertEqual(form.selection?.provider, "openai-api")
        XCTAssertEqual(form.selection?.parameters, TranscriptionModelSelection.Parameters())
        XCTAssertEqual(form.selection?.cacheEpoch, 0)
        XCTAssertNil(validate(form))
        form.selectModel(model(id: "gpt-4o-transcribe"))
        XCTAssertNil(form.selection)
    }

    func testRevisionChangeRequiresModelReselectionWithoutAdvancingRetryEpoch() {
        var form = form(epoch: 8)
        let identity = form.catalogReadIdentity
        form.selectConnection(connection())
        XCTAssertEqual(form.modelID, "whisper-1")
        form.selectConnection(connection(revision: 2))
        XCTAssertEqual(form.connectionRevision, 2)
        XCTAssertEqual(form.modelID, "")
        XCTAssertEqual(form.cacheEpoch, 8)
        XCTAssertNil(form.selection)
        XCTAssertNotEqual(form.catalogReadIdentity, identity)
    }

    func testNonOpenAIConnectionCannotReplaceTheSavedDraft() {
        var form = form()
        let original = form
        form.selectConnection(connection(providerID: .codexAppServer))
        XCTAssertEqual(form, original)
        form.selectConnection(nil)
        XCTAssertEqual(form.connectionID, "")
        XCTAssertNil(form.connectionRevision)
        XCTAssertEqual(form.modelID, "")
    }

    func testInvalidProgrammaticProviderIsNotSilentlyConvertedToOpenAI() {
        let form = TranscriptionModelForm(selection: TranscriptionModelSelection(
            provider: "codex-app-server", connectionID: ProviderSettingsFixtures.connectionID,
            connectionRevision: 1, modelID: "whisper-1"))
        XCTAssertEqual(form.provider, "codex-app-server")
        XCTAssertNil(form.selection)
        XCTAssertNotNil(validate(form))
    }

    func testBothTimestampedModelsAreSelectableWithoutInventingPricesOrLimits() {
        for id in TranscriptionModelSelection.modelIDs {
            let model = model(id: id)
            XCTAssertNil(TranscriptionModelForm.modelUnavailableReason(model))
            XCTAssertTrue(TranscriptionModelForm.isTranscriptionModel(model))
            XCTAssertNil(model.contextWindow)
            XCTAssertNil(model.maxOutputTokens)
            XCTAssertNil(model.billing.audioUSDPerMinute)
            XCTAssertNil(validate(form(modelID: id), models: [model]))
        }
    }

    func testUnknownOrMismatchedCapabilitiesAreNotInferredFromAModelName() {
        let invalid = [
            model(id: "gpt-4o-transcribe"), model(availability: .unverified), model(availability: .retired),
            model(roles: [.llm]), model(inputs: [.text]), model(outputs: [.audio]),
            model(timestamp: ProviderTimestampSupport.none), model(timestamp: .segment), model(timestamp: .diarizedSegment),
            model(id: "gpt-4o-transcribe-diarize", timestamp: .word), model(source: .runtime),
            model(billing: .subscription), model(billing: .unknown),
            model(schema: ProviderParameterSchema(properties: ["max_tokens": ProviderModelParameter(type: .integer)])),
            model(schema: ProviderParameterSchema(properties: ["prompt": ProviderModelParameter(type: .string)])),
        ]
        for model in invalid {
            XCTAssertNotNil(TranscriptionModelForm.modelUnavailableReason(model), model.modelID)
            XCTAssertFalse(TranscriptionModelForm.isTranscriptionModel(model), model.modelID)
        }
    }

    func testExpirationUsesThePublishedUTCDateAndMalformedDatesFailClosed() throws {
        let expiration = try XCTUnwrap(ISO8601DateFormatter().date(from: "2026-08-29T00:00:00Z"))
        let model = model(expires: "2026-08-29")
        XCTAssertNil(TranscriptionModelForm.modelUnavailableReason(model, at: expiration.addingTimeInterval(-1)))
        XCTAssertNotNil(TranscriptionModelForm.modelUnavailableReason(model, at: expiration))
        XCTAssertNotNil(TranscriptionModelForm.modelUnavailableReason(self.model(expires: "2026-02-30"), at: expiration))
    }

    func testAPIOptInAndRealLanguageIdentifiersAreRequired() {
        for policy in ["", "local_only", "subscription_ok"] {
            XCTAssertTrue(validate(form(), policy: policy)?.contains("api_ok") == true)
        }
        for language in ["", "JA", "ja-JP", "xx", "zz", "auto "] {
            XCTAssertTrue(validate(form(), language: language)?.contains("ISO 639-1") == true)
        }
        XCTAssertNil(validate(form(), language: "auto"))
        XCTAssertNotNil(validate(form(), supportedProviders: []))
        XCTAssertNotNil(validate(form(), supportedProviders: ["codex-app-server"]))
    }

    func testAuthenticationCredentialAndRuntimeAreSeparateSelectionRequirements() {
        for connection in [
            connection(enabled: false), connection(credential: false), connection(authState: .unconfigured),
            connection(authState: .unverified), connection(authState: .failed), connection(providerID: .codexAppServer),
            connection(activeAuth: ProviderSettingsFixtures.activeAuth()),
        ] {
            XCTAssertNotNil(validate(form(), connections: [connection]))
        }
        for state in [ProviderRuntimeState.notPrepared, .preparing, .unavailable, .failed, .unknown] {
            XCTAssertNotNil(validate(form(), providers: [provider(state: state)]))
        }
        XCTAssertNotNil(validate(form(), providers: []))
        XCTAssertNotNil(validate(form(), providers: [provider(roles: [.llm])]))
    }

    func testSavedRevisionCatalogIdentityAndReadinessMustMatch() {
        let form = form()
        XCTAssertNotNil(validate(form, connections: []))
        XCTAssertTrue(validate(form, connections: [connection(revision: 2)])?.contains("選び直す") == true)
        let invalidCatalogs: [ListProviderModelsResponse?] = [
            nil, catalog(revision: 2), catalog(connectionID: "conn-abcdef012345"),
            catalog(state: .stale), catalog(state: .unfetched), catalog(models: []),
        ]
        for catalog in invalidCatalogs {
            XCTAssertNotNil(form.validationMessage(
                connections: [connection()], catalog: catalog, externalSendPolicy: "api_ok", language: "ja",
                supportedProviders: ["openai-api"], providers: [provider()]))
        }
    }
}
