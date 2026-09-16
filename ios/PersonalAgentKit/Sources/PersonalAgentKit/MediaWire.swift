import Foundation

/// The closed, version-one chat-parts vocabulary.
///
/// Images never travel in the chat JSON as base64.  The only image value the
/// chat endpoint accepts is the id of a completed media upload, so an old
/// client cannot accidentally turn a photo into an unbounded request body.
public enum ChatInputPart: Sendable, Equatable {
    case text(String)
    case imageReference(mediaID: String)
}

extension ChatInputPart: Codable {
    private enum CodingKeys: String, CodingKey { case type, text, mediaID = "media_id" }
    private enum Kind: String, Codable { case text, imageRef = "image_ref" }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .text(let text):
            try container.encode(Kind.text, forKey: .type)
            try container.encode(text, forKey: .text)
        case .imageReference(let mediaID):
            try container.encode(Kind.imageRef, forKey: .type)
            try container.encode(mediaID, forKey: .mediaID)
        }
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        switch try container.decode(Kind.self, forKey: .type) {
        case .text:
            self = .text(try container.decode(String.self, forKey: .text))
        case .imageRef:
            self = .imageReference(mediaID: try container.decode(String.self, forKey: .mediaID))
        }
    }
}

/// Exactly one of the legacy text field and the structured parts field.
///
/// Keeping the legacy shape for text-only messages is a compatibility promise:
/// adding `parts: []` would alter the server fingerprint of an existing draft.
public enum ChatInput: Sendable, Equatable {
    case legacyText(String)
    case parts([ChatInputPart])

    public var textForDisplay: String {
        switch self {
        case .legacyText(let text): return text
        case .parts(let parts):
            return parts.compactMap {
                if case .text(let text) = $0 { return text }
                return nil
            }.joined()
        }
    }
}

/// The declaration is a claim about bytes the device already prepared.  It is
/// deliberately separate from the completion receipt, whose hash is the
/// server's measurement rather than an echo of these fields.
public struct MediaUploadDeclaration: Encodable, Sendable, Equatable {
    public let mime: String
    public let size: Int
    public let sha256: String
    public let width: Int
    public let height: Int

    public init(mime: String, size: Int, sha256: String, width: Int, height: Int) {
        self.mime = mime
        self.size = size
        self.sha256 = sha256
        self.width = width
        self.height = height
    }

    private enum CodingKeys: String, CodingKey {
        case purpose, mime, size, sha256, width, height
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode("chat_image", forKey: .purpose)
        try container.encode(mime, forKey: .mime)
        try container.encode(size, forKey: .size)
        try container.encode(sha256, forKey: .sha256)
        try container.encode(width, forKey: .width)
        try container.encode(height, forKey: .height)
    }
}

public struct CreatedMediaUpload: Decodable, Sendable, Equatable {
    public let mediaID: String
    public let state: String
    public let expiresAt: String
    public let replayed: Bool

    private enum CodingKeys: String, CodingKey {
        case mediaID = "media_id", state, expiresAt = "expires_at", replayed
    }
}

public struct MediaUploadReceipt: Decodable, Sendable, Equatable {
    public let mediaID: String
    public let state: String
    public let mime: String
    public let size: Int

    private enum CodingKeys: String, CodingKey { case mediaID = "media_id", state, mime, size }
}

public struct CompletedMediaUpload: Decodable, Sendable, Equatable {
    public let mediaID: String
    public let state: String
    public let outcome: String
    public let retryAt: String?
    /// The server-measured plaintext digest.  It must be present before this
    /// value becomes an image reference in a chat request.
    public let contentSHA256: String?
    public let mime: String?
    public let size: Int?
    public let declaredWidth: Int?
    public let declaredHeight: Int?

    private enum CodingKeys: String, CodingKey {
        case mediaID = "media_id", state, outcome, retryAt = "retry_at"
        case contentSHA256 = "content_sha256", mime, size
        case declaredWidth = "declared_width", declaredHeight = "declared_height"
    }

    public var isReady: Bool {
        ["published", "already_ready"].contains(outcome)
            && ["ready", "bound"].contains(state) && contentSHA256 != nil
    }
}

public enum MediaUploadError: Error, Equatable {
    case incompleteCompletion(CompletedMediaUpload)
}

/// The only network operations the upload state machine is allowed to make.
/// Authentication and token refresh stay in `DeviceSession`; the state machine
/// only owns the order and the client-minted idempotency key.
public protocol MediaUploadBackend: Sendable {
    func createMediaUpload(
        declaration: MediaUploadDeclaration, idempotencyKey: String
    ) async throws -> CreatedMediaUpload
    func putMediaContent(mediaID: String, body: Data) async throws -> MediaUploadReceipt
    func completeMediaUpload(mediaID: String) async throws -> CompletedMediaUpload
    func readMedia(mediaID: String) async throws -> Data
}

public extension MediaUploadBackend {
    func readMedia(mediaID: String) async throws -> Data {
        throw AgentClientError.malformedResponse
    }
}

/// A prepared upload whose key must survive a lost create response.  The app
/// persists this alongside its protected local-file reference *before* it calls
/// `create`; this package type intentionally carries no file URL or image bytes.
public struct PendingMediaUpload: Codable, Sendable, Equatable {
    public let idempotencyKey: String
    public let declaration: MediaUploadDeclarationRecord
    public var mediaID: String?
    /// A confirmed PUT checkpoint. False is ambiguous, not proof that no PUT
    /// landed: the coordinator must query complete before trying that target.
    public var contentUploaded: Bool

    public init(
        idempotencyKey: String,
        declaration: MediaUploadDeclaration,
        mediaID: String? = nil,
        contentUploaded: Bool = false
    ) {
        self.idempotencyKey = idempotencyKey
        self.declaration = MediaUploadDeclarationRecord(declaration)
        self.mediaID = mediaID
        self.contentUploaded = contentUploaded
    }
}

/// Codable copy used only for the durable prepared-upload record.
public struct MediaUploadDeclarationRecord: Codable, Sendable, Equatable {
    public let mime: String
    public let size: Int
    public let sha256: String
    public let width: Int
    public let height: Int

    init(_ declaration: MediaUploadDeclaration) {
        mime = declaration.mime
        size = declaration.size
        sha256 = declaration.sha256
        width = declaration.width
        height = declaration.height
    }

    var declaration: MediaUploadDeclaration {
        .init(mime: mime, size: size, sha256: sha256, width: width, height: height)
    }
}

/// The deterministic, resumable three-step upload protocol.  It never mints a
/// new key after preparation and never turns an in-progress completion into a
/// ready reference.  The UI may poll `complete` again with the recorded id;
/// only `ready` can flow into chat parts.
public actor MediaUploadCoordinator {
    private let backend: any MediaUploadBackend
    private let now: @Sendable () -> Date

    public init(
        backend: any MediaUploadBackend,
        now: @escaping @Sendable () -> Date = { Date() }
    ) {
        self.backend = backend
        self.now = now
    }

    public func begin(declaration: MediaUploadDeclaration) -> PendingMediaUpload {
        PendingMediaUpload(idempotencyKey: IdempotencyKey.mint(), declaration: declaration)
    }

    public func create(_ pending: PendingMediaUpload) async throws -> PendingMediaUpload {
        var pending = pending
        if pending.mediaID == nil {
            let created = try await backend.createMediaUpload(
                declaration: pending.declaration.declaration,
                idempotencyKey: pending.idempotencyKey
            )
            pending.mediaID = created.mediaID
        }
        return pending
    }

    public func put(_ pending: PendingMediaUpload, bytes: Data) async throws -> PendingMediaUpload {
        guard let mediaID = pending.mediaID else {
            return try await createThenPut(pending, bytes: bytes)
        }
        guard !pending.contentUploaded else { return pending }
        // A persisted target does not tell us whether an earlier PUT reached
        // the server. Ask its state before consuming the one-shot target.
        let status = try await backend.completeMediaUpload(mediaID: mediaID)
        if status.isReady {
            var recovered = pending
            recovered.contentUploaded = true
            return recovered
        }
        // An interrupted unsealed attempt is reclaimable only after the
        // advertised deadline. Server-side locking/CAS remains authoritative:
        // a fast client clock may attempt early, but cannot steal a live claim.
        let expiredClaim = status.state == "uploading"
            && status.retryAt.flatMap(Self.retryDeadline).map { $0 <= now() } == true
        guard status.outcome == "in_progress",
              status.state == "pending" || expiredClaim else {
            throw MediaUploadError.incompleteCompletion(status)
        }
        _ = try await backend.putMediaContent(mediaID: mediaID, body: bytes)
        var updated = pending
        updated.contentUploaded = true
        return updated
    }

    private static func retryDeadline(_ value: String) -> Date? {
        // Agent timestamps use fixed-width microseconds; the Calendar mirror
        // formatter accepts second-only timestamps and is not this contract.
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = formatter.date(from: value) { return date }
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: value)
    }

    private func createThenPut(
        _ pending: PendingMediaUpload, bytes: Data
    ) async throws -> PendingMediaUpload {
        // This convenience is only for an in-memory caller. A durable caller
        // must call `create`, persist its return value, then call `put`.
        let created = try await create(pending)
        return try await put(created, bytes: bytes)
    }

    public func upload(
        _ pending: PendingMediaUpload, bytes: Data
    ) async throws -> (pending: PendingMediaUpload, completed: CompletedMediaUpload) {
        let target = try await create(pending)
        let uploaded = try await put(target, bytes: bytes)
        let completed = try await complete(uploaded)
        return (uploaded, completed)
    }

    public func complete(_ pending: PendingMediaUpload) async throws -> CompletedMediaUpload {
        guard let mediaID = pending.mediaID else {
            // No target has ever been returned. The caller must replay `upload`
            // with the same declaration and key, never fabricate a media id.
            throw MediaUploadError.incompleteCompletion(
                .init(
                    mediaID: "", state: "pending", outcome: "in_progress", retryAt: nil,
                    contentSHA256: nil, mime: nil, size: nil,
                    declaredWidth: nil, declaredHeight: nil
                )
            )
        }
        guard pending.contentUploaded else {
            throw MediaUploadError.incompleteCompletion(
                .init(
                    mediaID: mediaID, state: "pending", outcome: "in_progress", retryAt: nil,
                    contentSHA256: nil, mime: nil, size: nil,
                    declaredWidth: nil, declaredHeight: nil
                )
            )
        }
        let completed = try await backend.completeMediaUpload(mediaID: mediaID)
        guard completed.isReady else { throw MediaUploadError.incompleteCompletion(completed) }
        return completed
    }
}
