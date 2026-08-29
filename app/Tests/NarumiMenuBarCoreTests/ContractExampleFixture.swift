import Foundation

enum ContractExampleFixture {
    struct Missing: Error {}

    static func outputs(tool: String) throws -> [Data] {
        var directory = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        while directory.path != directory.deletingLastPathComponent().path {
            let file = directory.appendingPathComponent("contracts/tools/\(tool).json")
            if FileManager.default.fileExists(atPath: file.path) {
                let root = try JSONSerialization.jsonObject(with: Data(contentsOf: file)) as? [String: Any]
                let examples = root?["examples"] as? [String: Any]
                let output = examples?["output"] as? [Any] ?? []
                return try output.map { try JSONSerialization.data(withJSONObject: $0) }
            }
            directory.deleteLastPathComponent()
        }
        throw Missing()
    }
}
