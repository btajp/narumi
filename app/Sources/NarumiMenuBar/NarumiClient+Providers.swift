import NarumiMenuBarCore

extension NarumiClient: ProviderSettingsClient {
    func listProviders() async throws -> ListProvidersResponse {
        try await providerCall(ToolCatalog.listProviders)
    }

    func listProviderConnections() async throws -> ListProviderConnectionsResponse {
        try await providerCall(ToolCatalog.listProviderConnections)
    }

    func setProviderConnection(_ request: SetProviderConnectionRequest) async throws -> ProviderConnectionResponse {
        try await providerCall(ToolCatalog.setProviderConnection, request)
    }

    func deleteProviderConnection(_ request: DeleteProviderConnectionRequest) async throws -> DeleteProviderConnectionResponse {
        try await providerCall(ToolCatalog.deleteProviderConnection, request)
    }

    func authenticateProviderConnection(_ request: AuthenticateProviderConnectionRequest) async throws -> ProviderAuthResponse {
        try await providerCall(ToolCatalog.authenticateProviderConnection, request)
    }

    func providerAuthStatus(_ request: GetProviderAuthStatusRequest) async throws -> ProviderAuthResponse {
        try await providerCall(ToolCatalog.getProviderAuthStatus, request)
    }

    func testProviderConnection(_ request: TestProviderConnectionRequest) async throws -> ProviderConnectionTestResult {
        try await providerCall(ToolCatalog.testProviderConnection, request)
    }

    func listProviderModels(_ request: ListProviderModelsRequest) async throws -> ListProviderModelsResponse {
        try await providerCall(ToolCatalog.listProviderModels, request)
    }

    func verifyProviderModel(_ request: VerifyProviderModelRequest) async throws -> VerifyProviderModelResponse {
        try await providerCall(ToolCatalog.verifyProviderModel, request)
    }

    func prepareProviderRuntime(_ request: PrepareProviderRuntimeRequest) async throws -> PrepareProviderRuntimeResponse {
        try await providerCall(ToolCatalog.prepareProviderRuntime, request)
    }

    private func providerCall<T: Decodable>(_ name: String) async throws -> T {
        do { return try await call(name) }
        catch let failure as ToolFailure { throw ProviderSettingsFailure(code: failure.code) }
        catch { throw ProviderSettingsFailure(.internalError) }
    }

    private func providerCall<T: Decodable, Request: Encodable>(_ name: String, _ request: Request) async throws -> T {
        do { return try await call(name, Self.arguments(request)) }
        catch let failure as ToolFailure { throw ProviderSettingsFailure(code: failure.code) }
        catch { throw ProviderSettingsFailure(.internalError) }
    }
}
