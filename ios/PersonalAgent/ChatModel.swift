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
    /// The receipt of the message in hand, live or just settled.
    var liveReceipt: OperationReceipt?
    /// Set while a parked clarification is being answered, so the next send
    /// carries `clarification_of` instead of starting an unrelated message.
    var answering: (operationID: String, question: String)?
    /// An unresolved message this launch inherited. Shown rather than hidden: it
    /// may hold a write nobody has confirmed yet.
    var unresolved: ChatTimeline.PendingSend?
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
        busy = true
        defer { busy = false }
        do {
            try await timeline.loadLatest()
            await mirror()
            // Resuming comes *after* history so the receipt lands under the
            // message it belongs to rather than above an empty screen.
            if let receipt = try await timeline.resume() {
                liveReceipt = receipt
                try await timeline.syncNewer()
                await mirror()
            }
            lastError = nil
        } catch {
            lastError = describe(error)
        }
        unresolved = try? await timeline.pendingSend()
    }

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
    }

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
        unresolved = try? await timeline.pendingSend()
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
        unresolved = try? await timeline.pendingSend()
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
        unresolved = try? await timeline.pendingSend()
    }

    /// Abandon an unresolved message locally. Deliberately separate from
    /// `resumeUnresolved`, and deliberately not automatic: the operation may hold a
    /// write, so the id is shown first and the user decides.
    func discardUnresolved() async {
        do {
            try await timeline.discardPending()
            unresolved = nil
            lastError = nil
        } catch {
            lastError = describe(error)
        }
    }

    func beginAnswering(operationID: String, question: String) {
        answering = (operationID, question)
    }

    func cancelAnswering() {
        answering = nil
    }

    private func mirror() async {
        events = await timeline.events
        hasOlder = await timeline.hasOlder
    }
}
