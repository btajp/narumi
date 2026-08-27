import XCTest

@testable import NarumiMenuBarCore

final class ToolResponseErrorMessageTests: XCTestCase {
    private struct ReflectedError: Error, CustomStringConvertible {
        let description: String
    }

    func testGaiaSaveDecodeFailureNeverReflectsResponseValues() throws {
        let secret = "example-reflected-response-key"
        let data = Data("""
            {"connection":{"url":"http://127.0.0.1:4111/mcp","has_api_key":true,"source":"\(secret)"}}
            """.utf8)
        XCTAssertThrowsError(try JSONDecoder().decode(GaiaConnectionResponse.self, from: data)) { error in
            XCTAssertTrue(String(describing: error).contains(secret))
            let message = ToolResponseErrorMessage.decoding(toolName: ToolCatalog.setGaiaConnection, error: error)
            XCTAssertEqual(message, "set_gaia_connection の応答を解釈できません（安全のため応答の詳細は表示しません）")
            XCTAssertFalse(message.contains(secret))
        }
    }

    func testGaiaSaveUsesSameMessageForEveryDecodingFailure() {
        let first = ToolResponseErrorMessage.decoding(
            toolName: ToolCatalog.setGaiaConnection,
            error: ReflectedError(description: "example-first-key"))
        let second = ToolResponseErrorMessage.decoding(
            toolName: ToolCatalog.setGaiaConnection,
            error: ReflectedError(description: "example-second-key"))
        XCTAssertEqual(first, second)
        XCTAssertFalse(first.contains("example-"))
    }

    func testOtherToolErrorsKeepTheirExistingDiagnosticText() {
        let error = ReflectedError(description: "Missing required field")
        for tool in ToolCatalog.allUsed where tool != ToolCatalog.setGaiaConnection {
            XCTAssertEqual(
                ToolResponseErrorMessage.decoding(toolName: tool, error: error),
                "\(tool) の応答を解釈できません: Missing required field")
        }
    }
}
