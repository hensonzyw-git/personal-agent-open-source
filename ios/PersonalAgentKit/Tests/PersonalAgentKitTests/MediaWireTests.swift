import Foundation
import Testing

@testable import PersonalAgentKit

@Suite("The CAP-003 media wire and recovery boundary")
struct MediaWireTests {
    @Test("a structured pending send round-trips its exact ordered media reference")
    func pendingSendRoundTrip() throws {
        let pending = ChatTimeline.PendingSend(
            idempotencyKey: "00000000-0000-4000-8000-000000000001",
            conversationID: "timeline",
            text: "这张账单",
            parts: [.text("这张账单"), .imageReference(mediaID: "media_1")],
            clarificationOf: nil,
            operationID: nil
        )
        let restored = try JSONDecoder().decode(
            ChatTimeline.PendingSend.self, from: JSONEncoder().encode(pending)
        )
        #expect(restored == pending)
    }

    @Test("a completed upload keeps one create key and only returns ready evidence")
    func uploadKeepsCreateKey() async throws {
        let backend = MediaBackendStub()
        let coordinator = MediaUploadCoordinator(backend: backend)
        let declaration = MediaUploadDeclaration(
            mime: "image/jpeg", size: 3,
            sha256: String(repeating: "a", count: 64), width: 1, height: 3
        )
        let pending = await coordinator.begin(declaration: declaration)
        let result = try await coordinator.upload(pending, bytes: Data([1, 2, 3]))
        #expect(result.pending.mediaID == "media_1")
        #expect(result.completed.isReady)
        #expect(await backend.createKeys == [pending.idempotencyKey])
        #expect(await backend.putIDs == ["media_1"])
        #expect(await backend.completeIDs == ["media_1"])
    }

    @Test("in-progress completion is never an image reference")
    func incompleteCompletionRefuses() async throws {
        let backend = MediaBackendStub(ready: false)
        let coordinator = MediaUploadCoordinator(backend: backend)
        let pending = await coordinator.begin(declaration: .init(
            mime: "image/jpeg", size: 1,
            sha256: String(repeating: "b", count: 64), width: 1, height: 1
        ))
        await #expect(throws: MediaUploadError.self) {
            try await coordinator.upload(pending, bytes: Data([0]))
        }
    }
}

private actor MediaBackendStub: MediaUploadBackend {
    private let ready: Bool
    private(set) var createKeys: [String] = []
    private(set) var putIDs: [String] = []
    private(set) var completeIDs: [String] = []

    init(ready: Bool = true) { self.ready = ready }

    func createMediaUpload(
        declaration: MediaUploadDeclaration, idempotencyKey: String
    ) async throws -> CreatedMediaUpload {
        createKeys.append(idempotencyKey)
        return .init(mediaID: "media_1", state: "pending", expiresAt: "later", replayed: false)
    }

    func putMediaContent(mediaID: String, body: Data) async throws -> MediaUploadReceipt {
        putIDs.append(mediaID)
        return .init(mediaID: mediaID, state: "uploaded", mime: "image/jpeg", size: body.count)
    }

    func completeMediaUpload(mediaID: String) async throws -> CompletedMediaUpload {
        completeIDs.append(mediaID)
        return .init(
            mediaID: mediaID,
            state: ready ? "ready" : "uploading",
            outcome: ready ? "published" : "in_progress",
            retryAt: ready ? nil : "later",
            contentSHA256: ready ? String(repeating: "c", count: 64) : nil,
            mime: ready ? "image/jpeg" : nil,
            size: ready ? 3 : nil,
            declaredWidth: ready ? 1 : nil,
            declaredHeight: ready ? 3 : nil
        )
    }
}
