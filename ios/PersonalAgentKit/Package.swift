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
    // iOS 26 is required by the app target, and it is what lets this package use
    // the iOS 26 `SpeechAnalyzer` transcription API without `@available` guards.
    // macOS deliberately stays at 14: `swift test` builds this package for the
    // host, and raising it would be a decision about where the ASR code lives,
    // not a consequence of the iOS target.
    // `"26.0"` is a string rather than `.v26` on purpose: the `.vNN` enum cases
    // are pinned to the manifest's tools-version, and this toolchain's
    // `swift-tools-version: 6.0` surface has no `.v26` case (SourceKit rejects
    // it). The string form is version-agnostic and accepted here.
    platforms: [.iOS("26.0"), .macOS(.v14)],
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
