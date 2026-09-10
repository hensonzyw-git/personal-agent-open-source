import Foundation

/// Where an in-flight operation has provably reached, as the progress trail
/// reports it.
///
/// Every case is read off the server's own operation projection — the same
/// structured fields the receipt is built from — and never off model text. The
/// trail names the *stage*, and `dispatching` names the tool the server
/// recorded for it; nothing here can ever carry a model's reasoning, because
/// the server never stores it in the operation row.
public enum OperationStage: Sendable, Equatable {
    /// The server has anchored the request durably.
    case accepted
    /// The model turn is being interpreted.
    case interpreting
    /// A tool was selected and dispatched; this is the tool's registered name.
    case dispatching(tool: String?)
    /// The governed write may already have been submitted to the fact source.
    case sourceInProgress
    /// The outcome is being verified against the fact source.
    case verifying

    public static func == (lhs: OperationStage, rhs: OperationStage) -> Bool {
        switch (lhs, rhs) {
        case (.accepted, .accepted),
             (.interpreting, .interpreting),
             (.sourceInProgress, .sourceInProgress),
             (.verifying, .verifying):
            return true
        case (.dispatching(let a), .dispatching(let b)):
            return a == b
        default:
            return false
        }
    }
}

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
        /// Nil preserves decoding of pending sends written by older app builds.
        public let startNewSession: Bool?
        /// Every operation this message produced, in plan order — item 0 first.
        /// Known only as the server answers: the anchor merge names the
        /// message's own operation, and each device report names the plan item
        /// it settled.
        ///
        /// A list rather than one id because one message may be several
        /// operations (design §4.1/§4.2: 每项独立持有 operationID), and because
        /// the slot's question is about all of them. "This message is finished"
        /// is the claim the slot exists to make safe, and the message's own item
        /// settling says nothing about a sibling whose write may or may not
        /// exist — releasing on item 0 alone is how the user comes to ask for an
        /// event that a parked sibling already created.
        public var operationIDs: [String]
        /// The message's own operation: item 0 of its plan, or the operation
        /// itself when the plan is one item. What every path that speaks about
        /// *this message* means — the card the user is watching.
        public var operationID: String? { operationIDs.first }
        /// The operations whose outcome released the slot. A release is earned
        /// per operation, and the slot clears only once the whole set has one.
        public var releasedOperationIDs: [String]
        /// The device actions this device has already executed or refused for
        /// the pending operation, persisted *before* the executor runs (review
        /// R6). A poll, a resume or a replay can hand the same action over
        /// again — the server's projection carries it while the operation is
        /// parked — and executing twice is how a duplicate event gets born.
        /// The marker makes the execution exactly-once across restarts; the
        /// timeout sweep, not a re-execution, owns what the report's silence
        /// means.
        ///
        /// A list rather than one id, because a single reply may hand over
        /// several actions and each is its own operation with its own write
        /// (design §4.2: 每项独立持有 deliveredActionID). The set is per
        /// message, which is what the invariants need: a sibling's action is
        /// only ever delivered on the message's own projection, so the marker
        /// that must survive a restart is the message's.
        public var deliveredActionIDs: [String]

        public init(
            idempotencyKey: String,
            conversationID: String,
            text: String,
            clarificationOf: String?,
            startNewSession: Bool? = nil,
            operationID: String? = nil,
            operationIDs: [String] = [],
            deliveredActionIDs: [String] = [],
            releasedOperationIDs: [String] = []
        ) {
            self.idempotencyKey = idempotencyKey
            self.conversationID = conversationID
            self.text = text
            self.clarificationOf = clarificationOf
            self.startNewSession = startNewSession
            // A caller that knows the message's own operation (the anchor merge,
            // and every test written before plans were tracked) still says one
            // id; the list is that id plus whatever reports add later.
            self.operationIDs = operationIDs.isEmpty
                ? (operationID.map { [$0] } ?? []) : operationIDs
            self.deliveredActionIDs = deliveredActionIDs
            self.releasedOperationIDs = releasedOperationIDs
        }

        private enum CodingKeys: String, CodingKey {
            case idempotencyKey, conversationID, text, clarificationOf
            case startNewSession, operationID, operationIDs, deliveredActionIDs
            case releasedOperationIDs
            /// The one-action marker builds before design §4.2 wrote. Read as
            /// a fact about what already ran; also written, so that a build
            /// rolled back to one of those does not read an empty marker and
            /// execute an action this slot already claimed.
            case deliveredActionID
        }

        public init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            idempotencyKey = try container.decode(String.self, forKey: .idempotencyKey)
            conversationID = try container.decode(String.self, forKey: .conversationID)
            text = try container.decode(String.self, forKey: .text)
            clarificationOf = try container.decodeIfPresent(
                String.self, forKey: .clarificationOf
            )
            startNewSession = try container.decodeIfPresent(
                Bool.self, forKey: .startNewSession
            )
            var tracked =
                try container.decodeIfPresent([String].self, forKey: .operationIDs) ?? []
            // The singular field is what builds before §4.2 wrote, and it is
            // also what this build keeps writing for them (see `encode`); a
            // slot written by either shape reads back as one list.
            if let legacy = try container.decodeIfPresent(
                String.self, forKey: .operationID
            ), !tracked.contains(legacy) {
                tracked.append(legacy)
            }
            operationIDs = tracked
            releasedOperationIDs = try container.decodeIfPresent(
                [String].self, forKey: .releasedOperationIDs
            ) ?? []
            var claimed =
                try container.decodeIfPresent([String].self, forKey: .deliveredActionIDs)
                ?? []
            // An upgraded build must not forget an action the build it is
            // replacing already ran: losing that marker is how a restart
            // re-executes a write the server may have settled long ago.
            if let legacy = try container.decodeIfPresent(
                String.self, forKey: .deliveredActionID
            ), !claimed.contains(legacy) {
                claimed.append(legacy)
            }
            deliveredActionIDs = claimed
        }

        public func encode(to encoder: Encoder) throws {
            var container = encoder.container(keyedBy: CodingKeys.self)
            try container.encode(idempotencyKey, forKey: .idempotencyKey)
            try container.encode(conversationID, forKey: .conversationID)
            try container.encode(text, forKey: .text)
            try container.encodeIfPresent(clarificationOf, forKey: .clarificationOf)
            try container.encodeIfPresent(startNewSession, forKey: .startNewSession)
            try container.encode(operationIDs, forKey: .operationIDs)
            try container.encode(releasedOperationIDs, forKey: .releasedOperationIDs)
            // The message's own operation, written under the name a build that
            // predates the list reads. Same reasoning as `deliveredActionID`
            // below: the older build then tracks the message's own item, which
            // is the only one it knows how to speak about. It cannot see the
            // siblings, which is exactly the gap this batch closes.
            try container.encodeIfPresent(operationID, forKey: .operationID)
            try container.encode(deliveredActionIDs, forKey: .deliveredActionIDs)
            // The first claim is the message's own action (design §4.1: item 0
            // is the message's operation, delivered first), which is the one a
            // rolled-back single-action build could be handed a second time.
            // Writing it can only make that build *skip*, never re-execute.
            try container.encodeIfPresent(
                deliveredActionIDs.first, forKey: .deliveredActionID
            )
        }
    }

    /// `DEV-031`. One duplicate decision whose reply never arrived. Persisted
    /// for the same reason as `PendingSend`: the idempotency key inside is what
    /// makes a retry replay the server's recorded outcome instead of starting a
    /// second decision for the same check.
    public struct PendingDuplicateDecision: Codable, Sendable, Equatable {
        public let checkID: String
        public let decision: DuplicateDecision
        public let idempotencyKey: String
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
        /// `DEV-031`. A decision for this check may already have reached the
        /// server, so the *other* choice is locked: sending it under a fresh key
        /// would be refused there, and reusing the stored key is a `409`. The
        /// pending record is returned so the caller can say exactly which
        /// decision is in flight.
        case lockedDuplicateDecision(PendingDuplicateDecision)
        /// The local pending-decision records could not be decoded. Reported
        /// rather than discarded, for the same reason as `pendingSendMalformed`.
        case pendingDecisionsMalformed
        /// The local override-claim record could not be decoded. Reported
        /// rather than discarded or reset: the entries in it are the only
        /// evidence that an override's write already ran, and starting from an
        /// empty set would execute it a second time.
        case overrideClaimsMalformed
    }

    /// Which exactly-once marker owns the claim on a delivered action.
    ///
    /// Two shapes, because the second has no slot to live in. A chat send parks
    /// a `PendingSend` whose `operationID` names the operation the reply belongs
    /// to, and the marker rides inside it — so a released or replaced slot
    /// refuses to run anything, which is one of the ways a duplicate is
    /// prevented. A 「仍要创建」 override has no slot at all: it is a button on a
    /// card, not a message, and the operation it settles is derived server-side
    /// from the original. Its marker therefore stands alone, keyed by action id
    /// (the action id *is* the operation's idempotency key), and — unlike the
    /// message marker — it has to outlive the report, because the tap that must
    /// be refused is the one that arrives while the first report is in flight.
    private enum DeliveryClaim {
        case message(PendingSend)
        case override
    }

    private let backend: any ChatBackend
    private let store: CredentialStore
    /// The device-executed action runner. Nil in builds without one — in which
    /// case a handed action is still *reported* (as `failed`), because the
    /// server's operation parks at `source_in_progress` until it hears
    /// something; it never times out just because this build chose not to run.
    private let deviceActionExecutor: DeviceActionExecuting?
    private let sleep: @Sendable (Duration) async throws -> Void
    /// The poll schedule. Bounded on purpose: a client that polls forever hides a
    /// stuck operation behind a spinner and drains the battery doing it.
    private let pollDelays: [Duration]
    /// The progress trail's sink, set by the UI. Stage observations are
    /// delivered in arrival order; the sink decides what to show.
    private var progressSink: (@Sendable (OperationStage) async -> Void)?
    /// The calendar-mirror sink, set by the app, which owns the mirror engine.
    /// Fired once per delivered action whose report **landed** — design §9.1's
    /// second `.forced` trigger.
    ///
    /// Not redundant with an `EKEventStoreChanged` observer: one reply can
    /// carry several actions, and N reports arriving as N triggers is exactly
    /// the shape the engine collapses into at most two passes. It is also the
    /// signal the server records as the write's testimony, so it holds even
    /// when EventKit's own notification is coalesced away.
    ///
    /// A report whose reply was lost (the executor returns nil) fires nothing
    /// here — the device knows it wrote, but this seam is "the report landed",
    /// and the EventKit observer is what covers that case.
    private var calendarWriteSink: (@Sendable () async -> Void)?

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
        deviceActionExecutor: DeviceActionExecuting? = nil,
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
        self.deviceActionExecutor = deviceActionExecutor
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

    // --- the progress trail ----------------------------------------------------

    /// Observe where an in-flight operation has provably reached.
    ///
    /// The sink receives structured stages only. It never receives model text,
    /// because the operation projection has none to give — this method exists
    /// so the UI can show an execution pipeline instead of a bare spinner.
    public func setProgressSink(
        _ sink: (@Sendable (OperationStage) async -> Void)?
    ) {
        progressSink = sink
    }

    /// Install the calendar-mirror sink (design §9.1). The app sets this to
    /// `CalendarMirrorSyncEngine.noteCalendarChanged`, which records the change
    /// and runs the forced pass.
    public func setCalendarWriteSink(_ sink: (@Sendable () async -> Void)?) {
        calendarWriteSink = sink
    }

    /// Project one receipt's state onto the trail. Terminal and parked states
    /// say nothing: the receipt itself is about to tell the user the outcome.
    private static func stage(of receipt: OperationReceipt) -> OperationStage? {
        switch receipt.state {
        case .accepted: return .accepted
        case .interpreting: return .interpreting
        case .dispatching: return .dispatching(tool: receipt.tool)
        case .sourceInProgress: return .sourceInProgress
        case .verifying: return .verifying
        case .waitingForClarification, .waitingForDuplicateDecision,
             .succeeded, .failedSafe, .needsManualReview, .cancelledPreSubmit,
             .unrecognised:
            return nil
        }
    }

    private func report(_ stage: OperationStage) async {
        guard let sink = progressSink else { return }
        await sink(stage)
    }

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
        text: String,
        clarificationOf: String? = nil,
        startNewSession: Bool = false
    ) async throws -> OperationReceipt {
        let id = try requireConversation()
        if let pending = try loadPending() {
            throw ChatError.unresolvedSend(pending)
        }
        let pending = PendingSend(
            idempotencyKey: IdempotencyKey.mint(),
            conversationID: id,
            text: text,
            clarificationOf: clarificationOf,
            startNewSession: startNewSession ? true : nil,
            operationID: nil
        )
        // Persist before the request leaves. A crash in between leaves a key with
        // no operation, which `resume()` re-presents; the server then either
        // replays or creates exactly one operation for it.
        try savePending(pending)
        // The trail runs beside the POST, not after it: the whole point is the
        // window where the POST is still waiting and the server is already
        // interpreting and dispatching. It ends the moment the POST answers.
        let trail = startTrailRunner(key: pending.idempotencyKey)
        defer { trail.cancel() }
        let receipt: OperationReceipt
        do {
            receipt = try await backend.sendChatMessage(
                conversationID: pending.conversationID,
                text: pending.text,
                clarificationOf: pending.clarificationOf,
                startNewSession: pending.startNewSession == true,
                idempotencyKey: pending.idempotencyKey
            )
        } catch let error as AgentClientError where Self.provesNotAnchored(error) {
            // The server refused before it could create an operation, so there is
            // nothing to resume and keeping the record would block every later
            // message behind a request that can never land. The refusal may
            // land long after the user discarded this message (fifth review
            // I1): the release verifies the slot still belongs to it before
            // clearing, so a replaced message's pending survives.
            try? releaseSlotIfStillOwned(
                key: pending.idempotencyKey, operationID: nil
            )
            throw error
        }
        // The anchor merge (second review F2 / third review G1): the slot is
        // re-read and only the operation id is added, never the in-memory
        // copy written back whole, and the merge verifies the slot still
        // carries this send's key. A `send` starts from an empty slot it just
        // wrote, so the shape cannot arise here, but the rule is one rule —
        // the slot's authority is the disk, and its owner is the key.
        guard let anchored = try mergeAnchoredOperation(
            receipt.operationID, forKey: pending.idempotencyKey
        ) else {
            // Unreachable for a send that owns the slot, but refusing is the
            // safe answer: nothing is executed on a slot this call no longer
            // owns.
            return receipt
        }
        // The device-action hand-off runs here, before settling: the reply is
        // the only time the action exists on the wire, and the parked
        // operation cannot settle until this device reports what it did.
        // Executing *is* the settle step for this shape, so the returned
        // receipt replaces the polling loop's input.
        return try await runDeviceActionIfAny(receipt, pending: anchored)
    }

    /// Finish whatever was left unresolved, if anything.
    ///
    /// This is the reconnect path, and it is the reason a lost reply is not a lost
    /// write: the same key goes back to the server, which owns the answer.
    public func resume() async throws -> OperationReceipt? {
        guard let pending = try loadPending() else { return nil }
        if let operationID = pending.operationID {
            let receipt = try await backend.operation(operationID: operationID)
            // The by-id read goes through the same gate as a fresh reply: the
            // parked projection hands the action over on any read (review
            // R6), so a restart re-arms the hand-off rather than bypassing it.
            return try await runDeviceActionIfAny(receipt, pending: pending)
        }
        let receipt: OperationReceipt
        do {
            receipt = try await backend.sendChatMessage(
                conversationID: pending.conversationID,
                text: pending.text,
                clarificationOf: pending.clarificationOf,
                startNewSession: pending.startNewSession == true,
                idempotencyKey: pending.idempotencyKey
            )
        } catch let error as AgentClientError where Self.provesNotAnchored(error) {
            // Same rule as the send path (fifth review I1): a refusal landing
            // after this message was discarded and replaced must not delete
            // the replacement's slot.
            try? releaseSlotIfStillOwned(
                key: pending.idempotencyKey, operationID: nil
            )
            throw error
        }
        // Second review F2 / third review G1: the anchor merge re-reads the
        // stored slot rather than writing the in-memory copy back, and it
        // verifies the slot still belongs to **this message** — the idempotency
        // key the request was sent under. A reply that lands after the user
        // discarded this message and sent another must not anchor into the
        // new message's slot and must not execute this message's action: the
        // slot's owner is the key, and `operationID == nil` alone proves
        // nothing about who the slot now belongs to.
        guard let updated = try mergeAnchoredOperation(
            receipt.operationID, forKey: pending.idempotencyKey
        ) else {
            // The slot was released — or taken over by a different message —
            // while this request was in flight. Nothing is ours to run; the
            // caller receives the server's answer as a plain read.
            return receipt
        }
        // This branch anchored the operation *now*, so the reply really can
        // hand this device an action. The by-id branch above does not lose the
        // shape either: the parked projection hands the action over on any
        // read (review R6), and `settle` handles a delivered action inside its
        // poll loop through the same exactly-once gate.
        return try await runDeviceActionIfAny(receipt, pending: updated)
    }

    /// Record a just-learned operation id onto the durable slot, **merging**
    /// with whatever the slot now holds instead of overwriting it.
    ///
    /// Returns the merged record, or `nil` when the slot no longer exists,
    /// was released, or was taken over by a *different* message (third review
    /// G1: the merge carries the requesting message's idempotency key and
    /// verifies it against the stored record — a late reply whose message was
    /// discarded and replaced mid-flight must not anchor into the new
    /// message's slot or execute the old message's action). The only fields
    /// this call owns are the tracked operation ids; every other field — above
    /// all `deliveredActionIDs` — survives from disk.
    ///
    /// The anchor is the message's own operation, so it goes to the head of
    /// the list; a mismatch against a head already on disk means a different
    /// message now owns this key. It never *shortens* the list: a sibling
    /// recorded by an earlier report is a fact about this message, and the
    /// anchor arriving late must not erase it.
    private func mergeAnchoredOperation(
        _ operationID: String, forKey idempotencyKey: String
    ) throws -> PendingSend? {
        guard var stored = try loadPending() else { return nil }
        guard stored.idempotencyKey == idempotencyKey else { return nil }
        guard stored.operationID == nil || stored.operationID == operationID else {
            return nil
        }
        if stored.operationIDs.first != operationID {
            stored.operationIDs.insert(operationID, at: 0)
        }
        try savePending(stored)
        return stored
    }

    /// Ask the server to cancel. The reply is the operation's real state: past a
    /// possible source submit this only records the request, and the outcome still
    /// comes from the server.
    public func cancel(operationID: String) async throws -> OperationReceipt {
        let receipt = try await backend.cancelOperation(operationID: operationID)
        if receipt.outcome.releasesPendingSlot {
            // The cancel reply can land after the slot was discarded and
            // replaced (fourth review H1): release only what this call owns.
            // Membership in the message's operations, not headship — a plan's
            // later item is cancellable from its own card.
            if let pending = try loadPending(),
               pending.operationIDs.contains(receipt.operationID) {
                try releaseSlotIfStillOwned(
                    key: pending.idempotencyKey, operationID: receipt.operationID
                )
            }
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

    // --- duplicate decisions (`DEV-031`) --------------------------------------
    //
    // One durable slot per parked duplicate check, holding the idempotency key
    // the decision was first sent under. The slot exists for exactly one
    // ambiguity: the request may have landed while the reply was lost. The
    // server's own rules do the rest — the same key and decision replay the
    // recorded outcome, the same key under the other decision is a `409`, and a
    // fresh key for a resolved check is refused — so a retry can never start a
    // second decision and a crash can never lose which choice was made.

    /// Resolve a parked duplicate as the user chose.
    ///
    /// The key is minted once and persisted **before** the request leaves, for
    /// the same reason as a chat send: a lost reply then replays rather than
    /// risks a second decision. While a decision for this check is unconfirmed,
    /// the other choice is refused locally with `lockedDuplicateDecision` —
    /// changing one's mind is only safe once the server has answered.
    ///
    /// For `writeAnyway` the reply is the new override operation; it is polled
    /// bounded so the caller sees the write's real outcome, not the acceptance.
    public func decide(
        checkID: String, decision: DuplicateDecision
    ) async throws -> OperationReceipt {
        if let existing = try loadDecisions().first(where: { $0.checkID == checkID }) {
            guard existing.decision == decision else {
                throw ChatError.lockedDuplicateDecision(existing)
            }
            return try await sendDecision(existing)
        }
        let record = PendingDuplicateDecision(
            checkID: checkID,
            decision: decision,
            idempotencyKey: IdempotencyKey.mint()
        )
        try saveDecisions(try loadDecisions() + [record])
        return try await sendDecision(record)
    }

    /// Re-present every unconfirmed decision. The reconnect path, and the reason
    /// an app restart between tap and reply cannot lose or duplicate a decision.
    @discardableResult
    public func resumeDecisions() async throws -> [OperationReceipt] {
        var receipts: [OperationReceipt] = []
        for record in try loadDecisions() {
            receipts.append(try await sendDecision(record))
        }
        return receipts
    }

    /// The decisions whose reply never arrived, for the UI to show as
    /// in-flight rather than offering the choice again.
    public func pendingDecisions() throws -> [PendingDuplicateDecision] {
        try loadDecisions()
    }

    /// Forget an unconfirmed decision locally *without* asking the server. Only
    /// for a deliberate user action, and only after the check id was shown: the
    /// decision may already be recorded there.
    public func discardDecision(checkID: String) throws {
        try removeDecision(checkID)
    }

    // --- the calendar 「仍要创建」 override (design §3.3) ------------------------

    /// Re-issue a calendar write this device reported as a duplicate, because
    /// the user pressed 「仍要创建」.
    ///
    /// The endpoint is a replayable server-side binding rather than a new
    /// conversation turn: it resumes the arguments the original turn was
    /// already authorised to make, derives one operation from the original
    /// (`uuid5(ns, "<operation_id>:calendar-override")`), and answers every tap
    /// with that same operation's projection. So the client sends no key and a
    /// double tap, two concurrent taps and a retry after a lost reply all
    /// converge there — which is the whole reason a press is not a turn.
    ///
    /// What the server cannot do is *write*: the event exists only once this
    /// device puts it in EventKit, so the reply hands the re-issued action back
    /// over the ordinary delivery door, and that action carries
    /// `skip_local_dedup` — the check that produced the refusal is switched off
    /// because the user answered it. That leaves the claim in `recordDelivery`
    /// as the only thing between a double tap and two events, and it is why the
    /// action goes through the same gate everything else does rather than
    /// straight to the executor.
    ///
    /// A refusal (`INVALID_ARGUMENT`: not a duplicate, not `succeeded`, nothing
    /// retained to resume) propagates: nothing was created, and a receipt
    /// saying otherwise is a lie the card cannot recover from.
    ///
    /// Like the finance override it mirrors, the wait afterwards is bounded and
    /// carries no slot — the decision is durable on the server, and the write's
    /// outcome is the server's to show rather than the client's to insist on.
    @discardableResult
    public func overrideDeviceAction(
        actionID: String
    ) async throws -> OperationReceipt {
        let first = try await backend.overrideDeviceAction(actionID: actionID)
        if let reported = try await runDeliveredActions(first, claim: .override) {
            return reported.outcome.isSettled
                ? reported
                : try await settleOverride(reported)
        }
        return try await settleOverride(first)
    }

    // --- the manual-review resolution (`DEV-040`) -----------------------------

    /// Record what the user found in the ledger for an operation that ended at
    /// `needs_manual_review`, and release the durable slot that operation was
    /// holding.
    ///
    /// The slot release is the point of this method, and it is a *stronger* exit
    /// than the one it replaces. `releasesPendingSlot` is false for
    /// `needs_manual_review` because the operation may still hold a write and
    /// nobody has checked; until now the only way out was `discardPending()`,
    /// which asks for no verification at all and leaves no trace. A resolution is
    /// the verification — durable, server-side, and refused if it later
    /// contradicts itself — so once one is recorded the slot has nothing left to
    /// protect. Both values release it: `confirmed_written` means the entry is in
    /// the ledger and the user should move on, `confirmed_not_written` means
    /// re-entering it is exactly the right next step.
    ///
    /// Nothing is released unless the server accepted. A thrown request leaves the
    /// slot standing, because a failed report is not a report.
    @discardableResult
    public func resolveManualReview(
        operationID: String, resolution: ManualResolution
    ) async throws -> ManualResolutionReceipt {
        let receipt = try await backend.resolveManualReview(
            operationID: operationID, resolution: resolution
        )
        // Bind to the *server's* operation id, not the argument: the slot must
        // only ever be released for the operation the server actually answered
        // about — and only when the slot still belongs to that message (the
        // same ownership rule as every other post-wait release; a resolution
        // landing after a discard-and-replace must not delete the
        // replacement's slot).
        //
        // Membership, not headship: the card the user resolved may be a plan's
        // *later* item, whose write is exactly as unknown as the message's own.
        // Comparing against the head alone is how a resolved sibling left the
        // message blocked forever with nothing left to resolve.
        if let pending = try loadPending(),
           pending.operationIDs.contains(receipt.operationID) {
            try releaseSlotIfStillOwned(
                key: pending.idempotencyKey, operationID: receipt.operationID
            )
        }
        return receipt
    }

    /// `G1`. Correct one recorded expense's 分类, and wait for the real outcome.
    ///
    /// The reply to the correction route may be a `202` — the server accepted it
    /// and the governed update is still running — so this polls to a settled
    /// receipt the same way a `write anyway` override does. Returning the
    /// acceptance would hand the card a `.running` outcome, and the card would
    /// have to say "分类未修改：服务端仍在处理" for a change that was about to
    /// succeed.
    ///
    /// No durable pending slot, unlike a chat send or a duplicate decision, and
    /// the key is minted here rather than by the caller. The correction is a
    /// compare-and-swap: a replay after a lost reply finds the row already at
    /// the target value and is answered `already_current` without a second
    /// write. That is a stronger guarantee than a slot, and it needs no local
    /// state to survive an app restart -- which is also why a fresh key per tap
    /// is safe here where it would not be for a create.
    ///
    /// Minting inside the Kit is not incidental: `IdempotencyKey.mint` is the
    /// one place this client makes a key, in the canonical lower-case spelling
    /// the server actually parses. Letting a screen supply its own is how the
    /// upper-case-UUID bug reached production once already.
    public func updateExpenseCategory(
        recordID: String,
        category: String,
        expectedCurrentCategory: String?
    ) async throws -> OperationReceipt {
        let receipt = try await backend.updateExpenseCategory(
            recordID: recordID,
            category: category,
            expectedCurrentCategory: expectedCurrentCategory,
            idempotencyKey: IdempotencyKey.mint()
        )
        guard !receipt.outcome.isSettled else { return receipt }
        return try await settleDecision(receipt)
    }

    /// Send one decision and clear its slot once the server's answer is known.
    private func sendDecision(
        _ record: PendingDuplicateDecision
    ) async throws -> OperationReceipt {
        let receipt: OperationReceipt
        do {
            receipt = try await backend.decideDuplicate(
                checkID: record.checkID,
                decision: record.decision,
                idempotencyKey: record.idempotencyKey
            )
        } catch let error as AgentClientError where Self.provesNotAnchored(error) {
            // Refused before the decision could be recorded (or a key conflict a
            // retry can never repair, or the check is already resolved). Keeping
            // the slot would lock this check behind a decision that cannot land.
            try? removeDecision(record.checkID)
            throw error
        }
        // A 2xx means the server recorded the decision; it is durable there and
        // the slot has nothing left to protect. For `writeAnyway` the receipt is
        // the new override operation, so wait bounded for the write's outcome.
        try removeDecision(record.checkID)
        if record.decision == .writeAnyway, !receipt.outcome.isSettled {
            return try await settleDecision(receipt)
        }
        return receipt
    }

    /// Bounded polling for the override operation. Unlike `settle` there is no
    /// slot to manage: the decision is already durable, and the write's outcome
    /// is the server's to show, not the client's to re-request.
    private func settleDecision(_ first: OperationReceipt) async throws -> OperationReceipt {
        var receipt = first
        var attempt = 0
        while !receipt.outcome.isSettled && attempt < pollDelays.count {
            try await sleep(pollDelays[attempt])
            attempt += 1
            receipt = try await backend.operation(operationID: receipt.operationID)
        }
        return receipt
    }

    /// Bounded polling for the operation a 「仍要创建」 override derived.
    ///
    /// Slot-free like `settleDecision`, and for the same reason, but it cannot
    /// borrow that loop: any poll here may hand the re-issued action over again
    /// — a lost reply, or a `202` — and a poll that read the action and dropped
    /// it would leave the operation parked until the sweep, with the event the
    /// user asked for never written. So every poll goes through the delivery
    /// gate against the override's own claim, which is what makes the second
    /// read a no-op instead of a second event. The loop shape is `settle`'s for
    /// the same reason it is `settle`'s: one shape is easier to check than two.
    private func settleOverride(
        _ first: OperationReceipt
    ) async throws -> OperationReceipt {
        var receipt = first
        var attempt = 0
        while !receipt.outcome.isSettled && attempt < pollDelays.count {
            try await sleep(pollDelays[attempt])
            attempt += 1
            receipt = try await backend.operation(operationID: receipt.operationID)
            if let reported = try await runDeliveredActions(receipt, claim: .override) {
                receipt = reported
                if reported.outcome.isSettled { break }
            }
        }
        return receipt
    }

    // --- polling --------------------------------------------------------------

    /// Execute a handed device action and settle the operation with the
    /// report, when the reply carries one.
    ///
    /// The three failure shapes all still report, because the server's
    /// operation parks at `source_in_progress` until it hears *something* and
    /// its 15-minute timeout sweep ends at `needs_manual_review` — a state a
    /// person has to clear by hand:
    ///
    /// - an action the envelope resolved but no executor is composed: one
    ///   honest `failed` ("this build cannot execute this");
    /// - an action the envelope refused *with* its action id (unknown tool,
    ///   missing fields, unreadable times): the refusal is reportable now —
    ///   the server learns it was not executed instead of waiting out the
    ///   sweep;
    /// - a refusal *without* an action id (the id itself was missing) names
    ///   nothing reportable, so nothing is sent and the sweep is the
    ///   remaining witness. That is the honest state: the action is unknown.
    ///
    /// A receipt without a device action passes through untouched, and the
    /// report result carries no new local state — the settled projection from
    /// the server is the only fact this function returns.
    private func runDeviceActionIfAny(
        _ receipt: OperationReceipt, pending: PendingSend
    ) async throws -> OperationReceipt {
        // The direct hand-off routes through the same exactly-once helper the
        // poll path uses: the server now delivers the action on every reply
        // that reads the parked projection, so the "same" action legitimately
        // arrives twice (immediate reply, then a poll), and the second arrival
        // must be a no-op. (Review R6, 2026-09-08.)
        guard let reported = try await runDeliveredActions(
            receipt, claim: .message(pending)
        ) else {
            return try await settle(receipt, pending: pending)
        }
        // A settled report needs no polling; an unexpectedly non-terminal
        // projection still gets the bounded loop, reading the server's state
        // rather than trusting the report's echo.
        if reported.outcome.isSettled {
            if reported.outcome.releasesPendingSlot {
                // Fourth review H1: the report may have landed long after the
                // user discarded this message — the slot can belong to a
                // different send by now. Clearing unconditionally deleted
                // that message's pending. The release verifies ownership,
                // exactly like the anchor merge.
                try? releaseSlotIfStillOwned(
                    key: pending.idempotencyKey, operationID: pending.operationID
                )
            }
            return reported
        }
        return try await settle(reported, pending: pending)
    }

    /// Execute and report every action a reply hands over, in plan order, and
    /// answer with the message's own projection — item 0's — or `nil` when the
    /// reply carried no action at all (design §4.1: item 0 is the message's
    /// operation; §4.2: 交付循环逐项执行、逐项回报).
    ///
    /// Every item runs, not just the first. Each is a separate operation on the
    /// server, parked separately, and only the actions actually delivered on
    /// this reply will ever be delivered — the server hands each one over on
    /// the projection of *its own* operation, and this device never reads the
    /// siblings' operations to poll them. Executing only item 0 would leave
    /// every sibling parked until the sweep turned it into needs_manual_review
    /// for a write the device was told to make.
    ///
    /// The return value is item 0's because that is the operation this turn is
    /// about: its projection is the card the user is watching and the one the
    /// pending slot belongs to. A sibling's receipt is not returned, but it is
    /// *read*: it answers about the sibling's own operation, and that answer is
    /// the only place this device ever learns a sibling's operation id — the
    /// delivered action names its action id, which the server refuses as a
    /// lookup key for a derived item, and the whole list arrives on item 0's
    /// projection. The id is written to the slot, which is what lets the
    /// message know how much of itself is still outstanding (design §4.2:
    /// 每项独立持有 operationID).
    ///
    /// `nil` is *no action*, never a lost report: `handleDeliveredAction`
    /// always answers with a receipt, degrading to the parked one it was given
    /// when a report's reply is lost, so the two cases cannot be confused.
    private func runDeliveredActions(
        _ receipt: OperationReceipt, claim: DeliveryClaim
    ) async throws -> OperationReceipt? {
        guard !receipt.deviceActions.isEmpty else { return nil }
        var primary: OperationReceipt?
        var releasing: [String] = []
        for (index, envelope) in receipt.deviceActions.enumerated() {
            let reported = try await handleDeliveredAction(
                envelope, claim: claim, pollReceipt: receipt
            )
            // Bookkeeping, not safety: losing this write costs a slot that
            // cannot release, while *throwing* here would abandon the actions
            // after this one and skip writes the server is waiting on. So it
            // is best-effort, unlike the delivery marker it sits beside.
            try? recordDeliveredOperation(reported.operationID, claim: claim)
            if reported.outcome.releasesPendingSlot {
                releasing.append(reported.operationID)
            }
            if index == 0 { primary = reported }
        }
        // Releases are applied only once the whole list has run. Clearing in
        // the middle would take the delivery marker with it, and the next
        // item would read an empty slot as "not mine to run" — skipping a
        // write the server is still waiting on.
        if case .message(let pending) = claim {
            for operationID in releasing {
                try? releaseSlotIfStillOwned(
                    key: pending.idempotencyKey, operationID: operationID
                )
            }
        }
        // Non-empty list ⇒ set: item 0's branch always returns a receipt.
        return primary ?? receipt
    }

    /// Write one operation this message produced onto the durable slot.
    ///
    /// The list grows as the server answers: the anchor merge names the
    /// message's own item, and each action's report names the item that action
    /// belongs to. Appending is idempotent, and the ownership rule is the
    /// delivery marker's — a report that lands after the user discarded and
    /// replaced the message must not write into the replacement's slot.
    private func recordDeliveredOperation(
        _ operationID: String, claim: DeliveryClaim
    ) throws {
        guard case .message(let pending) = claim else { return }
        guard var stored = try loadPending(),
              stored.idempotencyKey == pending.idempotencyKey,
              stored.operationID == pending.operationID
        else { return }
        guard !stored.operationIDs.contains(operationID) else { return }
        stored.operationIDs.append(operationID)
        try savePending(stored)
    }

    /// Execute (or skip, if already executed) a delivered device action and
    /// report what happened. This is the exactly-once gate for every shape
    /// that can carry an action — the direct chat reply and any poll that
    /// read the parked projection (review R6, 2026-09-08).
    ///
    /// The delivery marker is persisted **before** the executor runs: a crash
    /// between marker and report leaves an executed-but-unreported action,
    /// which the server's sweep parks for review — the honest outcome. The
    /// reverse order (report, then marker) would let a crash re-execute, and
    /// a duplicate event is the one failure a retry may never produce.
    private func handleDeliveredAction(
        _ envelope: DeviceActionEnvelope,
        claim: DeliveryClaim,
        pollReceipt: OperationReceipt
    ) async throws -> OperationReceipt {
        switch envelope.resolve() {
        case .execute(let action):
            guard try recordDelivery(actionID: action.actionID, claim: claim) else {
                // Already executed on a previous delivery of this same
                // action: report nothing and keep polling the server's
                // state. The silence is the sweep's to interpret.
                return pollReceipt
            }
            if let executor = deviceActionExecutor {
                // A lost report reply degrades to the parked projection this
                // call was given — the one that names the right operation by
                // construction. (Review R9, 2026-09-08; design §4.2.)
                let reported = await executor.executeAndReport(action)
                // The report landed: the calendar has been written, and the
                // mirror has to catch up before the next query reads it back
                // as missing (design §9.1). Fired before returning, so the
                // pass is already armed when the query window opens.
                if reported != nil { await calendarWriteSink?() }
                return reported ?? pollReceipt
            } else {
                return try await reportFailure(
                    actionID: action.actionID,
                    detail: "no executor is composed on this device"
                )
            }
        case .refuse(let actionID, let error):
            guard let actionID else {
                // Cannot name what it refuses: nothing to report, and the
                // polling loop re-reads the real state either way.
                return pollReceipt
            }
            guard try recordDelivery(actionID: actionID, claim: claim) else {
                // The refusal was already reported for this action.
                return pollReceipt
            }
            return try await reportFailure(
                actionID: actionID,
                detail: "the device refused the action: \(error)"
            )
        }
    }

    /// Claim an action for execution: record its id as delivered and answer
    /// whether *this* call won the right to run it.
    ///
    /// The marker lives on disk, not in memory — `PendingSend` is a value
    /// type, and the poll loop must see what the last call actually wrote.
    /// The check-then-write reads the stored record fresh, so a second
    /// delivery of the same action (reply, then poll; or after a restart)
    /// reads back the id and loses the claim — exactly once, by construction.
    ///
    /// The claim is persisted **before** the executor runs: a crash between
    /// marker and report leaves an executed-but-unreported action, which the
    /// server's sweep parks for review — the honest outcome. The reverse
    /// order would let a crash re-execute, and a duplicate event is the one
    /// failure a retry may never produce. A missing stored record (the slot
    /// was already released) is read as "not mine to run" and also refuses:
    /// executing into a released slot is how a duplicate is born.
    ///
    /// The override's claim (design §3.3) makes the same promise against a
    /// marker no slot owns, and it is the one carrying the most weight: the
    /// re-issued action says `skip_local_dedup`, so the duplicate check is off
    /// by design and this claim is the *only* thing between a double tap and
    /// two events. It is taken the same way and at the same point — before the
    /// executor runs — and read fresh, so a second delivery of the same action
    /// loses the claim here.
    private func recordDelivery(
        actionID: String, claim: DeliveryClaim
    ) throws -> Bool {
        switch claim {
        case .message(let pending):
            guard var stored = try loadPending(),
                  stored.operationID == pending.operationID
            else { return false }
            guard !stored.deliveredActionIDs.contains(actionID) else { return false }
            stored.deliveredActionIDs.append(actionID)
            try savePending(stored)
            return true
        case .override:
            // Never cleared by a successful run, unlike the message marker's
            // slot: deleting the entry is how the write it guards becomes
            // repeatable, and there is nothing to delete it for. The set is
            // capped, and the server stops delivering an action once its
            // operation settles, so an entry is only ever needed again while
            // its operation is still parked.
            var claimed = try loadOverrideClaims()
            guard !claimed.contains(actionID) else { return false }
            claimed.append(actionID)
            try saveOverrideClaims(claimed)
            return true
        }
    }

    /// Report an execution failure against the action's id and return the
    /// server's settled projection. The report endpoint itself answers with
    /// the settled operation — its CAS guarantees exactly-once, and the
    /// projection is what the UI renders, so no separate read-back is needed.
    private func reportFailure(actionID: String, detail: String) async throws -> OperationReceipt {
        try await backend.reportDeviceActionResult(
            actionID: actionID, body: .failed(detail: detail)
        )
    }

    private func settle(
        _ first: OperationReceipt, pending: PendingSend
    ) async throws -> OperationReceipt {
        var receipt = first
        if let stage = Self.stage(of: receipt) {
            await report(stage)
        }
        var attempt = 0
        while !receipt.outcome.isSettled && attempt < pollDelays.count {
            try await sleep(pollDelays[attempt])
            attempt += 1
            receipt = try await backend.operation(operationID: receipt.operationID)
            if let stage = Self.stage(of: receipt) {
                await report(stage)
            }
            // The parked projection can hand the actions over on any poll
            // (review R6): a send that detached at 202, or a lost reply,
            // reaches its actions here. Executing them is what settles the
            // operations, so every delivered action is handled inside the loop
            // and the loop continues from item 0's answer — the operation this
            // slot and this card belong to.
            if let reported = try await runDeliveredActions(
                receipt, claim: .message(pending)
            ) {
                receipt = reported
                if reported.outcome.isSettled { break }
            }
        }
        if receipt.outcome.releasesPendingSlot {
            // Polling may stop without releasing the slot. Unknown and
            // needs-manual-review outcomes stay on disk because the client
            // giving up on watching is not proof that another key is safe.
            // The release also verifies ownership (fourth review H1): a poll
            // answering after the slot was discarded and replaced must not
            // delete the replacement.
            try? releaseSlotIfStillOwned(
                key: pending.idempotencyKey, operationID: receipt.operationID
            )
        }
        return receipt
    }

    /// Clear the pending slot **only if it still belongs to the message that
    /// earned the release** (fourth review H1).
    ///
    /// Every path that clears the slot after a network wait — the settled
    /// report, the settle loop's terminal poll, a cancellation — can land
    /// after the user discarded that message and sent another; the slot then
    /// holds a different idempotency key, and deleting it would strand the
    /// new message's write. Ownership is the pair (key, operation): the key
    /// names the message, the operation names the server-side work this
    /// release speaks for. A slot with a `nil` operation matches by key alone
    /// (the discard-and-replace window before any reply anchors).
    ///
    /// A release is earned **per operation**, and the slot clears only once
    /// every operation the message produced has earned one. One message may be
    /// several operations (design §4.1), and its own item settling says
    /// nothing about a sibling whose write may or may not exist — releasing on
    /// item 0 alone is how the user comes to ask for an event a parked sibling
    /// already created. A message with nothing tracked yet keeps the old
    /// key-only rule: there is no operation to disagree with.
    private func releaseSlotIfStillOwned(
        key: String, operationID: String?
    ) throws {
        guard var stored = try loadPending() else { return }
        guard stored.idempotencyKey == key else { return }
        guard let operationID, !stored.operationIDs.isEmpty else {
            // No operation to attribute: a refusal that never anchored
            // anything, a give-up with no reply ever seen. The key named by
            // the caller is the only owner there is to check.
            try clearPending()
            return
        }
        guard stored.operationIDs.contains(operationID) else {
            // This release speaks for an operation that is not this message's.
            // Clearing would strand the replacement's write.
            return
        }
        if !stored.releasedOperationIDs.contains(operationID) {
            stored.releasedOperationIDs.append(operationID)
            try savePending(stored)
        }
        // Something is still outstanding: an item that has not settled, or one
        // that settled into a state the client may not treat as safe to forget
        // (`needs_manual_review` above all). The slot stays, and stays as the
        // one thing blocking the next send, until a person resolves it.
        let outstanding = stored.operationIDs.filter {
            !stored.releasedOperationIDs.contains($0)
        }
        guard outstanding.isEmpty else { return }
        try clearPending()
    }

    /// Run the by-key trail concurrently with the chat POST.
    ///
    /// The POST can hold the connection for up to 30 seconds, and that window is
    /// exactly when the model turn runs: interpreting, tool selection, the
    /// dispatch. The client has no operation id yet, so the only door is
    /// `GET /v1/operations/by-key/{key}` — and the key is the one this send
    /// persisted before the request left. Every failure there (unanchored,
    /// transport, 5xx) means "no evidence this instant", never a conclusion
    /// about a write, so all of it is swallowed and the loop simply continues.
    /// The schedule is the same bounded one settle uses — 8 waits, ~26s — so a
    /// POST that runs to its full 30s ceiling leaves the trail idle for the
    /// last stretch; the settle loop owns that tail by id. The task is
    /// cancelled the moment the POST answers.
    ///
    /// Every poll is preceded by a wait, the first one included. A POST that
    /// settles inside the first window then cancels a task that has not yet
    /// touched the network — a fast send costs zero by-key round trips. It also
    /// skips the pointless t=0 poll in production, where anchoring cannot have
    /// happened before the server has even parsed the POST.
    private func startTrailRunner(key: String) -> Task<Void, Never> {
        let stageOf = Self.stage(of:)
        let sink = progressSink
        let delays = pollDelays
        let sleep = self.sleep
        let backend = self.backend
        return Task {
            var attempt = 0
            while attempt < delays.count {
                try? await sleep(delays[attempt])
                if Task.isCancelled { break }
                attempt += 1
                // Best effort by contract: any throw is "no evidence yet".
                let receipt = try? await backend.operation(idempotencyKey: key)
                if let receipt, let stage = stageOf(receipt) {
                    await sink?(stage)
                }
            }
        }
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

    private func loadDecisions() throws -> [PendingDuplicateDecision] {
        guard let data = try store.read(CredentialKey.pendingDuplicateDecisions) else {
            return []
        }
        do {
            return try JSONDecoder().decode([PendingDuplicateDecision].self, from: data)
        } catch {
            throw ChatError.pendingDecisionsMalformed
        }
    }

    private func saveDecisions(_ decisions: [PendingDuplicateDecision]) throws {
        if decisions.isEmpty {
            try store.delete(CredentialKey.pendingDuplicateDecisions)
            return
        }
        try store.write(
            CredentialKey.pendingDuplicateDecisions,
            value: try JSONEncoder().encode(decisions)
        )
    }

    /// How many override claims are remembered.
    ///
    /// The marker only has to outlive one in-flight tap, and an entry is only
    /// ever needed again while its operation is still parked — the server stops
    /// delivering an action once it settles — so a dropped entry has never been
    /// the one still owed. Sixty-four is far past the number of taps that can
    /// be in flight inside a single window.
    private static let overrideClaimLimit = 64

    private func loadOverrideClaims() throws -> [String] {
        guard let data = try store.read(CredentialKey.claimedOverrideActions) else {
            return []
        }
        do {
            return try JSONDecoder().decode([String].self, from: data)
        } catch {
            // Reported, never reset to empty: an empty set means "nothing was
            // ever written", and the override would then run its write again.
            throw ChatError.overrideClaimsMalformed
        }
    }

    private func saveOverrideClaims(_ claims: [String]) throws {
        let bounded = Array(claims.suffix(Self.overrideClaimLimit))
        if bounded.isEmpty {
            try store.delete(CredentialKey.claimedOverrideActions)
            return
        }
        try store.write(
            CredentialKey.claimedOverrideActions,
            value: try JSONEncoder().encode(bounded)
        )
    }

    private func removeDecision(_ checkID: String) throws {
        try saveDecisions(try loadDecisions().filter { $0.checkID != checkID })
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
        startNewSession: Bool,
        idempotencyKey: String
    ) async throws -> OperationReceipt

    func operation(operationID: String) async throws -> OperationReceipt

    /// The progress trail's poll: the same projection as `operation(operationID:)`,
    /// resolved by the idempotency key the client already holds. The server
    /// answers `400 OPERATION_NOT_ANCHORED` while the key names no operation yet,
    /// which the caller reads as "keep waiting".
    func operation(idempotencyKey: String) async throws -> OperationReceipt

    func cancelOperation(operationID: String) async throws -> OperationReceipt

    func timelinePage(
        conversationID: String,
        cursor: String?,
        direction: TimelineDirection,
        limit: Int?
    ) async throws -> TimelinePageResponse

    /// `DEV-031`. Resolve one parked duplicate under the caller's idempotency key.
    func decideDuplicate(
        checkID: String,
        decision: DuplicateDecision,
        idempotencyKey: String
    ) async throws -> OperationReceipt

    /// `DEV-040`. Record what a person found in the ledger for one operation
    /// parked at `needs_manual_review`. No client key: see `AgentClient`.
    func resolveManualReview(
        operationID: String,
        resolution: ManualResolution
    ) async throws -> ManualResolutionReceipt

    /// `G1`. Correct one recorded expense's 分类 from the receipt card.
    ///
    /// Carries a client key, unlike `resolveManualReview`: this one really does
    /// write to the ledger, so a retry has to land on the same idempotency slot
    /// rather than being re-decided.
    func updateExpenseCategory(
        recordID: String,
        category: String,
        expectedCurrentCategory: String?,
        idempotencyKey: String
    ) async throws -> OperationReceipt

    /// Settle a device-executed action this device was handed. The report is
    /// the evidence the server settles on; its CAS makes a retry after a lost
    /// reply safe (a replay answers the settled projection, never re-migrates),
    /// so the caller may resend without a client idempotency key.
    func reportDeviceActionResult(
        actionID: String,
        body: DeviceActionResultBody
    ) async throws -> OperationReceipt

    /// Answer 「仍要创建」 for a calendar write this device reported as a
    /// duplicate (design §3.3). No client key: the server derives it from the
    /// action's own operation, so every tap and every retry converge on one
    /// derived operation.
    func overrideDeviceAction(actionID: String) async throws -> OperationReceipt

    /// Upload one calendar mirror batch. `window_complete` on the last batch
    /// authorises the server to mark window events absent from the upload as
    /// deleted — the device is the fact source. `snapshotAsOf` is the batch's
    /// version, identical across every batch of one window (second review F1:
    /// the server's schema requires it).
    ///
    /// `calendars` rides on every batch and is idempotent (§2.1). It carries
    /// **all** ordinary event calendars, subscribed ones included, so the
    /// server can recognise and refuse them; the `events` alongside it come
    /// only from the non-subscribed ones.
    func uploadCalendarSync(
        windowStart: Date,
        windowEnd: Date,
        events: [CalendarMirrorEvent],
        calendars: [CalendarDirectoryEntry],
        windowComplete: Bool,
        snapshotAsOf: Date
    ) async throws -> CalendarSyncResponse
}
