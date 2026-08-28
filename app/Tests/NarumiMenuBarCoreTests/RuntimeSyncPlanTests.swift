import XCTest

@testable import NarumiMenuBarCore

final class RuntimeSyncPlanTests: XCTestCase {
    // Paths with spaces and Japanese: they travel as argv elements, never through a shell.
    let bundle = BundledRuntime(
        root: URL(
            fileURLWithPath: "/Applications/なるみ 検証.app/Contents/Resources/runtime",
            isDirectory: true))
    let paths = RuntimePaths(
        dataRoot: URL(
            fileURLWithPath: "/Users/山田 太郎/Library/Application Support/narumi", isDirectory: true))
    let manifest = RuntimeManifest(
        appVersion: "0.1.0", python: "3.13", uvVersion: "0.11.25",
        wheels: [
            // Intentionally out of sorted order in source: the plan must sort.
            "narumi_server-0.1.0-py3-none-any.whl": "bbbb",
            "narumi-0.1.0-py3-none-any.whl": "aaaa",
        ],
        requirementsSHA256: "cccc")

    var runtimeRoot: String { "/Applications/なるみ 検証.app/Contents/Resources/runtime" }
    var home: String { "/Users/山田 太郎/Library/Application Support/narumi/runtime" }

    func testStepsInSpecOrderWithExactArgv() throws {
        let plan = RuntimeSyncPlan(bundle: bundle, paths: paths, manifest: manifest)
        XCTAssertEqual(plan.steps.map(\.name), [
            "Python 取得", "venv 作成", "依存インストール", "アプリ本体インストール", "venv 切り替え（旧版を保持）",
        ])

        guard case .run(_, let python) = plan.steps[0],
            case .run(_, let venv) = plan.steps[1],
            case .run(_, let requirements) = plan.steps[2],
            case .run(_, let wheels) = plan.steps[3],
            case .activate = plan.steps[4]
        else {
            return XCTFail("unexpected step kinds: \(plan.steps)")
        }

        // 1. uv python install 3.13 (into <data root>/runtime/python)
        XCTAssertEqual(python.executable.path, "\(runtimeRoot)/uv")
        XCTAssertEqual(python.arguments, ["python", "install", "3.13"])
        XCTAssertEqual(python.environment, ["UV_PYTHON_INSTALL_DIR": "\(home)/python"])

        // 2. uv venv into venv.new (never touching the live venv), same managed Python dir.
        // --relocatable keeps entry-point scripts alive across the venv.new → venv rename.
        XCTAssertEqual(venv.executable.path, "\(runtimeRoot)/uv")
        XCTAssertEqual(
            venv.arguments,
            ["venv", "\(home)/venv.new", "--clear", "--relocatable", "--python", "3.13"])
        XCTAssertEqual(venv.environment, ["UV_PYTHON_INSTALL_DIR": "\(home)/python"])

        // 3. pinned third-party deps, hash-checked
        XCTAssertEqual(
            requirements.arguments,
            [
                "pip", "install", "--python", "\(home)/venv.new",
                "--require-hashes", "-r", "\(runtimeRoot)/requirements.txt",
            ])

        // 4. our wheels, no dependency resolution, sorted for determinism
        XCTAssertEqual(
            wheels.arguments,
            [
                "pip", "install", "--python", "\(home)/venv.new", "--no-deps",
                "\(runtimeRoot)/wheels/narumi-0.1.0-py3-none-any.whl",
                "\(runtimeRoot)/wheels/narumi_server-0.1.0-py3-none-any.whl",
            ])

        // Activation retains the old venv and marker. Committing installed.json is NOT a
        // sync step: the launcher must first verify the candidate server's identity.
        XCTAssertEqual(plan.steps.count, 5)
    }

    func testNoShellAnywhere() {
        let plan = RuntimeSyncPlan(bundle: bundle, paths: paths, manifest: manifest)
        for step in plan.steps {
            guard case .run(_, let command) = step else {
                continue
            }
            XCTAssertEqual(command.executable.lastPathComponent, "uv")
            // Paths land in argv verbatim; nothing is interpolated into shell text.
            XCTAssertFalse(command.arguments.contains { $0.contains("$") })
        }
    }

    func testEmptyWheelsSkipsTheWheelStep() {
        var noWheels = manifest
        noWheels.wheels = [:]
        let plan = RuntimeSyncPlan(bundle: bundle, paths: paths, manifest: noWheels)
        XCTAssertEqual(plan.steps.map(\.name), [
            "Python 取得", "venv 作成", "依存インストール", "venv 切り替え（旧版を保持）",
        ])
    }

    func testRuntimePathsLayout() {
        XCTAssertEqual(paths.root.path, home)
        XCTAssertEqual(paths.pythonDir.path, "\(home)/python")
        XCTAssertEqual(paths.venv.path, "\(home)/venv")
        XCTAssertEqual(paths.venvStaging.path, "\(home)/venv.new")
        XCTAssertEqual(paths.venvPrevious.path, "\(home)/venv.previous")
        XCTAssertEqual(paths.transactionJournal.path, "\(home)/installation.json")
        XCTAssertEqual(paths.installedManifest.path, "\(home)/installed.json")
        XCTAssertEqual(paths.serverExecutable.path, "\(home)/venv/bin/narumi-server")
    }

    func testBundledRuntimeLayout() {
        XCTAssertEqual(bundle.uv.path, "\(runtimeRoot)/uv")
        XCTAssertEqual(bundle.manifest.path, "\(runtimeRoot)/manifest.json")
        XCTAssertEqual(bundle.requirements.path, "\(runtimeRoot)/requirements.txt")
        XCTAssertEqual(bundle.wheelsDir.path, "\(runtimeRoot)/wheels")
        XCTAssertEqual(bundle.contractsDir.path, "\(runtimeRoot)/contracts")
        XCTAssertEqual(bundle.wheel("a.whl").path, "\(runtimeRoot)/wheels/a.whl")
    }
}
