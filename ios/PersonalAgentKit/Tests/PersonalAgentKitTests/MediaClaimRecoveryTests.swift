import Foundation
import Testing
@testable import PersonalAgentKit

@Suite("Upload claim recovery")
struct MediaClaimRecoveryTests {
    @Test("only pending or expired unsealed attempts permit PUT", arguments: 0..<9)
    func claimAdmission(_ scenario: Int) async throws {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let cases: [(String, String?, String, Bool)] = [
            ("pending", nil, "in_progress", true),
            ("uploading", RFC3339.string(from: now.addingTimeInterval(-1)), "in_progress", true),
            ("uploading", RFC3339.string(from: now), "in_progress", true),
            ("uploading", RFC3339.string(from: now.addingTimeInterval(1)), "in_progress", false),
            ("uploading", nil, "in_progress", false),
            ("uploading", "invalid-date", "in_progress", false),
            ("uploaded", RFC3339.string(from: now.addingTimeInterval(-1)), "in_progress", false),
            ("uploading", RFC3339.string(from: now.addingTimeInterval(-1)), "tombstoned", false),
            ("uploading", RFC3339.string(from: now).replacingOccurrences(of: "Z", with: ".000000Z"), "in_progress", true),
        ]
        let (state, retryAt, outcome, mayPut) = cases[scenario]
        let backend = ClaimBackend(status: .init(
            mediaID: "media_1", state: state, outcome: outcome, retryAt: retryAt,
            contentSHA256: nil, mime: nil, size: nil,
            declaredWidth: nil, declaredHeight: nil
        ))
        let coordinator = MediaUploadCoordinator(backend: backend, now: { now })
        let pending = PendingMediaUpload(
            idempotencyKey: "00000000-0000-4000-8000-000000000001",
            declaration: .init(mime: "image/jpeg", size: 1,
                               sha256: String(repeating: "a", count: 64), width: 1, height: 1),
            mediaID: "media_1"
        )
        if mayPut {
            let result = try await coordinator.put(pending, bytes: Data([0]))
            #expect(result.contentUploaded)
            #expect(result.mediaID == pending.mediaID)
            #expect(result.idempotencyKey == pending.idempotencyKey)
        } else {
            await #expect(throws: MediaUploadError.self) {
                try await coordinator.put(pending, bytes: Data([0]))
            }
        }
        #expect(await backend.putIDs == (mayPut ? ["media_1"] : []))
    }
}

private actor ClaimBackend: MediaUploadBackend {
    let status: CompletedMediaUpload
    private(set) var putIDs: [String] = []
    init(status: CompletedMediaUpload) { self.status = status }
    func createMediaUpload(
        declaration: MediaUploadDeclaration, idempotencyKey: String
    ) async throws -> CreatedMediaUpload {
        throw AgentClientError.transport("recovery must not create another target")
    }
    func completeMediaUpload(mediaID: String) async throws -> CompletedMediaUpload { status }
    func putMediaContent(mediaID: String, body: Data) async throws -> MediaUploadReceipt {
        putIDs.append(mediaID)
        return .init(mediaID: mediaID, state: "uploaded", mime: "image/jpeg", size: body.count)
    }
}
