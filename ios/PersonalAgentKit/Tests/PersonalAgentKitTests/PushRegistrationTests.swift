import XCTest
@testable import PersonalAgentKit

/// `DEV-040`. The half of push registration that can be wrong without anyone
/// noticing until a push silently lands nowhere.
final class PushRegistrationTests: XCTestCase {

    func testHexEncodingMatchesTheWireContract() {
        let raw = Data([0x00, 0x0f, 0xa1, 0xff])
        XCTAssertEqual(PushRegistration.hexToken(from: raw), "000fa1ff")
    }

    func testHexEncodingKeepsLeadingZeroBytes() {
        // The bug a naive encoder has: 0x0f becoming "f" shortens the token and
        // Apple rejects it much later, in a place that looks like a server fault.
        let raw = Data([0x00, 0x00, 0x01])
        XCTAssertEqual(PushRegistration.hexToken(from: raw), "000001")
    }

    func testHexEncodingIsNotTheDataDescription() {
        // `String(describing: Data)` yields "4 bytes", which is plausible enough
        // in a log to survive review and is not a token.
        let raw = Data([0xde, 0xad, 0xbe, 0xef])
        let encoded = PushRegistration.hexToken(from: raw)
        XCTAssertEqual(encoded, "deadbeef")
        XCTAssertFalse(encoded.contains("bytes"))
    }

    func testAFullLengthTokenRoundTripsToLowercaseHex() {
        let raw = Data((0..<32).map { UInt8($0) })
        let encoded = PushRegistration.hexToken(from: raw)
        XCTAssertEqual(encoded.count, 64)
        XCTAssertTrue(PushRegistration.isWellFormed(encoded))
    }

    // --- when to upload -------------------------------------------------------

    func testAFirstTokenIsAlwaysUploaded() {
        XCTAssertTrue(
            PushRegistration.shouldUpload(newToken: "abcd", lastConfirmed: nil)
        )
        XCTAssertTrue(
            PushRegistration.shouldUpload(newToken: "abcd", lastConfirmed: "")
        )
    }

    func testAnUnchangedTokenIsNotReUploaded() {
        XCTAssertFalse(
            PushRegistration.shouldUpload(newToken: "abcd", lastConfirmed: "abcd")
        )
    }

    func testAChangedTokenIsUploaded() {
        // The failure this exists for: iOS reissues a token on reinstall or on
        // restore to a new device, and the server's stored one is then dead
        // while Apple keeps accepting pushes for it.
        XCTAssertTrue(
            PushRegistration.shouldUpload(newToken: "beef", lastConfirmed: "abcd")
        )
    }

    // --- what the server would refuse ----------------------------------------

    func testWellFormedAcceptsLowercaseHexOfEvenLength() {
        XCTAssertTrue(PushRegistration.isWellFormed("00ff"))
        XCTAssertTrue(PushRegistration.isWellFormed(String(repeating: "a", count: 64)))
    }

    func testWellFormedRejectsWhatTheServerWould() {
        XCTAssertFalse(PushRegistration.isWellFormed(""), "empty")
        XCTAssertFalse(PushRegistration.isWellFormed("abc"), "odd length")
        XCTAssertFalse(PushRegistration.isWellFormed("00FF"), "uppercase")
        XCTAssertFalse(PushRegistration.isWellFormed("00 ff"), "whitespace")
        XCTAssertFalse(PushRegistration.isWellFormed("zzzz"), "not hex")
        XCTAssertFalse(PushRegistration.isWellFormed("32 bytes"), "a description")
    }
}
