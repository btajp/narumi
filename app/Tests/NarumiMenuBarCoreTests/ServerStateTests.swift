import XCTest

@testable import NarumiMenuBarCore

final class ServerStateTests: XCTestCase {
    let url = URL(string: "http://127.0.0.1:8765/mcp")!

    func testExitTransitions() {
        let starting = ServerState.starting(since: Date())
        XCTAssertEqual(starting.afterExit(code: 2, signaled: false, requested: false), .failed("起動時に終了 (exit 2)"))
        XCTAssertEqual(starting.afterExit(code: 0, signaled: false, requested: false), .stopped(exitCode: 0))
        XCTAssertEqual(starting.afterExit(code: 9, signaled: true, requested: false), .failed("シグナル 9 で終了"))
        XCTAssertEqual(starting.afterExit(code: 9, signaled: true, requested: true), .stopped(exitCode: 9))

        let running = ServerState.running(pid: 42)
        XCTAssertEqual(running.afterExit(code: 0, signaled: false, requested: true), .stopped(exitCode: 0))
        XCTAssertEqual(running.afterExit(code: 1, signaled: false, requested: false), .stopped(exitCode: 1))
        XCTAssertEqual(running.afterExit(code: 11, signaled: true, requested: false), .failed("シグナル 11 で終了"))
    }

    func testStartupTimeout() {
        let starting = ServerState.starting(since: Date())
        XCTAssertEqual(starting.afterStartupTimeout(processAlive: true, exitCode: 0), .failed(ServerState.startupTimeoutMessage))
        XCTAssertEqual(starting.afterStartupTimeout(processAlive: false, exitCode: 3), .stopped(exitCode: 3))
        // Only a starting server can time out.
        XCTAssertEqual(ServerState.running(pid: 1).afterStartupTimeout(processAlive: true, exitCode: 0), .running(pid: 1))
    }

    func testCapabilities() {
        XCTAssertFalse(ServerState.external(url).canRestart)
        XCTAssertFalse(ServerState.notConfigured.canRestart)
        XCTAssertTrue(ServerState.running(pid: 1).canRestart)
        XCTAssertTrue(ServerState.stopped(exitCode: 0).canRestart)
        XCTAssertTrue(ServerState.failed("x").canRestart)
        XCTAssertTrue(ServerState.starting(since: Date()).canRestart)

        XCTAssertTrue(ServerState.running(pid: 1).pollsServerInfo)
        XCTAssertTrue(ServerState.external(url).pollsServerInfo)
        XCTAssertFalse(ServerState.starting(since: Date()).pollsServerInfo)
        XCTAssertFalse(ServerState.failed("x").pollsServerInfo)
    }

    func testTitles() {
        let info = ServerInfoSummary(version: "0.1.0", contractVersion: "1.0.0", recordingCapable: true)
        XCTAssertEqual(ServerStatusText.title(for: .starting(since: Date()), info: nil, reachable: false), "サーバー: 起動中…")
        XCTAssertEqual(
            ServerStatusText.title(for: .running(pid: 7), info: info, reachable: true), "サーバー: 稼働中 v0.1.0 契約 1.0.0")
        XCTAssertEqual(ServerStatusText.title(for: .running(pid: 7), info: info, reachable: false), "サーバー: 応答なし (pid 7)")
        XCTAssertEqual(
            ServerStatusText.title(for: .external(url), info: ServerInfoSummary(version: "0.1.0", recordingCapable: false), reachable: true),
            "サーバー: 外部サーバーに接続 v0.1.0 録画不可")
        XCTAssertEqual(
            ServerStatusText.title(for: .notConfigured, info: nil, reachable: false),
            "サーバー: 未設定（リポジトリを選択してください）")
        XCTAssertEqual(ServerStatusText.title(for: .stopped(exitCode: 2), info: nil, reachable: false), "サーバー: 停止 (exit 2)")
        XCTAssertEqual(ServerStatusText.title(for: .failed("boom"), info: nil, reachable: false), "サーバー: 起動失敗（ログ参照）")
        XCTAssertEqual(ServerState.failed("boom").detail, "boom")
        XCTAssertEqual(ServerState.external(url).detail, "http://127.0.0.1:8765/mcp（このアプリは起動・停止しません）")
    }
}
