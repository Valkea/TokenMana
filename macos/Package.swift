// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "TokenMana",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "TokenMana", path: "Sources/TokenMana")
    ]
)
