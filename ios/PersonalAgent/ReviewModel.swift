import Foundation
import Observation
import PersonalAgentKit

/// The view state for `DEV-031`'s review surface.
///
/// As with the chat, everything that could be wrong is in `PersonalAgentKit`
/// and tested with `swift test`; this type only mirrors it for SwiftUI. The one
/// judgement it makes is about the ledger jump: the URL comes from
/// `/v1/capabilities` or from nowhere, and only an `https` one is offered —
/// the phone opens whatever it is handed, so a scheme downgrade is refused
/// here even though the server already validates it.
@MainActor
@Observable
final class ReviewModel {
    /// Newest day first, exactly as the server ordered it.
    var summaries: [ReviewSummary] = []
    /// The card currently on screen, presented as a sheet.
    var openedDetail: OpenedReview?
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
        guard let raw = capabilities.ledgerURL,
              let url = URL(string: raw), url.scheme == "https"
        else {
            ledgerURL = nil
            return
        }
        ledgerURL = url
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

    func open(reviewID: String) async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        do {
            let detail = try await center.open(reviewID: reviewID)
            openedDetail = OpenedReview(detail: detail)
            summaries = await center.summaries
            lastError = nil
        } catch {
            lastError = describe(error)
        }
    }

    /// 确认都正确. Marks the card as looked at — never a ledger change.
    func ack() async {
        guard let reviewID = openedDetail?.detail.summary.reviewID else { return }
        guard !busy else { return }
        busy = true
        defer { busy = false }
        do {
            let summary = try await center.ack(reviewID: reviewID)
            replaceOpened(summary: summary)
            summaries = await center.summaries
            lastError = nil
        } catch {
            lastError = describe(error)
        }
    }

    /// 稍后处理. A refusal (the card was already reviewed) is shown as the
    /// error it is; the stale card stays until the next load tells the truth.
    func deferCard() async {
        guard let reviewID = openedDetail?.detail.summary.reviewID else { return }
        guard !busy else { return }
        busy = true
        defer { busy = false }
        do {
            let summary = try await center.deferCard(reviewID: reviewID)
            replaceOpened(summary: summary)
            summaries = await center.summaries
            lastError = nil
        } catch {
            lastError = describe(error)
        }
    }

    func closeDetail() {
        openedDetail = nil
    }

    private func replaceOpened(summary: ReviewSummary) {
        guard let opened = openedDetail, opened.detail.summary.reviewID == summary.reviewID
        else { return }
        openedDetail = OpenedReview(
            detail: ReviewDetail(summary: summary, items: opened.detail.items)
        )
    }
}

/// `Identifiable` wrapper so the detail presents as a sheet keyed by the card
/// id rather than by value equality.
struct OpenedReview: Identifiable {
    let detail: ReviewDetail
    var id: String { detail.summary.reviewID }
}
