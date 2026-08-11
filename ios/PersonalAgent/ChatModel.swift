import Foundation
import Observation
import PersonalAgentKit

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
    /// Oldest-to-newest, exactly as the server ordered it.
    var events: [TimelineEvent] = []
    var hasOlder = false
    var draft: String = ""
    /// §1c: what `/v1/capabilities` says this device may do, for the empty state's
    /// 能力清单. Empty until the first successful read -- an unread capability set is
    /// not the same as an empty one, so the empty state omits the section entirely
    /// rather than claiming nothing is available.
    var tools: [Capabilities.Tool] = []
    /// The receipt of the message in hand, live or just settled.
    var liveReceipt: OperationReceipt?
    /// Set while a parked clarification is being answered, so the next send
    /// carries `clarification_of` instead of starting an unrelated message.
    var answering: (operationID: String, question: String)?
    /// An unresolved message this launch inherited. Shown rather than hidden: it
    /// may hold a write nobody has confirmed yet.
    var unresolved: ChatTimeline.PendingSend?
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
    var lastError: String?
    var busy = false
    var loadingOlder = false

    private let timeline: ChatTimeline
    private let describe: @MainActor (Error) -> String

    init(timeline: ChatTimeline, describe: @escaping @MainActor (Error) -> String) {
        self.timeline = timeline
        self.describe = describe
    }

    /// Open the Timeline the server named, then finish anything left unresolved.
    func open(conversationID: String) async {
        await timeline.bind(conversationID: conversationID)
        // `bind` drops the loaded history when the Timeline changes, so the
        // decisions projected out of it are dropped here too. Every later
        // `mirror()` merges rather than replaces, and this is the one place that
        // is allowed to forget.
        resolvedDuplicateDecisions = [:]
        resolvedManualReviews = [:]
        busy = true
        defer { busy = false }
        do {
            try await timeline.loadLatest()
            await mirror()
            // Resuming comes *after* history so the receipt lands under the
            // message it belongs to rather than above an empty screen.
            var needsSync = false
            if let receipt = try await timeline.resume() {
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
    func loadOlder() async {
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
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        busy = true
        defer { busy = false }
        let clarificationOf = answering?.operationID
        do {
            let receipt = try await timeline.send(
                text: text, clarificationOf: clarificationOf
            )
            draft = ""
            answering = nil
            liveReceipt = receipt
            lastError = nil
            // The user message and the result are Timeline events, so the screen
            // reads them back from the server rather than inventing local copies.
            try await timeline.syncNewer()
            await mirror()
        } catch {
            lastError = describe(error)
        }
        await mirrorPendingSlots()
    }

    /// Ask the server to cancel. Past a possible submit this only records the
    /// request; the state the server returns is still the answer.
    func cancelLive() async {
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
        busy = true
        defer { busy = false }
        do {
            liveReceipt = try await timeline.resume()
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

    // --- duplicate decisions (`DEV-031`) ---------------------------------------

    /// Resolve a parked duplicate as the user chose. The decision key lives in
    /// `ChatTimeline`; a lost reply is retried with the same key, so a flaky
    /// network cannot record the choice twice.
    func decideDuplicate(checkID: String, decision: DuplicateDecision) async {
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
        await decideDuplicate(checkID: pending.checkID, decision: pending.decision)
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

    private func mirror() async {
        events = await timeline.events
        hasOlder = await timeline.hasOlder
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
            // Same merge rule, same reason: the card that offered the buttons and
            // the marker that closes it are different events, and a rebuild from
            // `events` alone would re-offer a conclusion the server has already
            // recorded — where the second tap is either a no-op or, if the user
            // changes their mind, a `409` the screen would have invited.
            if case .manualReviewResolved(let resolution) = event.kind,
               let operationID = event.operationID {
                resolvedManualReviews[operationID] = resolution
            }
        }
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
}
