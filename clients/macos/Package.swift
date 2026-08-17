// swift-tools-version: 6.0
import PackageDescription

let package = Package(
  name: "AgnesDesktop",
  platforms: [
    .macOS(.v14)
  ],
  products: [
    .executable(name: "AgnesDesktop", targets: ["AgnesDesktop"])
  ],
  targets: [
    .executableTarget(
      name: "AgnesDesktop",
      resources: [
        .process("Resources")
      ],
      swiftSettings: [
        .swiftLanguageMode(.v5)
      ]
    ),
    .testTarget(
      name: "AgnesDesktopTests",
      dependencies: ["AgnesDesktop"],
      swiftSettings: [
        .swiftLanguageMode(.v5)
      ]
    ),
  ]
)
