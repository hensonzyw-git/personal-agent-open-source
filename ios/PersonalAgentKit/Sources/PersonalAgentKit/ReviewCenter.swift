import Foundation

/// The review surface of `DEV-031`, kept out of SwiftUI so it can be tested
/// headlessly with `swift test`.
///
/// Unlike the chat there is no client-side state machine to protect here: ack
/// and defer are idempotent state transitions the server owns, and a retried
/// call returns the same state. The one rule this type enforces is therefore
/// about *whose* state is shown:
///
/// **the list updates from the server's reply, never from the client's
/// assumption.** A tap on 确认都正确 does not mark the card reviewed; the
/// server's returned summary does. A refused defer (the card was already
/// reviewed elsewhere) propagates as an error and leaves the stale state
/// visible for what it is: stale, and due a reload.
public actor ReviewCenter {
    /// Newest day first, exactly as the server ordered the list.
    public private(set) var summaries: [ReviewSummary] = []
    /// The currently opened card, if any.
    public private(set) var opened: ReviewDetail?

    private let backend: any ReviewBackend

    public init(backend: any ReviewBackend) {
        self.backend = backend
    }

    /// Load the card list. `status` is the server's filter vocabulary; passing
    /// `nil` lists everything.
    @discardableResult
    public func loadList(status: ReviewStatus? = nil) async throws -> [ReviewSummary] {
        let response = try await backend.dailyReviews(status: status?.wire)
        summaries = response.reviews
        return summaries
    }

    /// Open one card. The values come back live from the ledger, so reopening
    /// is the only refresh this surface needs or has.
    @discardableResult
    public func open(reviewID: String) async throws -> ReviewDetail {
        let detail = try await backend.dailyReview(reviewID: reviewID)
        opened = detail
        adopt(detail.summary)
        return detail
    }

    /// Mark the card as looked at. Free of ledger effects (design 7.7 step 6).
    @discardableResult
    public func ack(reviewID: String) async throws -> ReviewSummary {
        let summary = try await backend.ackReview(reviewID: reviewID)
        adopt(summary)
        if opened?.summary.reviewID == reviewID {
            opened = ReviewDetail(summary: summary, items: opened?.items ?? [])
        }
        return summary
    }

    /// Push the card back for later. A `400` (the card was already reviewed) is
    /// the server's answer and propagates; the local state is left untouched so
    /// a reload shows the truth rather than a wish.
    @discardableResult
    public func deferCard(reviewID: String) async throws -> ReviewSummary {
        let summary = try await backend.deferReview(reviewID: reviewID)
        adopt(summary)
        if opened?.summary.reviewID == reviewID {
            opened = ReviewDetail(summary: summary, items: opened?.items ?? [])
        }
        return summary
    }

    /// Replace the matching list entry with the server's own summary, or drop
    /// it from view when a filter no longer includes it. The server orders the
    /// list; a status change does not reorder days.
    private func adopt(_ summary: ReviewSummary) {
        if let index = summaries.firstIndex(where: { $0.reviewID == summary.reviewID }) {
            summaries[index] = summary
        }
    }
}

/// What `ReviewCenter` needs from the network.
///
/// A protocol for the same reason as `ChatBackend`: the token policy stays in
/// `DeviceSession`, and the tests conform the **real** session while stubbing
/// only HTTP, so the real decoding and the real refresh policy stay in the loop.
public protocol ReviewBackend: Sendable {
    func dailyReviews(status: String?) async throws -> ReviewListResponse
    func dailyReview(reviewID: String) async throws -> ReviewDetail
    func ackReview(reviewID: String) async throws -> ReviewSummary
    func deferReview(reviewID: String) async throws -> ReviewSummary
}
