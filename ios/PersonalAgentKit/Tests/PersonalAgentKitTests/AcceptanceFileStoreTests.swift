import Foundation
import XCTest
@testable import PersonalAgentKit

final class AcceptanceFileStoreTests: XCTestCase {
    func testAtomicReplacementAndUnknownKeys() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let store = try AcceptanceFileStore(testDirectory: root)
        try store.write(CredentialKey.enrollment, value: Data("old".utf8))
        XCTAssertThrowsError(try store.write("../escape", value: Data()))
        try store.write(CredentialKey.enrollment, value: Data("new".utf8))
        XCTAssertEqual(try store.read(CredentialKey.enrollment), Data("new".utf8))
        try store.closeAndClear()
        XCTAssertThrowsError(try store.write(CredentialKey.enrollment, value: Data()))
    }
    func testFailedReplacementPreservesIdentityAndBackupExclusion() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let store = try AcceptanceFileStore(testDirectory: root)
        try store.write(CredentialKey.enrollment, value: Data("identity".utf8))
        let broken = try AcceptanceFileStore(testDirectory: root, beforeReplace: { throw AcceptanceFileStore.Failure.closed })
        XCTAssertThrowsError(try broken.write(CredentialKey.enrollment, value: Data("replacement".utf8)))
        XCTAssertEqual(try store.read(CredentialKey.enrollment), Data("identity".utf8))
        let file = root.appendingPathComponent(CredentialKey.enrollment)
        XCTAssertEqual(try file.resourceValues(forKeys: [.isExcludedFromBackupKey]).isExcludedFromBackup, true)
        XCTAssertEqual(try root.resourceValues(forKeys: [.isExcludedFromBackupKey]).isExcludedFromBackup, true)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: root.path), [CredentialKey.enrollment])
    }
    func testRejectsSymlinkWithoutFollowingOrDeletingIt() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let store = try AcceptanceFileStore(testDirectory: root)
        let target = root.appendingPathComponent("external")
        try Data("keep".utf8).write(to: target)
        let link = root.appendingPathComponent(CredentialKey.enrollment)
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: target)
        XCTAssertThrowsError(try store.read(CredentialKey.enrollment))
        XCTAssertThrowsError(try store.write(CredentialKey.enrollment, value: Data()))
        XCTAssertThrowsError(try store.closeAndClear())
        XCTAssertEqual(try Data(contentsOf: target), Data("keep".utf8))
    }
}
