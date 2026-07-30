import Foundation

/// The chat state machine of `DEV-030`, kept out of SwiftUI so it can be tested
/// headlessly with `swift test`.
///
/// Three of the task's constraints are structure here rather than caller
/// discipline:
///
/// - **the client never creates or manages a Session.** There is no code path
///   that invents a `conversation_id`: it arrives from `/v1/capabilities`, and
///   `send` refuses until it has. If the server answers a page with a different
///   canonical id (an alias it resolved), that answer is adopted, never argued
///   with.
/// - **a reconnect resumes the same operation.** The idempotency key is minted
///   once, persisted to the Keychain *before* the request leaves, and reused for
///   every retry of that message. A lost reply is therefore ambiguous exactly once:
///   `resume()` re-presents the same key, and the server either replays the
///   operation it already created or creates the first one. Nothing here ever mints
///   a second key for the same intent, so a flaky network cannot produce two
///   ledger rows.
/// - **success is never parsed out of prose.** This type only ever reads
///   `OperationReceipt.outcome`, which is derived from `state` and `record_id`.
public actor ChatTimeline {
    /// One unfinished message. Persisted so that an app restart, a crash between
    /// the write and the request, or a dropped reply all resume the *same* server
    /// operation instead of starting a second one.
    public struct PendingSend: Codable, Sendable, Equatable {
        public let idempotencyKey: String
        public let conversationID: String
        public let text: String
        public let clarificationOf: String?
        /// Known only once the server has answered at least once.
        public var operationID: String?
    }

    public enum ChatError: Error, Equatable {
        /// No Timeline id yet. `/v1/capabilities` has to answer first.
        case timelineUnknown
        /// A previous message is still unresolved. Resume or cancel it; starting a
        /// new one would leave an operation nobody is watching.
        case unresolvedSend(PendingSend)
        /// The local pending record could not be decoded. Reported rather than
        /// discarded: dropping it would hide an operation that may hold a write.
        case pendingSendMalformed
    }

    private let backend: any ChatBackend
    private let store: CredentialStore
    private let sleep: @Sendable (Duration) async throws -> Void
    /// The poll schedule. Bounded on purpose: a client that polls forever hides a
    /// stuck operation behind a spinner and drains the battery doing it.
    private let pollDelays: [Duration]

    private var conversationID: String?
    private var seenEventIDs: Set<String> = []
    /// Oldest-to-newest, in the order the server returned. Never re-sorted from
    /// `created_at`: the server's sequence is the ordering authority.
    public private(set) var events: [TimelineEvent] = []
    /// Cursor for scrolling up. Separate from `newestCursor` because a page of old
    /// events also carries a `newer_cursor` — anchored at *its* newest row, which
    /// is far behind the live edge. Storing that as the sync cursor would re-deliver
    /// the whole history on the next incremental sync.
    public private(set) var oldestCursor: String?
    public private(set) var newestCursor: String?
    public private(set) var hasOlder = false

    public init(
        backend: any ChatBackend,
        store: CredentialStore,
        pollDelays: [Duration] = [
            .milliseconds(400), .seconds(1), .seconds(2), .seconds(3), .seconds(5),
            .seconds(5), .seconds(5), .seconds(5),
        ],
        sleep: @escaping @Sendable (Duration) async throws -> Void = {
            try await Task.sleep(for: $0)
        }
    ) {
        self.backend = backend
        self.store = store
        self.pollDelays = pollDelays
        self.sleep = sleep
    }

    // --- the Timeline the server owns ----------------------------------------

    /// Adopt the canonical Timeline id from `/v1/capabilities`.
    public func bind(conversationID: String) {
        if self.conversationID != conversationID {
            // A different Timeline means the loaded page belongs to something
            // else. Keeping it on screen under a new id would be a history that
            // lies about where it came from.
            events = []
            seenEventIDs = []
            oldestCursor = nil
            newestCursor = nil
            hasOlder = false
        }
        self.conversationID = conversationID
    }

    public var boundConversationID: String? { conversationID }

    // --- history --------------------------------------------------------------

    /// The newest page, which is what a chat opens on.
    public func loadLatest() async throws {
        let id = try requireConversation()
        let page = try await backend.timelinePage(
            conversationID: id, cursor: nil, direction: .older, limit: nil
        )
        adoptCanonical(page.conversationID)
        events = []
        seenEventIDs = []
        append(page.events)
        oldestCursor = page.olderCursor
        newestCursor = page.newerCursor
        hasOlder = page.hasOlder
    }

    /// Scroll up one page. Returns false when there is nothing older to load.
    @discardableResult
    public func loadOlder() async throws -> Bool {
        let id = try requireConversation()
        guard let cursor = oldestCursor else { return false }
        let page: TimelinePageResponse
        do {
            page = try await backend.timelinePage(
                conversationID: id, cursor: cursor, direction: .older, limit: nil
            )
        } catch let error as AgentClientError where error.errorCode == "INVALID_CURSOR" {
            // A cursor the server will not accept cannot be retried. Reload the
            // newest page once; never loop on the same refusal.
            try await loadLatest()
            return false
        }
        adoptCanonical(page.conversationID)
        prepend(page.events)
        oldestCursor = page.olderCursor
        hasOlder = page.hasOlder
        // `newestCursor` is deliberately untouched: this page's `newer_cursor`
        // anchors at the top of an old page, not at the live edge.
        return !page.events.isEmpty
    }

    /// Incremental sync from the live edge. Before a cursor exists, reload the
    /// bounded newest page: an empty Timeline has no row from which the server
    /// could mint a cursor, and otherwise it could never discover its first event.
    public func syncNewer() async throws {
        let id = try requireConversation()
        guard let cursor = newestCursor else {
            // An empty Timeline has no row from which the server can mint a
            // `newer` cursor. Reloading the bounded newest page is the only way
            // it can ever discover its first message.
            try await loadLatest()
            return
        }
        let page: TimelinePageResponse
        do {
            page = try await backend.timelinePage(
                conversationID: id, cursor: cursor, direction: .newer, limit: nil
            )
        } catch let error as AgentClientError where error.errorCode == "INVALID_CURSOR" {
            try await loadLatest()
            return
        }
        adoptCanonical(page.conversationID)
        append(page.events)
        // An empty page carries no cursor. Clearing ours on a quiet poll would end
        // incremental sync permanently and look exactly like a Timeline that
        // stopped receiving events.
        if let cursor = page.newerCursor {
            newestCursor = cursor
        }
    }

    // --- sending --------------------------------------------------------------

    /// Send one message and wait, bounded, for it to settle.
    ///
    /// The receipt returned is the last one the server gave; `.running` means it is
    /// still working and the pending record is still on disk for `resume()`.
    public func send(
        text: String, clarificationOf: String? = nil
    ) async throws -> OperationReceipt {
        let id = try requireConversation()
        if let pending = try loadPending() {
            throw ChatError.unresolvedSend(pending)
        }
        var pending = PendingSend(
            idempotencyKey: UUID().uuidString,
            conversationID: id,
            text: text,
            clarificationOf: clarificationOf,
            operationID: nil
        )
        // Persist before the request leaves. A crash in between leaves a key with
        // no operation, which `resume()` re-presents; the server then either
        // replays or creates exactly one operation for it.
        try savePending(pending)
        let receipt: OperationReceipt
        do {
            receipt = try await backend.sendChatMessage(
                conversationID: pending.conversationID,
                text: pending.text,
                clarificationOf: pending.clarificationOf,
                idempotencyKey: pending.idempotencyKey
            )
        } catch let error as AgentClientError where Self.provesNotAnchored(error) {
            // The server refused before it could create an operation, so there is
            // nothing to resume and keeping the record would block every later
            // message behind a request that can never land.
            try? clearPending()
            throw error
        }
        pending.operationID = receipt.operationID
        try savePending(pending)
        return try await settle(receipt, pending: pending)
    }

    /// Finish whatever was left unresolved, if anything.
    ///
    /// This is the reconnect path, and it is the reason a lost reply is not a lost
    /// write: the same key goes back to the server, which owns the answer.
    public func resume() async throws -> OperationReceipt? {
        guard let pending = try loadPending() else { return nil }
        if let operationID = pending.operationID {
            let receipt = try await backend.operation(operationID: operationID)
            return try await settle(receipt, pending: pending)
        }
        let receipt: OperationReceipt
        do {
            receipt = try await backend.sendChatMessage(
                conversationID: pending.conversationID,
                text: pending.text,
                clarificationOf: pending.clarificationOf,
                idempotencyKey: pending.idempotencyKey
            )
        } catch let error as AgentClientError where Self.provesNotAnchored(error) {
            try? clearPending()
            throw error
        }
        var updated = pending
        updated.operationID = receipt.operationID
        try savePending(updated)
        return try await settle(receipt, pending: updated)
    }

    /// Ask the server to cancel. The reply is the operation's real state: past a
    /// possible source submit this only records the request, and the outcome still
    /// comes from the server.
    public func cancel(operationID: String) async throws -> OperationReceipt {
        let receipt = try await backend.cancelOperation(operationID: operationID)
        if receipt.outcome.releasesPendingSlot, let pending = try loadPending(),
           pending.operationID == receipt.operationID {
            try clearPending()
        }
        return receipt
    }

    public func pendingSend() throws -> PendingSend? { try loadPending() }

    /// Forget an unresolved message *without* asking the server. Only for a
    /// deliberate user action: it abandons a durable operation, so the caller has
    /// to have shown the operation id first.
    public func discardPending() throws {
        try clearPending()
    }

    // --- polling --------------------------------------------------------------

    private func settle(
        _ first: OperationReceipt, pending: PendingSend
    ) async throws -> OperationReceipt {
        var receipt = first
        var attempt = 0
        while !receipt.outcome.isSettled && attempt < pollDelays.count {
            try await sleep(pollDelays[attempt])
            attempt += 1
            receipt = try await backend.operation(operationID: receipt.operationID)
        }
        if receipt.outcome.releasesPendingSlot {
            // Polling may stop without releasing the slot. Unknown and
            // needs-manual-review outcomes stay on disk because the client
            // giving up on watching is not proof that another key is safe.
            if let stored = try loadPending(), stored.operationID == receipt.operationID {
                try clearPending()
            }
        }
        return receipt
    }

    // --- helpers --------------------------------------------------------------

    /// Errors that prove this intent was never anchored: authentication and
    /// body validation both run ahead of the atomic
    /// `api_request + operation` creation, and a refusal there rolls back whole.
    /// A transport failure or a 5xx proves nothing and keeps the pending record.
    private static func provesNotAnchored(_ error: AgentClientError) -> Bool {
        switch error {
        case .badRequest, .forbidden, .unauthenticated, .deviceRejected,
             .idempotencyConflict, .notFound:
            return true
        case .transport, .serverError, .malformedResponse, .invalidBaseURL,
             .enrollmentRejected, .cursorRequired:
            return false
        }
    }

    private func requireConversation() throws -> String {
        guard let conversationID else { throw ChatError.timelineUnknown }
        return conversationID
    }

    private func adoptCanonical(_ serverConversationID: String) {
        // The server may have resolved an alias to the canonical Timeline. Its
        // answer is the id all later pages and messages must use.
        conversationID = serverConversationID
    }

    private func append(_ incoming: [TimelineEvent]) {
        for event in incoming where !seenEventIDs.contains(event.eventID) {
            seenEventIDs.insert(event.eventID)
            events.append(event)
        }
    }

    private func prepend(_ incoming: [TimelineEvent]) {
        let fresh = incoming.filter { !seenEventIDs.contains($0.eventID) }
        for event in fresh { seenEventIDs.insert(event.eventID) }
        events.insert(contentsOf: fresh, at: 0)
    }

    private func loadPending() throws -> PendingSend? {
        guard let data = try store.read(CredentialKey.pendingChatSend) else {
            return nil
        }
        do {
            return try JSONDecoder().decode(PendingSend.self, from: data)
        } catch {
            throw ChatError.pendingSendMalformed
        }
    }

    private func savePending(_ pending: PendingSend) throws {
        try store.write(
            CredentialKey.pendingChatSend, value: try JSONEncoder().encode(pending)
        )
    }

    private func clearPending() throws {
        try store.delete(CredentialKey.pendingChatSend)
    }
}

/// What `ChatTimeline` needs from the network.
///
/// A protocol rather than a concrete client so the token policy stays in
/// `DeviceSession` (one refresh, never a loop) and the chat logic does not learn
/// about tokens at all. The tests conform the **real** `DeviceSession` to it and
/// stub only the HTTP layer, so the real client, the real decoding and the real
/// refresh policy are all still in the loop.
public protocol ChatBackend: Sendable {
    func sendChatMessage(
        conversationID: String,
        text: String,
        clarificationOf: String?,
        idempotencyKey: String
    ) async throws -> OperationReceipt

    func operation(operationID: String) async throws -> OperationReceipt
    func cancelOperation(operationID: String) async throws -> OperationReceipt

    func timelinePage(
        conversationID: String,
        cursor: String?,
        direction: TimelineDirection,
        limit: Int?
    ) async throws -> TimelinePageResponse
}
