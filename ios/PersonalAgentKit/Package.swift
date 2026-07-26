// swift-tools-version: 6.0
//
// `DEV-029`. Everything the iPhone app does that is *not* SwiftUI lives here, in
// a package that builds and tests on macOS with `swift test` -- no Xcode project,
// no simulator, no signing. The reason is the same one that put the Python wire
// contract in its own module: the encoding either matches the server byte for
// byte or a real device cannot authenticate, and that is worth checking on every
// commit rather than only when someone opens Xcode.
//
// The app target depends on this package and adds the parts that genuinely need
// a device: the Secure Enclave key and the screens.

import PackageDescription

let package = Package(
    name: "PersonalAgentKit",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [
        .library(name: "PersonalAgentKit", targets: ["PersonalAgentKit"])
    ],
    targets: [
        .target(name: "PersonalAgentKit"),
        .testTarget(
            name: "PersonalAgentKitTests",
            dependencies: ["PersonalAgentKit"]
        ),
    ]
)
