import Foundation
import Security

/// The TLS challenge completes before URLSession can write HTTP headers or body bytes.
/// A matching leaf alone is insufficient: validity and numeric-host SANs are checked as well.
final class MCPPinnedServerTrust: NSObject, URLSessionTaskDelegate, Sendable {
    private let bootstrap: MCPServerBootstrap
    private let rejection = MCPTrustRejection()
    var rejectedPeer: Bool { rejection.value }

    init(bootstrap: MCPServerBootstrap) {
        self.bootstrap = bootstrap
    }

    func urlSession(
        _ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping @Sendable (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        let space = challenge.protectionSpace
        guard space.authenticationMethod == NSURLAuthenticationMethodServerTrust,
            space.host == MCPServerEndpoint.trustHost(bootstrap.url),
            space.port == bootstrap.url.port,
            let trust = space.serverTrust, accepts(trust)
        else {
            rejection.reject()
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        completionHandler(.useCredential, URLCredential(trust: trust))
    }

    func accepts(_ trust: SecTrust) -> Bool {
        guard let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate],
            let leaf = chain.first,
            MCPServerBootstrap.fingerprint(SecCertificateCopyData(leaf) as Data) == bootstrap.certificateSHA256,
            let der = try? bootstrap.certificateDER(),
            let pinnedCertificate = SecCertificateCreateWithData(nil, der as CFData)
        else { return false }
        let policy = SecPolicyCreateSSL(true, MCPServerEndpoint.trustHost(bootstrap.url) as CFString)
        guard SecTrustSetPolicies(trust, policy) == errSecSuccess,
            SecTrustSetAnchorCertificates(trust, [pinnedCertificate] as CFArray) == errSecSuccess,
            SecTrustSetAnchorCertificatesOnly(trust, true) == errSecSuccess,
            SecTrustSetNetworkFetchAllowed(trust, false) == errSecSuccess
        else { return false }
        return SecTrustEvaluateWithError(trust, nil)
    }

    func urlSession(
        _ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void
    ) {
        completionHandler(nil)
    }
}

private final class MCPTrustRejection: @unchecked Sendable {
    private let lock = NSLock()
    private var rejected = false
    var value: Bool { lock.withLock { rejected } }
    func reject() { lock.withLock { rejected = true } }
}
