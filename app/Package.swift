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
    dependencies: [
        // In-app updates (spec 2026-08-27-narumi-app-distribution-design.md §2). Binary
        // xcframework; a dependency of the NarumiMenuBar executable only, so the libraries
        // and their tests never link Sparkle.
        .package(url: "https://github.com/sparkle-project/Sparkle", from: "2.9.6")
    ],
    targets: [
        .target(name: "NarumiRecorderKit"),
        // Foundation-only logic of the menu bar app (server config / launch command / state),
        // kept free of AppKit so `swift test` covers it.
        .target(name: "NarumiMenuBarCore"),
        .executableTarget(name: "narumi-recorder", dependencies: ["NarumiRecorderKit"]),
        .executableTarget(
            name: "NarumiMenuBar",
            dependencies: [
                "NarumiMenuBarCore",
                .product(name: "Sparkle", package: "Sparkle"),
            ],
            linkerSettings: [
                // The released .app carries Sparkle.framework in Contents/Frameworks
                // (scripts/build-app.sh); dev builds resolve it from the SwiftPM build dir.
                .unsafeFlags(["-Xlinker", "-rpath", "-Xlinker", "@executable_path/../Frameworks"])
            ]
        ),
        .testTarget(name: "NarumiRecorderKitTests", dependencies: ["NarumiRecorderKit"]),
        .testTarget(name: "NarumiMenuBarCoreTests", dependencies: ["NarumiMenuBarCore"]),
    ],
    swiftLanguageModes: [.v6]
)
