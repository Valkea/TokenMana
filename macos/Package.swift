// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "CreditsTracker",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "CreditsTracker", path: "Sources/CreditsTracker")
    ]
)
