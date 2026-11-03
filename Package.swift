// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "FlightOps",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "FlightOps", path: "Sources/FlightOps",
                          swiftSettings: [.swiftLanguageMode(.v5)])
    ]
)
