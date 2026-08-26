// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "narumi-app",
    platforms: [.macOS(.v15)],
    products: [
        .library(name: "NarumiRecorderKit", targets: ["NarumiRecorderKit"]),
        .executable(name: "narumi-recorder", targets: ["narumi-recorder"]),
        .executable(name: "NarumiMenuBar", targets: ["NarumiMenuBar"]),
    ],
    targets: [
        .target(name: "NarumiRecorderKit"),
        .executableTarget(name: "narumi-recorder", dependencies: ["NarumiRecorderKit"]),
        .executableTarget(name: "NarumiMenuBar"),
        .testTarget(name: "NarumiRecorderKitTests", dependencies: ["NarumiRecorderKit"]),
    ],
    swiftLanguageModes: [.v6]
)
