import Darwin
import Foundation
import NarumiMenuBarCore

@main
struct KeychainMain {
    static func main() {
        let result: KeychainHelperResult
        if CommandLine.arguments.count != 1 {
            result = .invalidRequest()
        } else {
            do {
                var input = Data()
                let limit = KeychainHelperProtocol.maximumRequestBytes
                while input.count <= limit {
                    let remaining = min(4096, limit + 1 - input.count)
                    guard let chunk = try FileHandle.standardInput.read(upToCount: remaining),
                          !chunk.isEmpty else { break }
                    input.append(chunk)
                }
                result = KeychainHelperProtocol.handle(input, store: KeychainSecretStore())
            } catch {
                result = .unavailable()
            }
        }
        do {
            try FileHandle.standardOutput.write(contentsOf: result.output)
        } catch {
            exit(1)
        }
        exit(result.exitStatus)
    }
}
