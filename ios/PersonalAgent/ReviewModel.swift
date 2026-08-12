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

    /// Which card is being opened, and since when.
    ///
    /// Opening a card re-reads every record's *current* Feishu values, so it is a
    /// genuine multi-round-trip wait — several seconds is normal, not a fault. What
    /// was a fault is that the tap produced no visible response at all, which is
    /// §3.5's named failure: slow and stuck looked identical.
    ///
    /// The start time is carried rather than a plain flag because §3.5 rejects a
    /// looping spinner as the liveness signal — it animates just as happily when the
    /// app has hung. Elapsed time cannot: it stops when the work stops.
    struct Opening: Equatable {
        let reviewID: String
        let since: Date
    }
    private(set) var opening: Opening?

    /// Which card's open failed, and why. Kept per-row rather than as a global
    /// error (3f): the failure belongs to the row that was tapped, not to the
    /// whole list, and the row stays visible — showing 写入时的值 is still better
    /// than pretending there is nothing to look at.
    struct OpenFailed: Equatable {
        let reviewID: String
        let message: String
    }
    private(set) var openFailed: OpenFailed?

    /// The in-flight open, so 取消 can cancel the actual network read rather than
    /// only hiding the liveness line. `URLSession.data(for:)` responds to Task
    /// cancellation, so a cancel propagates into the request.
    private var openTask: Task<Void, Never>?

    /// A monotonically increasing tag so a cancelled open's `defer` cannot clear
    /// the state of the open that replaced it. Without this, tapping a second row
    /// while the first is in flight leaves a race: the first's `defer` runs after
    /// the second has already set `opening`/`openTask`, and wipes them both.
    private var openGeneration = 0

    /// §3f: open one card. A second tap on another row cancels the first open and
    /// starts the new one — 活性归属于被点的那一行, and 其余行完全正常可点击. A tap on
    /// the row already opening is a no-op (its own liveness line already shows).
    func open(reviewID: String) {
        guard opening?.reviewID != reviewID else { return }
        openTask?.cancel()
        openGeneration += 1
        let generation = openGeneration
        openTask = Task { await performOpen(reviewID: reviewID, generation: generation) }
    }

    /// §3f: abandon the in-flight read. The underlying `URLSession` request is
    /// cancelled by `Task.cancel()`, so this is a real abort, not a UI smoke-screen.
    func cancelOpen() {
        openTask?.cancel()
        openTask = nil
        opening = nil
        openFailed = nil
    }

    /// §3f: 重试 the failed row — a fresh open of the same card.
    func retryOpen() {
        guard let failed = openFailed else { return }
        openFailed = nil
        open(reviewID: failed.reviewID)
    }

    private func performOpen(reviewID: String, generation: Int) async {
        opening = Opening(reviewID: reviewID, since: Date())
        openFailed = nil
        defer {
            // Only the latest generation owns the cleanup. An older open's defer
            // must not wipe the state of the one that superseded it.
            if generation == openGeneration {
                openTask = nil
                opening = nil
            }
        }
        do {
            let detail = try await center.open(reviewID: reviewID)
            // A cancel that landed after the read but before we set the sheet must
            // not still present a card the user had already walked away from.
            guard !Task.isCancelled else { return }
            openedDetail = OpenedReview(detail: detail)
            summaries = await center.summaries
            lastError = nil
        } catch {
            // A user-initiated cancel is not an error to show. The row simply
            // returns to normal.
            if Task.isCancelled { return }
            openFailed = OpenFailed(reviewID: reviewID, message: describe(error))
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
