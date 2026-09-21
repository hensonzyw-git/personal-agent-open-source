import Foundation
import Observation
import PersonalAgentKit
import UIKit

/// The view state for `DEV-030`'s chat surface.
///
/// Everything that could be wrong is in `PersonalAgentKit` and tested with
/// `swift test`; this type only mirrors it for SwiftUI. In particular it holds no
/// opinion about whether a write happened — it renders
/// `OperationReceipt.outcome`, which is derived from `state` and `record_id` and
/// never from model prose.
@MainActor
@Observable
final class ChatModel {
    private struct PendingPhotoSend: Codable {
        let localPath: String
        var upload: PendingMediaUpload
        let text: String
        let clarificationOf: String?
        let startNewSession: Bool
        var chatKey: String?
    }
    /// Oldest-to-newest, exactly as the server ordered it.
    var events: [TimelineEvent] = []
    var hasOlder = false
    var focusedDevelopmentEventID: String?
    var draft: String = ""
    /// The photo has only been prepared locally.  It is not a Timeline event,
    /// not a server object and not an authorization to send until the user taps
    /// the ordinary send action.
    var preparedPhoto: PreparedPhoto?
    var imageCapability = Capabilities.ImageInputCapability()
    /// The message that has left the composer but whose Timeline event has not come
    /// back yet.
    ///
    /// §3.4's first half: the message goes up immediately and then shows its own
    /// waiting state, instead of sitting in the composer for the whole round trip
    /// and appearing together with the result.
    ///
    /// It is **not** a Timeline event and never becomes one. It is cleared the
    /// moment the server's own copy arrives through `syncNewer`/`mirror`, so the
    /// rule that history is read back from the server rather than invented locally
    /// still holds — this is a pending element in front of the history, not an entry
    /// in it.
    ///
    /// §3.4's *second* half, the composer staying usable so a second message can be
    /// sent while this one is in flight, is deliberately not implemented: it depends
    /// on write serialisation that does not exist yet, and without it two concurrent
    /// writes both pass the duplicate gate.
    private(set) var sending: String?
    /// §1c: what `/v1/capabilities` says this device may do, for the empty state's
    /// 能力清单. Empty until the first successful read -- an unread capability set is
    /// not the same as an empty one, so the empty state omits the section entirely
    /// rather than claiming nothing is available.
    var tools: [Capabilities.Tool] = []
    /// §1d's 打开飞书账本 action on a receipt. Re-validated on every refresh through
    /// the same `Capabilities.validatedLedgerURL` the review surface uses, so the
    /// two can never disagree about whether a jump is safe to offer.
    var ledgerURL: URL?
    /// The receipt of the message in hand, live or just settled.
    var liveReceipt: OperationReceipt?
    /// The progress trail: where the in-flight operation has provably reached,
    /// newest last. Every entry is read off the server's operation projection —
    /// stage names and the tool the server recorded — never off model prose.
    /// Cleared when the send's round trip ends, because the settled receipt is
    /// then the full truth on screen.
    private(set) var liveStages: [OperationStage] = []
    /// Whether a trail is worth showing: the optimistic bubble is up and the
    /// operation has not settled.
    var showProgressTrail: Bool { sending != nil && !liveStages.isEmpty }
    /// Set while a parked clarification is being answered, so the next send
    /// carries `clarification_of` instead of starting an unrelated message.
    var answering: (operationID: String, question: String)?
    /// The next submitted message is a user-confirmed, server-side Session
    /// reset. It remains local until `ChatTimeline` persists its idempotent key.
    var startNewTopic = false
    /// An unresolved message this launch inherited. Shown rather than hidden: it
    /// may hold a write nobody has confirmed yet.
    var unresolved: ChatTimeline.PendingSend?
    private(set) var hasPendingPhotoSend = false
    /// `DEV-031`. Decisions submitted whose reply never arrived, keyed by check
    /// id so a card can show "已提交，待确认" instead of offering the choice
    /// again — offering it would be refused by the server anyway.
    var pendingDecisions: [String: ChatTimeline.PendingDuplicateDecision] = [:]
    /// Decisions the server has durably projected into the Timeline. Historic
    /// duplicate prompts consult this map so a handled card never offers the
    /// buttons again after a refresh or app restart.
    var resolvedDuplicateDecisions: [String: String] = [:]
    /// `DEV-040`. Operations whose `needs_manual_review` card a person has already
    /// answered, keyed by operation id, holding the raw resolution wire value.
    /// Populated the same way as `resolvedDuplicateDecisions`: from this session's
    /// own accepted call, and from the permanent Timeline marker once it is inside
    /// the loaded window.
    var resolvedManualReviews: [String: String] = [:]
    /// `G1`. The newest ledger row this session has seen for each `record_id`.
    ///
    /// This is what makes Henson's 2026-08-15 decision true: the card follows
    /// the ledger's current value rather than freezing at what was first
    /// written. A category correction is a *new* operation describing the *same*
    /// ledger row, so keying by `record_id` — not by operation — lets the
    /// original receipt, scrolled back to weeks later, show the corrected
    /// category rather than the one the model first guessed.
    ///
    /// Storage stays honest underneath: the server never rewrites the original
    /// operation's sealed record, and the Timeline stays append-only. Only the
    /// *display* resolves to the newest, which is the only layer where "current"
    /// is the right answer.
    var currentRecords: [String: FinanceExpenseRecord] = [:]
    /// In-flight and failed category corrections, keyed by `record_id`.
    var categoryEdits: [String: CategoryEditState] = [:]
    /// `1j`. The newest `daily_review` event id per `review_id`.
    ///
    /// A late-verified write reopens a card and seals a *newer* snapshot as a
    /// second event with the same `review_id`. The Timeline stays append-only,
    /// so both events remain; this map lets the screen resolve the card to the
    /// newest snapshot and collapse the older one instead of drawing the same
    /// review twice with two different item counts.
    private(set) var latestDailyReviewEventID: [String: String] = [:]
    var lastError: String?
    var busy = false
    var loadingOlder = false

    private var acceptanceStopping = false
    private var acceptanceWork = 0
    func stopForAcceptanceReset() async {
        acceptanceStopping = true
        while acceptanceWork > 0 { try? await Task.sleep(for: .milliseconds(50)) }
    }

    var developmentReplyContext: DevelopmentReplyContext?
    var loadDevelopmentContext: ((String) async throws -> DevelopmentReplyContext)?
    var loadDevelopmentDocument: ((String) async throws -> DevelopmentDocument)?
    private let timeline: ChatTimeline
    private let mediaUploads: MediaUploadCoordinator
    private let mediaBackend: any MediaUploadBackend
    private let store: CredentialStore
    private let describe: @MainActor (Error) -> String
    /// The pre-send mirror top-up (review R5), owned by the composition and
    /// optional so tests compose without one. Absent ⇒ no top-up is attempted.
    /// Returns a handle whose `wait()` bounds how long the send path blocks on
    /// the top-up: past the budget the send proceeds and the sync continues
    /// in the background (second review F8 — a permission prompt or a slow
    /// upload must never stop an unrelated 记账 message).
    var onSyncMirror: (() -> MirrorSyncHandle)?

    /// The client-local calendar query gate (design §9.1), owned by the
    /// composition and optional so tests compose without one. Absent ⇒ the gate
    /// reports clean, which is the honest reading of a device with no mirror
    /// engine: there is nothing it has changed and failed to upload.
    var onReadCalendarUnsynced: (@MainActor () async -> Bool)?

    /// The last answer from `onReadCalendarUnsynced`.
    ///
    /// A read-only mirror of a client-local fact, never a server projection:
    /// the server's `mirror_stale` is a different statement about a different
    /// source, and neither substitutes for the other (design §9.1). It is
    /// re-read on every `mirror()`, which is the one funnel every path that can
    /// change the screen — open, refresh, pagination, a settled send — goes
    /// through, so a card drawn from history is drawn against the flag as it is
    /// now rather than as it was when the card arrived.
    private(set) var calendarUnsynced = false

    init(
        timeline: ChatTimeline,
        mediaBackend: any MediaUploadBackend,
        store: CredentialStore,
        describe: @escaping @MainActor (Error) -> String
    ) {
        self.timeline = timeline
        self.mediaUploads = MediaUploadCoordinator(backend: mediaBackend)
        self.mediaBackend = mediaBackend
        self.store = store
        self.describe = describe
        // The trail survives for exactly one round trip: it starts when a send
        // starts and is wiped when that send ends. The sink is @Sendable, so it
        // captures its own weak self and hops to the main actor — SwiftUI reads
        // `liveStages` there. The outer Task exists only to register the sink
        // through the actor's async method and captures nothing.
        Task { [weak self] in
            await timeline.setProgressSink { [weak self] stage in
                await self?.appendStage(stage)
            }
        }
    }

    /// One provable stage arrived from the poll loop.
    private func appendStage(_ stage: OperationStage) async {
        liveStages.append(stage)
    }

    /// Open the Timeline the server named, then finish anything left unresolved.
    func open(conversationID: String) async {
        guard !acceptanceStopping else { return }
        acceptanceWork += 1
        defer { acceptanceWork -= 1 }
        await timeline.bind(conversationID: conversationID)
        // `bind` drops the loaded history when the Timeline changes, so the
        // decisions projected out of it are dropped here too. Every later
        // `mirror()` merges rather than replaces, and this is the one place that
        // is allowed to forget.
        resolvedDuplicateDecisions = [:]
        resolvedManualReviews = [:]
        // Same rule: `mirror()` merges, and this is the one place allowed to
        // forget. A row corrected in another Timeline is not this one's fact.
        currentRecords = [:]
        categoryEdits = [:]
        latestDailyReviewEventID = [:]
        busy = true
        defer { busy = false }
        do {
            try await timeline.loadLatest()
            await mirror()
            // Resuming comes *after* history so the receipt lands under the
            // message it belongs to rather than above an empty screen.
            var needsSync = false
            // A chat slot wins over the media slot. Once `ChatTimeline` has
            // persisted the sealed parts/key, retrying upload would only trip
            // its unresolved-send guard and hide the real recovery path.
            if let receipt = try await timeline.resume() {
                liveReceipt = receipt
                needsSync = true
                if let photo = try loadPendingPhotoSend() {
                    try clearPendingPhotoSend(photo)
                }
            } else if let receipt = try await resumePendingPhotoSend() {
                liveReceipt = receipt
                needsSync = true
            }
            // `DEV-031`: the same recovery for decisions whose reply was lost.
            // Both resume before the final sync, so one page brings every
            // consequence the server recorded while this app was away.
            let resumedDecisions = try await timeline.resumeDecisions()
            if !resumedDecisions.isEmpty {
                liveReceipt = resumedDecisions.last
                needsSync = true
            }
            if needsSync {
                try await timeline.syncNewer()
            }
            await mirror()
            lastError = nil
        } catch {
            lastError = describe(error)
        }
        await mirrorPendingSlots()
    }

    /// Pull-to-refresh re-reads *everything* current, not just newer events
    /// (Henson, 2026-08-01). `syncNewer` alone cannot change the durable slots —
    /// they are local Keychain records — so this call adds no correctness the
    /// gesture did not already have. It exists so the gesture means one thing:
    /// after it, the screen shows current state from every source it has. It is
    /// also the only path on which a *corrupt* slot would otherwise stay
    /// unreported, since `pendingSendMalformed` surfaces through here.
    func refresh() async {
        guard !acceptanceStopping else { return }
        acceptanceWork += 1
        defer { acceptanceWork -= 1 }
        busy = true
        defer { busy = false }
        do {
            try await timeline.syncNewer()
            await mirror()
            lastError = nil
        } catch {
            lastError = describe(error)
        }
        await mirrorPendingSlots()
    }

    /// Pagination, and deliberately the one path that does not re-read the
    /// durable slots: it appends older history and makes no claim about current
    /// state, so `refresh()` — not this — is the gesture that means "show me
    /// everything as it is now".
    func focusDevelopmentEvent(_ id: String) async {
        focusedDevelopmentEventID = nil
        await refresh()
        for _ in 0..<20 {
            if events.contains(where: { $0.eventID == id }) {
                focusedDevelopmentEventID = id
                return
            }
            guard hasOlder, !Task.isCancelled else { break }
            let count = events.count
            await loadOlder()
            if count == events.count { break }
        }
        lastError = "通知对应的消息暂未加载，请在 Timeline 继续加载历史消息。"
    }

    func loadOlder() async {
        guard !acceptanceStopping else { return }
        acceptanceWork += 1
        defer { acceptanceWork -= 1 }
        guard !loadingOlder else { return }
        loadingOlder = true
        defer { loadingOlder = false }
        do {
            _ = try await timeline.loadOlder()
            await mirror()
        } catch {
            lastError = describe(error)
        }
    }

    func send() async {
        guard !acceptanceStopping else { return }
        acceptanceWork += 1
        defer { acceptanceWork -= 1 }
        guard !busy else { return }
        do {
            if try loadPendingPhotoSend() != nil {
                await resumeUnresolved()
                return
            }
        } catch {
            lastError = describe(error)
            return
        }
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        let photo = preparedPhoto
        guard !text.isEmpty || photo != nil else { return }
        if photo != nil && developmentReplyContext != nil {
            lastError = "回复开发文档时请发送文字；图片可以另发一条消息。"
            return
        }
        let replyContext = developmentReplyContext
        busy = true
        defer { busy = false }
        // Consume this draft synchronously. The field remains editable while
        // Calendar sync waits, so no later continuation may clear its new text.
        let clarificationOf = answering?.operationID
        let startNewSession = startNewTopic
        draft = ""
        // The pre-send mirror top-up (review R5), **bounded** (second review
        // F8): a calendar question in this very message is answered against
        // the mirror, so a stale one is topped up first — but a permission
        // prompt, a slow network or a multi-batch upload is not allowed to
        // stop an unrelated 记账 message. The wait has a budget; past it the
        // send proceeds while the sync keeps running in the background, and
        // the server labels a still-stale answer honestly.
        let syncHandle = onSyncMirror?()
        await syncHandle?.wait()
        // Out of the composer and onto the screen before the request leaves.
        sending = text.isEmpty ? "[图片]" : text
        liveStages = []
        defer { sending = nil; liveStages = [] }
        do {
            let receipt: OperationReceipt
            if let photo {
                receipt = try await uploadAndSend(
                    photo: photo,
                    text: text,
                    clarificationOf: clarificationOf,
                    startNewSession: startNewSession
                )
                // The composer selection was consumed at durable preparation,
                // not here: a newer selection may have arrived during upload.
            } else {
                receipt = try await timeline.send(
                    text: text,
                    clarificationOf: clarificationOf,
                    startNewSession: startNewSession,
                    dalReplyContext: replyContext
                )
            }
            developmentReplyContext = nil
            answering = nil
            startNewTopic = false
            liveReceipt = receipt
            // The POST and device execution have returned. History refresh is
            // not another send and must not keep its progress indicator alive.
            sending = nil
            liveStages = []
            lastError = nil
            // The user message and the result are Timeline events, so the screen
            // reads them back from the server rather than inventing local copies.
            try await timeline.syncNewer()
            // Resolving override buttons on older receipts is unrelated to
            // this send. Refresh/open can perform those network lookups.
            await mirror(refreshLegacyOverrides: false)
        } catch {
            lastError = describe(error)
        }
        await mirrorPendingSlots()

        // Give the text back only when nothing is tracking it.
        //
        // `ChatTimeline.send` persists the pending record *before* the request
        // leaves, so an ambiguous failure leaves a slot behind and `unresolved`
        // owns the message -- restoring the draft there would invite a second send
        // against a first one that may well have landed. The pending record is
        // cleared only when the server proved it never anchored, and that is
        // exactly the case where the message is genuinely gone and the user should
        // find their words still in the box.
        if lastError != nil, unresolved == nil, !hasPendingPhotoSend, draft.isEmpty {
            draft = text
        }
    }

    /// Task actions have their own input; never consume the ordinary composer,
    /// selected photo, pending document reply, or another topic's clarification.
    func sendDevelopmentAction(_ action: DevelopmentTaskAction, taskID: String, text: String = "") async throws {
        guard !busy, !acceptanceStopping else { throw DevelopmentActionError.busy }
        let command = try action.command(taskID: taskID, text: text)
        busy = true
        acceptanceWork += 1
        defer { busy = false; acceptanceWork -= 1; sending = nil }
        guard try loadPendingPhotoSend() == nil,
              try await timeline.pendingSend() == nil else { throw DevelopmentActionError.busy }
        sending = DevelopmentTaskAction.displayText(command)
        do {
            let receipt = try await timeline.send(text: command)
            liveReceipt = receipt
            lastError = nil
            // A failed refresh after POST is not permission to resend.
            do { try await timeline.syncNewer() }
            catch { lastError = "操作已发送，结果同步暂未完成，请刷新对话查看。" }
            await mirror(refreshLegacyOverrides: false)
            await mirrorPendingSlots()
        } catch {
            lastError = describe(error)
            await mirrorPendingSlots()
            throw error
        }
    }

    func preparePhoto(_ data: Data) {
        do {
            preparedPhoto = try PhotoPreparation.prepare(data, capability: imageCapability)
            lastError = nil
        } catch {
            preparedPhoto = nil
            lastError = "照片无法安全处理，请重新选择或重拍。"
        }
    }

    func readPhoto(_ mediaID: String) async throws -> Data {
        try await mediaBackend.readMedia(mediaID: mediaID)
    }

    func clearPreparedPhoto() {
        preparedPhoto = nil
    }

    func discardUnsentPhoto() async {
        guard !acceptanceStopping else { return }
        acceptanceWork += 1
        defer { acceptanceWork -= 1 }
        guard !busy else { return }
        do {
            guard try await timeline.pendingSend() == nil else {
                lastError = "消息已提交，需先确认消息结果，不能放弃图片后重新发送。"
                return
            }
            if let pending = try loadPendingPhotoSend() {
                try clearPendingPhotoSend(pending)
            }
            preparedPhoto = nil
            lastError = nil
        } catch { lastError = describe(error) }
        await mirrorPendingSlots()
    }

    func appendVoiceDraft(_ transcript: String) {
        guard !transcript.isEmpty else { return }
        draft = draft.isEmpty ? transcript : "\(draft) \(transcript)"
    }

    /// Every network phase is preceded by a durable record. On restart we resume
    /// the exact upload state, or `ChatTimeline` resumes the sealed chat request.
    private func uploadAndSend(
        photo: PreparedPhoto,
        text: String,
        clarificationOf: String?,
        startNewSession: Bool
    ) async throws -> OperationReceipt {
        let localPath = try PhotoStaging.write(photo).path
        let declaration = MediaUploadDeclaration(
            mime: "image/jpeg", size: photo.data.count, sha256: photo.sha256,
            width: photo.width, height: photo.height
        )
        var pending = PendingPhotoSend(
            localPath: localPath,
            upload: await mediaUploads.begin(declaration: declaration),
            text: text,
            clarificationOf: clarificationOf,
            startNewSession: startNewSession
        )
        try savePendingPhotoSend(pending)
        // Ownership moves to the durable recovery slot before the first
        // network call. Keep genuinely newer selections/edits untouched, and
        // never put this consumed image back into the composer on recovery.
        if preparedPhoto?.selectionID == photo.selectionID {
            preparedPhoto = nil
        }
        return try await continuePendingPhotoSend(&pending)
    }

    private func resumePendingPhotoSend() async throws -> OperationReceipt? {
        guard var pending = try loadPendingPhotoSend() else { return nil }
        return try await continuePendingPhotoSend(&pending)
    }

    private func continuePendingPhotoSend(_ pending: inout PendingPhotoSend) async throws -> OperationReceipt {
        if pending.chatKey == nil {
            pending.chatKey = IdempotencyKey.mint()
            try savePendingPhotoSend(pending)
        }
        let bytes = try PhotoStaging.read(pending.localPath)
        pending.upload = try await mediaUploads.create(pending.upload)
        try savePendingPhotoSend(pending)
        pending.upload = try await mediaUploads.put(pending.upload, bytes: bytes)
        try savePendingPhotoSend(pending)
        let completed = try await mediaUploads.complete(pending.upload)
        var parts: [ChatInputPart] = []
        if !pending.text.isEmpty { parts.append(.text(pending.text)) }
        parts.append(.imageReference(mediaID: completed.mediaID))
        // `ChatTimeline.send` synchronously persists its full parts/key before
        // issuing the first chat POST. Keep media recovery until that succeeds.
        let receipt = try await timeline.send(
            parts: parts,
            clarificationOf: pending.clarificationOf,
            startNewSession: pending.startNewSession,
            idempotencyKey: pending.chatKey
        )
        try clearPendingPhotoSend(pending)
        return receipt
    }

    private func loadPendingPhotoSend() throws -> PendingPhotoSend? {
        #if ADK_ACCEPTANCE
        guard try store.read(CredentialKey.pendingMediaSend) == nil else { throw AgentClientError.malformedResponse }
        return nil
        #else
        guard let data = try store.read(CredentialKey.pendingMediaSend) else { return nil }
        return try JSONDecoder().decode(PendingPhotoSend.self, from: data)
        #endif
    }

    private func savePendingPhotoSend(_ pending: PendingPhotoSend) throws {
        try store.write(CredentialKey.pendingMediaSend, value: JSONEncoder().encode(pending))
    }

    private func clearPendingPhotoSend(_ pending: PendingPhotoSend) throws {
        try store.delete(CredentialKey.pendingMediaSend)
        PhotoStaging.remove(pending.localPath)
    }

    /// Ask the server to cancel. Past a possible submit this only records the
    /// request; the state the server returns is still the answer.
    func cancelLive() async {
        guard !acceptanceStopping else { return }
        acceptanceWork += 1
        defer { acceptanceWork -= 1 }
        guard let operationID = liveReceipt?.operationID else { return }
        busy = true
        defer { busy = false }
        do {
            liveReceipt = try await timeline.cancel(operationID: operationID)
            try await timeline.syncNewer()
            await mirror()
            lastError = nil
        } catch {
            lastError = describe(error)
        }
        await mirrorPendingSlots()
    }

    /// Re-ask the server about the unresolved message.
    func resumeUnresolved() async {
        guard !acceptanceStopping else { return }
        acceptanceWork += 1
        defer { acceptanceWork -= 1 }
        busy = true
        liveStages = []
        defer { busy = false; liveStages = [] }
        do {
            if let receipt = try await timeline.resume() {
                liveReceipt = receipt
                if let photo = try loadPendingPhotoSend() { try clearPendingPhotoSend(photo) }
            } else {
                liveReceipt = try await resumePendingPhotoSend()
            }
            try await timeline.syncNewer()
            await mirror()
            lastError = nil
        } catch {
            lastError = describe(error)
        }
        await mirrorPendingSlots()
    }

    /// Abandon an unresolved message locally. Deliberately separate from
    /// `resumeUnresolved`, and deliberately not automatic: the operation may hold a
    /// write, so the id is shown first and the user decides.
    func discardUnresolved() async {
        guard !acceptanceStopping else { return }
        acceptanceWork += 1
        defer { acceptanceWork -= 1 }
        do {
            try await timeline.discardPending()
            lastError = nil
        } catch {
            lastError = describe(error)
        }
        // Re-read rather than assigning `unresolved = nil`. The assignment was
        // right — `discardPending` had succeeded — but it was the last place a
        // slot's displayed value came from what this code believed instead of
        // from the slot, and its twin `discardDecision` already re-read.
        await mirrorPendingSlots()
    }

    func beginAnswering(operationID: String, question: String) {
        answering = (operationID, question)
    }

    func cancelAnswering() {
        answering = nil
    }

    func prepareNewTopic() {
        // A clarification answer must retain its source Session. The explicit
        // reset intentionally abandons that pre-submit source server-side.
        answering = nil
        startNewTopic = true
        lastError = nil
    }

    func cancelNewTopic() {
        startNewTopic = false
    }

    // --- duplicate decisions (`DEV-031`) ---------------------------------------

    /// Resolve a parked duplicate as the user chose. The decision key lives in
    /// `ChatTimeline`; a lost reply is retried with the same key, so a flaky
    /// network cannot record the choice twice.
    func decideDuplicate(checkID: String, decision: DuplicateDecision) async {
        guard !acceptanceStopping else { return }
        acceptanceWork += 1
        defer { acceptanceWork -= 1 }
        busy = true
        defer { busy = false }
        do {
            let receipt = try await timeline.decide(checkID: checkID, decision: decision)
            // For `writeAnyway` this is the override operation's receipt, settled
            // or still running; either way it is the structured truth to show.
            liveReceipt = receipt
            resolvedDuplicateDecisions[checkID] = decision.rawValue
            try await timeline.syncNewer()
            await mirror()
            lastError = nil
        } catch {
            lastError = describe(error)
        }
        await mirrorPendingSlots()
    }

    /// Re-present a decision whose reply never arrived. The stored key is
    /// reused, so this is a replay, never a second decision.
    func retryDecision(_ pending: ChatTimeline.PendingDuplicateDecision) async {
        guard !acceptanceStopping else { return }
        acceptanceWork += 1
        defer { acceptanceWork -= 1 }
        await decideDuplicate(checkID: pending.checkID, decision: pending.decision)
    }

    // --- the category correction (`G1`) ----------------------------------------

    /// What a card should show while its 分类 is being corrected.
    enum CategoryEditState: Equatable {
        /// A correction is in flight; `target` is what was asked for, not what
        /// the ledger holds. The row shows it greyed beside a spinner, never as
        /// the value.
        case inFlight(target: String)
        /// The correction did not happen. The row still shows the ledger's
        /// value; this message says why the tap did not take.
        case failed(message: String)
    }

    func currentRecord(forRecordID recordID: String) -> FinanceExpenseRecord? {
        currentRecords[recordID]
    }

    func categoryEdit(forRecordID recordID: String) -> CategoryEditState? {
        categoryEdits[recordID]
    }

    /// Correct one recorded expense's 分类.
    ///
    /// Three properties, all of them deliberate:
    ///
    /// - **nothing is optimistic.** `currentRecords` is written only from a
    ///   receipt the server settled as `recorded`, which the server only issues
    ///   after reading the row back from the ledger. Showing the new category on
    ///   tap would make the receipt card assert an unverified write — the one
    ///   thing the whole projection exists to prevent, on the one screen that
    ///   exists to be checkable.
    /// - **a no-op is not a request.** Choosing the category the row already has
    ///   sends nothing. The server would answer `already_current` and it would
    ///   be harmless, but spending a governed write and an idempotency slot on a
    ///   tap that changes nothing is not harmless.
    /// - **the idempotency key is the Kit's to mint.** This screen never makes
    ///   one: `IdempotencyKey.mint` is the single place this client spells a key,
    ///   and a screen minting its own is how an upper-case UUID reached
    ///   production and had every write refused.
    func changeCategory(
        recordID: String, from current: String?, to target: String
    ) async {
        guard target != current else { return }
        guard ExpenseCategory.isKnown(target) else {
            // Unreachable from the picker, which is built from the same list.
            // Kept because "unreachable" is a claim about today's UI, and the
            // ledger refuses an unknown option rather than creating it.
            categoryEdits[recordID] = .failed(message: "「\(target)」不是账本里的分类")
            return
        }
        categoryEdits[recordID] = .inFlight(target: target)
        defer { busy = false }
        busy = true
        do {
            let receipt = try await timeline.updateExpenseCategory(
                recordID: recordID,
                category: target,
                expectedCurrentCategory: current
            )
            liveReceipt = receipt
            if case .recorded(_, _, let record) = receipt.outcome, let record {
                // Verified against the ledger by the server. Only now.
                currentRecords[recordID] = record
                categoryEdits[recordID] = nil
            } else {
                // Settled as something other than a proven write — parked,
                // indeterminate, failed safe. The row keeps the ledger's value
                // and the card says the correction did not take.
                categoryEdits[recordID] = .failed(
                    message: "分类未修改：\(Self.categoryEditReason(receipt.outcome))"
                )
            }
            try await timeline.syncNewer()
            await mirror()
            lastError = nil
        } catch {
            categoryEdits[recordID] = .failed(message: describe(error))
            lastError = describe(error)
        }
        await mirrorPendingSlots()
    }

    /// Why a settled correction did not become a proven change.
    ///
    /// Derived from the structured outcome, never from prose: this string sits
    /// directly under a ledger value, and a model sentence has no business
    /// explaining what did or did not reach the ledger.
    private static func categoryEditReason(_ outcome: OperationOutcome) -> String {
        switch outcome {
        case .failedSafe(let reason):
            return reason ?? "服务端安全拒绝，账本未改动"
        case .needsManualReview:
            return "结果待人工核对，请打开账本确认"
        case .indeterminate:
            return "本客户端无法判定结果，请打开账本确认"
        case .cancelledBeforeSubmit:
            return "已取消，账本未改动"
        case .running:
            return "服务端仍在处理"
        case .needsClarification, .needsDuplicateDecision, .answered,
             .answeredWithQuery, .answeredWithCalendarQuery, .answeredV2, .recorded,
             .calendarEventWritten:
            // None of these are reachable for this route — it dispatches one
            // governed update and never a model turn — but the switch stays
            // exhaustive so a new outcome fails to compile here rather than
            // silently rendering as an empty explanation.
            return "服务端未确认这次修改"
        }
    }

    // --- overriding a duplicate calendar write ---------------------------------

    /// The answers to 「仍要创建」 this client had to ask for, by operation id.
    ///
    /// Populated only for calendar receipts whose own frozen fields are silent —
    /// a Timeline event appended before the server projected `device_result` and
    /// `device_action_id`. Every receipt this build produces decides the question
    /// by itself, so this map is empty in normal use and never consulted for a
    /// live reply.
    ///
    /// Filled by `refreshOverrideDecisions()` once per page load, not per card
    /// and not per redraw: a scroll through a year of calendar cards must not
    /// become one request per row.
    private(set) var overrideDecisions: [String: OverrideDecision] = [:]

    /// Answer 「仍要创建」 for a calendar event the phone found already there.
    ///
    /// The action id comes from the server's projection, never from this
    /// client's own reading of the receipt — the server derives the override's
    /// idempotency key from it, which is what makes a double tap, a retry and a
    /// concurrent pair converge on **one** created event instead of three. The
    /// wait afterwards is `ChatTimeline`'s, and it carries no slot: the decision
    /// is durable on the server, and a lost reply leaves the operation settleable
    /// rather than repeatable.
    ///
    /// A refusal is reported as-is. The endpoint refuses for reasons the user
    /// cannot see from the card — the operation was not a duplicate, it is not
    /// settled, nothing was retained to resume — and inventing reassurance for
    /// those would be the client claiming a decision the server did not make.
    func overrideDeviceAction(actionID: String) async {
        guard !acceptanceStopping else { return }
        acceptanceWork += 1
        defer { acceptanceWork -= 1 }
        busy = true
        defer { busy = false }
        do {
            _ = try await timeline.overrideDeviceAction(actionID: actionID)
            try await timeline.syncNewer()
            await mirror()
            lastError = nil
        } catch {
            lastError = describe(error)
        }
        await mirrorPendingSlots()
    }

    /// What 「仍要创建」 may be answered on one card: the card's own answer when
    /// its fields give one, and this map's when they do not.
    ///
    /// The `??` is the whole policy. An entry exists only where the card asked a
    /// question it could not answer, so a missing entry means the card decided
    /// for itself — never "the lookup failed", which stores `.notOffered`.
    func overrideDecision(
        for outcome: OperationOutcome, operationID: String?
    ) -> OverrideDecision {
        guard let operationID, let asked = overrideDecisions[operationID] else {
            return outcome.overrideDecision
        }
        return asked
    }

    /// Ask the server about every calendar receipt on screen whose own fields
    /// cannot say whether 「仍要创建」 belongs.
    ///
    /// Called after a page load, where "on screen" is "in the Timeline as
    /// loaded" and the count is bounded by how many undecided calendar receipts
    /// that page contains — normally the oldest few, and none at all once the
    /// history in question was written by this build. Answers are kept, so a
    /// later page load re-asks about nothing already answered.
    ///
    /// Deliberately not an error path: `ChatTimeline.overrideDecision` fails
    /// closed to `.notOffered`, and a banner about a button that is not there
    /// would report a problem the user cannot act on. The *tap* is where a
    /// refusal matters, and `overrideDeviceAction` surfaces that one.
    func refreshOverrideDecisions() async {
        guard !acceptanceStopping else { return }
        acceptanceWork += 1
        defer { acceptanceWork -= 1 }
        for (operationID, outcome) in await timeline.undecidedOverrideCandidates()
        where overrideDecisions[operationID] == nil {
            overrideDecisions[operationID] = await timeline.overrideDecision(
                frozen: outcome, operationID: operationID
            )
        }
    }

    // --- the manual-review resolution (`DEV-040`) ------------------------------

    /// Report what the user found in the ledger for a parked
    /// `needs_manual_review` operation.
    ///
    /// The local map is written only after the server accepts, and from the
    /// resolution the server echoed rather than the one that was tapped — so a
    /// replay of an earlier, different answer shows what is actually recorded
    /// instead of what this tap asked for. A refusal (`409` on a contradicting
    /// answer) leaves the card exactly as it was and says why.
    func resolveManualReview(
        operationID: String, resolution: ManualResolution
    ) async {
        guard !acceptanceStopping else { return }
        acceptanceWork += 1
        defer { acceptanceWork -= 1 }
        busy = true
        defer { busy = false }
        do {
            let receipt = try await timeline.resolveManualReview(
                operationID: operationID, resolution: resolution
            )
            resolvedManualReviews[receipt.operationID] = receipt.resolution
            try await timeline.syncNewer()
            await mirror()
            lastError = nil
        } catch {
            lastError = describe(error)
        }
        // The resolution may have released the durable send slot, which is what
        // unblocks the composer.
        await mirrorPendingSlots()
    }

    /// Forget an unconfirmed decision locally. Deliberately not automatic: the
    /// server may already have recorded it, so the check id is shown first and
    /// the user decides.
    func discardDecision(checkID: String) async {
        guard !acceptanceStopping else { return }
        acceptanceWork += 1
        defer { acceptanceWork -= 1 }
        do {
            try await timeline.discardDecision(checkID: checkID)
            lastError = nil
        } catch {
            lastError = describe(error)
        }
        await mirrorPendingSlots()
    }

    /// Re-read both durable slots and show exactly what they say.
    ///
    /// A malformed record is reported, never swallowed. `ChatTimeline` raises
    /// `pendingSendMalformed` / `pendingDecisionsMalformed` precisely so a
    /// record that may hold a write cannot be dropped; reading them with `try?`
    /// would turn that back into `nil`, which the screen renders as "nothing
    /// pending" — the exact lie those errors exist to prevent. On failure the
    /// previous value is left standing, because a corrupt read is not evidence
    /// that a banner shown a moment ago was wrong.
    private func mirrorPendingSlots() async {
        do {
            unresolved = try await timeline.pendingSend()
            hasPendingPhotoSend = try loadPendingPhotoSend() != nil
        } catch {
            report(error)
        }
        do {
            let decisions = try await timeline.pendingDecisions()
            pendingDecisions = Dictionary(
                uniqueKeysWithValues: decisions.map { ($0.checkID, $0) }
            )
        } catch {
            report(error)
        }
    }

    /// Surface a follow-up failure without overwriting the primary one: an error
    /// already on screen explains the action the user took, and these slot reads
    /// run after it.
    private func report(_ error: Error) {
        if lastError == nil { lastError = describe(error) }
    }

    private func mirror(refreshLegacyOverrides: Bool = true) async {
        events = await timeline.events
        hasOlder = await timeline.hasOlder
        // Same merge rule, same reason as the duplicate map below: the card that
        // offered the buttons and the marker that closes it are different
        // events, and a rebuild from `events` alone would re-offer a conclusion
        // the server has already recorded — where the second tap is either a
        // no-op or, if the user changes their mind, a `409` the screen would
        // have invited. The rule itself is stated once, in the Kit, because the
        // acceptance tests have to ask the screen's own question.
        resolvedManualReviews.merge(
            ManualReviewResolutionIndex.answered(by: events)
        ) { _, newest in newest }
        // The query gate is read here rather than at the card, for the same
        // reason everything else on screen is re-derived here: one funnel, one
        // statement of what this screen currently knows.
        calendarUnsynced = await onReadCalendarUnsynced?() ?? false
        // Merge, never replace. `decideDuplicate` records its own choice as soon
        // as the server accepts it, but the permanent marker is only projected
        // here once it is inside the loaded window. Rebuilding this map from
        // `events` alone would drop that entry and offer both buttons again for
        // a check the server has already resolved — and by then the local
        // decision slot is gone, so the second tap mints a fresh key the server
        // refuses. The failure is closed; the prompt would still be a lie.
        // `open(conversationID:)` is the only place allowed to forget.
        for event in events {
            if case .duplicateDecision(let checkID, let decision) = event.kind {
                resolvedDuplicateDecisions[checkID] = decision
            }
            // (The manual-review map is folded below rather than in this loop:
            // it is the same merge rule, and it is the one fold whose rule the
            // acceptance tests also have to ask — see
            // `ManualReviewResolutionIndex`.)
            if case .expenseCategoryCorrected(let recordID, let record) = event.kind {
                currentRecords[recordID] = record
            }
            // `G1`. The newest row wins, and `events` is in Timeline order, so a
            // later category correction overwrites the original write's row.
            // This is how the *original* receipt, scrolled back to, shows the
            // corrected category: the two operations are different, the ledger
            // row is the same, and the card follows the row.
            //
            // A correction that is not itself a proven write never lands in
            // either projection: an operation result must be `.recorded`, and a
            // category marker is emitted only after the verified row is sealed.
            // A failed edit therefore cannot repaint a card as though it took.
            if case .operationResult(let outcome, _, _) = event.kind,
               case .recorded(let recordID, _, let record) = outcome,
               let record {
                currentRecords[recordID] = record
            }
            // `1j`. Same merge rule and the same reason: `events` is in Timeline
            // order, so the last `daily_review` event for a `review_id` wins the
            // map, and the screen collapses any older snapshot for that day.
            if case .dailyReview(let snapshot) = event.kind {
                latestDailyReviewEventID[snapshot.reviewID] = event.eventID
            }
        }
        // Last, because it is the only part of this that touches the network and
        // it must not delay anything above. Self-limiting rather than
        // rate-limited: it asks only about receipts with no entry yet, so the
        // steady state makes no requests at all, whichever call site ran this.
        if refreshLegacyOverrides {
            await refreshOverrideDecisions()
        }
    }

    /// `1j`. Whether a `daily_review` event has been superseded by a newer
    /// snapshot for the same `review_id` (a late-verified write reopened the
    /// card). The older event stays in the sealed archive; the screen collapses
    /// it to a marker rather than drawing the same review twice.
    func isSupersededDailyReview(_ event: TimelineEvent) -> Bool {
        guard case .dailyReview(let snapshot) = event.kind else { return false }
        return latestDailyReviewEventID[snapshot.reviewID] != event.eventID
    }

    /// A live receipt stays visible until the same state has arrived as a
    /// Timeline event. This covers a successful decision followed by a failed
    /// sync without duplicating the card once the permanent event is present.
    func hasMirroredReceipt(_ receipt: OperationReceipt) -> Bool {
        events.contains { event in
            event.operationID == receipt.operationID
                && event.content["state"]?.stringValue == receipt.state.wire
        }
    }

    /// Keep the archive intact, but replace obsolete running projections on
    /// screen when the same operation has a newer server receipt.
    var visibleEvents: [TimelineEvent] {
        var newest: [String: String] = [:]
        for event in events {
            if case .operationResult = event.kind, let id = event.operationID {
                newest[id] = event.eventID
            }
        }
        return events.filter { event in
            guard case .operationResult(let outcome, _, _) = event.kind,
                  !outcome.isSettled, let id = event.operationID else { return true }
            if newest[id] != event.eventID { return false }
            if let liveReceipt, liveReceipt.operationID == id,
               liveReceipt.outcome.isSettled { return false }
            return true
        }
    }
}
