import Foundation

/// Public, test-only TLS material. This private key and its password are intentionally public.
/// Never use this identity outside loopback tests. It is not installed in any Keychain.
/// EC P-256 / SHA-256, IP SANs 127.0.0.1 and ::1, validity 2010-01-01 through 2100-01-01.
/// The test-only issue date predates macOS leaf lifetime limits, keeping this fixed
/// identity usable with real SecTrust evaluation without a clock-dependent fixture.
/// Legacy PKCS#12 encryption here is only for Security.framework import compatibility.
enum LoopbackTLSCertificate {
    static let password = "narumi-test-only"
    static let certificateDER = Data(
        base64Encoded: """
        MIIByTCCAXCgAwIBAgIUdrWoogzgmtIXGKi6++dVATOkq4swCgYIKoZIzj0EAwIwNzE1MDMGA1UEAwwsTmFydW1pIHB1YmxpYyB0
        ZXN0LW9ubHkgbG9vcGJhY2sgY2VydGlmaWNhdGUwIBcNMTAwMTAxMDAwMDAwWhgPMjEwMDAxMDEwMDAwMDBaMDcxNTAzBgNVBAMM
        LE5hcnVtaSBwdWJsaWMgdGVzdC1vbmx5IGxvb3BiYWNrIGNlcnRpZmljYXRlMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEzN0a
        BV25dqA3oXJPTBMgIUi6JgbhZYsqmzSI8dbsmA1zc273PFyElJI1LntAtd6Q1EcThtBuNgHIQB9OQEf1KKNYMFYwIQYDVR0RBBow
        GIcEfwAAAYcQAAAAAAAAAAAAAAAAAAAAATAMBgNVHRMBAf8EAjAAMA4GA1UdDwEB/wQEAwIHgDATBgNVHSUEDDAKBggrBgEFBQcD
        ATAKBggqhkjOPQQDAgNHADBEAiAHB8BwtkgTJttRigeZKVu0Olea83hy8+MnWe8zZq65EwIgVMAqVzxKAZ9d8QkJrUEAwwZA6/Uc
        slduMTj4U5ir9K8=
        """, options: .ignoreUnknownCharacters)!

    static let identityPKCS12 = Data(
        base64Encoded: """
        MIIELwIBAzCCA/UGCSqGSIb3DQEHAaCCA+YEggPiMIID3jCCAp8GCSqGSIb3DQEHBqCCApAwggKMAgEAMIIChQYJKoZIhvcNAQcB
        MBwGCiqGSIb3DQEMAQMwDgQIs/M9QSDSOBsCAggAgIICWHR2u+2Fm8HzIsbtpUZhf6m78SukFZYvmjipyMLlNH2I5xVe5URUbldU
        HGDV2qm07PdABc4eQXQSumikIaBT0Q08BOUuNGGJ8vi4Mhul5eRLUETUcvTRGBNdaW0jECt2qkHvL8MSJVyHGgBFuRg6ksozqnl0
        NA+YFr5s1WIRyJlgX6JwwS7+9u/sstQE6FPfvtIJpEkGHxd1Ax+jazBG+GAlqyQmxnCznzJntto9squbgfcQKMM7BLu2gHGD+q6l
        KdS8A6AFemLfr1eN+NxnsyXmDc/8aLLqCcdSHBImTv9GVpYUYL6gEAV/JEloVOrzCC1va90xXDw5EPokdwcgCIO36/ddhZksM89x
        ioHqfGYOcsKBa0hOctokAmzmtfXSHq/IEFglUcjEF2m+6BzUGaANOyZ94t/2TUNxHYKhqtRq2GvGzVtn//a1ZR/nMi3drc5/4K+o
        ApQ4VxCFT3v4rGHuB9bdFPIQugZ/5tOJLHVkPqGZmQENXX/rhvLK6xCUI0S64LU7mRxhkHsRzZxeQs3l2M8GvqyoI7WA2MEBaWYd
        LxiDVFgNgd3laHd05JQQ9/2CtXB3BqIcBdisZmX2ksa8PT/vdVnlEOfnqLjIidu5S06tCg6IZnw6O9wF/vJCBSvT7Sx/6n45pz11
        NXnDlqKsrkKPXyeusz8y2YNI0bUV2WtsFDa2B2ql/pp9rReJSV5P5egU91NnzMoVtbuy4bOkvGxyoFBCvuRX88BO4iyQezqeAosP
        7qOfj+BdBMyMg6LAOovUmlVT37GlFPZ3pMoqsMzIugRSqTCCATcGCSqGSIb3DQEHAaCCASgEggEkMIIBIDCCARwGCyqGSIb3DQEM
        CgECoIG0MIGxMBwGCiqGSIb3DQEMAQMwDgQIJCoYx9vBqWUCAggABIGQO0813BiU8kuk4nF5r6V6Sk3PE6HN4a/gbu/HXwrfEC7Z
        2oqU25rXzzh0PMpMcIQbfrKGYrOhZdgDBGHTiD0RvBbRlcdfmPfqnVBvxsaqGPoD2Bj7rx3La6iYc4DihreSHQn+mOkBgRQllSIF
        TIVqUfkYkukTqfiX5Y4dmyW49G5hBhJGeCjCm50soLlvpyv3MVYwIwYJKoZIhvcNAQkVMRYEFNyB4xHTWJBj77xBpOT57gIz5r73
        MC8GCSqGSIb3DQEJFDEiHiAAbgBhAHIAdQBtAGkALQB0AGUAcwB0AC0AbwBuAGwAeTAxMCEwCQYFKw4DAhoFAAQUe3jl8giS/+tm
        WOkOYgLWdvVcq6UECAxE6Mn/CO/CAgIIAA==
        """, options: .ignoreUnknownCharacters)!

    static var certificatePEM: String {
        let base64 = certificateDER.base64EncodedString()
        let lines = stride(from: 0, to: base64.count, by: 64).map { offset in
            let start = base64.index(base64.startIndex, offsetBy: offset)
            let end = base64.index(start, offsetBy: min(64, base64.count - offset))
            return String(base64[start..<end])
        }
        return "-----BEGIN CERTIFICATE-----\n" + lines.joined(separator: "\n")
            + "\n-----END CERTIFICATE-----\n"
    }
}
