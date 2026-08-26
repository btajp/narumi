// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "narumi-app",
    platforms: [.macOS(.v15)],
    products: [
        .library(name: "NarumiRecorderKit", targets: ["NarumiRecorderKit"]),
        .library(name: "NarumiMenuBarCore", targets: ["NarumiMenuBarCore"]),
        .executable(name: "narumi-recorder", targets: ["narumi-recorder"]),
        .executable(name: "NarumiMenuBar", targets: ["NarumiMenuBar"]),
    ],
    targets: [
        .target(name: "NarumiRecorderKit"),
        // Foundation-only logic of the menu bar app (server config / launch command / state),
        // kept free of AppKit so `swift test` covers it.
        .target(name: "NarumiMenuBarCore"),
        .executableTarget(name: "narumi-recorder", dependencies: ["NarumiRecorderKit"]),
        .executableTarget(name: "NarumiMenuBar", dependencies: ["NarumiMenuBarCore"]),
        .testTarget(name: "NarumiRecorderKitTests", dependencies: ["NarumiRecorderKit"]),
        .testTarget(name: "NarumiMenuBarCoreTests", dependencies: ["NarumiMenuBarCore"]),
    ],
    swiftLanguageModes: [.v6]
)
