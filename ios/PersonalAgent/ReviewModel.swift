import Foundation
import Observation
import PersonalAgentKit

/// The view state for the daily-review surface, now rendered as a card inside
/// the Timeline (design `1j`) rather than a separate page.
///
/// The frozen values live in the sealed `daily_review` Timeline event; this type
/// owns only the parts that keep changing -- the status and the ack/defer calls.
/// As with the chat, everything that could be wrong is in `PersonalAgentKit` and
/// tested with `swift test`; this type only mirrors it for SwiftUI. The one
/// judgement it makes is about the ledger jump: the URL comes from
/// `/v1/capabilities` or from nowhere, and only an `https` one is offered --
/// the phone opens whatever it is handed, so a scheme downgrade is refused
/// here even though the server already validates it.
@MainActor
@Observable
final class ReviewModel {
    /// Newest day first, exactly as the server ordered it.
    var summaries: [ReviewSummary] = []
    var lastError: String?
    var busy = false
    /// Where 打开飞书账本 goes. `nil` is an honest answer too: the service
    /// named no ledger, and the button then says so instead of opening
    /// anything.
    private(set) var ledgerURL: URL?

    private let center: ReviewCenter
    private let describe: @MainActor (Error) -> String

    init(
        center: ReviewCenter,
        ledgerURL: URL?,
        describe: @escaping @MainActor (Error) -> String
    ) {
        self.center = center
        self.ledgerURL = ledgerURL
        self.describe = describe
    }

    /// Re-validate the service-named URL on each refresh. A value that stops
    /// being a valid `https` URL retracts the jump rather than keeping the last
    /// good one: stale and missing are both "the service names no ledger now".
    func updateLedgerURL(from capabilities: Capabilities) {
        ledgerURL = capabilities.validatedLedgerURL
    }

    func load() async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        do {
            _ = try await center.loadList()
            summaries = await center.summaries
            lastError = nil
        } catch {
            lastError = describe(error)
        }
    }

    /// The live status of one card, by its `review_id`. The Timeline event's
    /// snapshot is frozen; this is the one value the card must not freeze,
    /// because ack/defer keep changing it after the snapshot was sealed.
    func summary(for reviewID: String) -> ReviewSummary? {
        summaries.first { $0.reviewID == reviewID }
    }

    /// 确认都正确. Marks the card as looked at -- never a ledger change.
    func ack(reviewID: String) async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        do {
            _ = try await center.ack(reviewID: reviewID)
            summaries = await center.summaries
            lastError = nil
        } catch {
            lastError = describe(error)
        }
    }

    /// 稍后处理. A refusal (the card was already reviewed) is shown as the
    /// error it is; the stale status stays until the next load tells the truth.
    func deferCard(reviewID: String) async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        do {
            _ = try await center.deferCard(reviewID: reviewID)
            summaries = await center.summaries
            lastError = nil
        } catch {
            lastError = describe(error)
        }
    }
}
