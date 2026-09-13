// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "MishkanEngineeringFixture",
    targets: [
        .target(name: "Fixture", path: "swift/Sources"),
        .testTarget(name: "FixtureTests", dependencies: ["Fixture"], path: "swift/Tests"),
    ]
)
