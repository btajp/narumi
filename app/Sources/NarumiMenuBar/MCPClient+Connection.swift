import Darwin
import Foundation
import NarumiMenuBarCore

extension MCPClient {
    /// Changes only the expected local launch context. The endpoint and pin still have to
    /// match a freshly validated owner-only bootstrap before the next MCP initialization.
    func configure(_ configuration: ServerConfig) throws {
        try configuration.validateSecureEndpoint()
        if configuration != config {
            reset()
            config = configuration
        }
    }

    func prepareConnection(expectedProcessID: Int32? = nil, expectedProcessGroup: Int32? = nil) throws {
        try config.validateSecureEndpoint()
        if bootstrap == nil || transport == nil {
            let loader: any MCPServerBootstrapLoading
            if let bootstrapLoader {
                loader = bootstrapLoader
            } else {
                let helper = try config.validatedKeychainHelper()
                loader = MCPServerBootstrapReader(
                    dataRoot: config.bootstrapDataRoot, secrets: KeychainHelperSecretReader(helperURL: helper))
            }
            let connection = try loader.load(expectedURL: config.serverURL)
            bootstrap = connection.bootstrap
            transport = transportFactory(connection)
        }
        guard let bootstrap else { throw MCPConnectionError.bootstrapUnavailable }
        if let expectedProcessID {
            let sameProcess = bootstrap.pid == expectedProcessID
            let sameOwnedGroup = expectedProcessGroup.map { $0 > 0 && getpgid(bootstrap.pid) == $0 } ?? false
            guard sameProcess || sameOwnedGroup else {
                reset()
                throw MCPConnectionError.connectionChanged
            }
        }
    }
}
