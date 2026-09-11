import Foundation
import Testing

@testable import PersonalAgentKit

@Suite("The CAP-003 media wire and recovery boundary")
struct MediaWireTests {
    @Test("lost PUT reply recovers through complete without a second PUT")
    func lostPutReply() async throws {
        let backend = MediaBackendStub(losePutReply: true)
        let coordinator = MediaUploadCoordinator(backend: backend)
        let initial = await coordinator.begin(declaration: .init(
            mime: "image/jpeg", size: 3, sha256: String(repeating: "a", count: 64),
            width: 1, height: 3))
        let durable = try await coordinator.create(initial)
        await #expect(throws: AgentClientError.self) {
            try await coordinator.put(durable, bytes: Data([1, 2, 3]))
        }
        let restored = try JSONDecoder().decode(PendingMediaUpload.self,
                                                from: JSONEncoder().encode(durable))
        let recovered = try await coordinator.put(restored, bytes: Data([1, 2, 3]))
        let completed = try await coordinator.complete(recovered)
        #expect(completed.outcome == "already_ready")
        #expect(completed.isReady)
        #expect(await backend.putIDs.count == 1)
    }

    @Test("Timeline preserves image references even when the text is empty")
    func timelineImageReference() {
        let id = "00000000-0000-4000-8000-000000000001"
        let event = TimelineEvent(eventID: "e", eventType: "user_message", operationID: nil,
                                  createdAt: "now", content: [
                                    "text": .string(""),
                                    "parts": .array([.object([
                                        "type": .string("image_ref"), "media_id": .string(id)
                                    ])])
                                  ])
        #expect(event.imageMediaIDs == [id])
    }
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
        #expect(await backend.completeIDs == ["media_1", "media_1"])
    }

    @Test("a restored PUT checkpoint never uploads the same sealed target twice")
    func restoredPutCheckpointSkipsSecondPut() async throws {
        let backend = MediaBackendStub()
        let coordinator = MediaUploadCoordinator(backend: backend)
        let declaration = MediaUploadDeclaration(
            mime: "image/jpeg", size: 3,
            sha256: String(repeating: "d", count: 64), width: 1, height: 3
        )
        let initial = await coordinator.begin(declaration: declaration)
        let created = try await coordinator.create(initial)
        let uploaded = try await coordinator.put(created, bytes: Data([1, 2, 3]))
        let restored = try JSONDecoder().decode(
            PendingMediaUpload.self, from: JSONEncoder().encode(uploaded)
        )
        let resumed = try await coordinator.put(restored, bytes: Data([1, 2, 3]))
        #expect(resumed == restored)
        #expect(await backend.putIDs == ["media_1"])
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
    private let losePutReply: Bool
    private var published = false
    private(set) var createKeys: [String] = []
    private(set) var putIDs: [String] = []
    private(set) var completeIDs: [String] = []

    init(ready: Bool = true, losePutReply: Bool = false) {
        self.ready = ready
        self.losePutReply = losePutReply
    }

    func createMediaUpload(
        declaration: MediaUploadDeclaration, idempotencyKey: String
    ) async throws -> CreatedMediaUpload {
        createKeys.append(idempotencyKey)
        return .init(mediaID: "media_1", state: "pending", expiresAt: "later", replayed: false)
    }

    func putMediaContent(mediaID: String, body: Data) async throws -> MediaUploadReceipt {
        putIDs.append(mediaID)
        if losePutReply { throw AgentClientError.transport("lost PUT response") }
        return .init(mediaID: mediaID, state: "uploaded", mime: "image/jpeg", size: body.count)
    }

    func completeMediaUpload(mediaID: String) async throws -> CompletedMediaUpload {
        completeIDs.append(mediaID)
        if ready && putIDs.isEmpty {
            return .init(mediaID: mediaID, state: "pending", outcome: "in_progress",
                         retryAt: nil, contentSHA256: nil, mime: nil, size: nil,
                         declaredWidth: nil, declaredHeight: nil)
        }
        let outcome = published ? "already_ready" : "published"
        if ready { published = true }
        return .init(
            mediaID: mediaID,
            state: ready ? "ready" : "uploading",
            outcome: ready ? outcome : "in_progress",
            retryAt: ready ? nil : "later",
            contentSHA256: ready ? String(repeating: "c", count: 64) : nil,
            mime: ready ? "image/jpeg" : nil,
            size: ready ? 3 : nil,
            declaredWidth: ready ? 1 : nil,
            declaredHeight: ready ? 3 : nil
        )
    }
}
