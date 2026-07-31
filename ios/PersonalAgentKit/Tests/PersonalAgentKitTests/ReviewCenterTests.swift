import Foundation
import Testing

@testable import PersonalAgentKit

/// `DEV-031`'s failure set for the daily-review surface, designed against the
/// task's boundaries before the implementation.
///
/// The cases are the ways this screen could lie about the ledger:
///
/// - an item whose values cannot be read must stay on the card with its reason
///   (the count is the truth about what was written);
/// - a status this build does not know must stay visible as itself — reading it
///   as "pending" would ask Henson to redo a review, dropping it would hide one;
/// - ack and defer update the list from the **server's reply**, so a refused
///   defer leaves the stale state visible rather than pretending it worked;
/// - a malformed body is an error, never an empty list that looks like a quiet
///   day with no writes.
///
/// The counterparty is the **real** `AgentClient` and `DeviceSession` over the
/// shared stubbed-HTTP harness from `ChatTimelineTests.swift`. Only HTTP is fake.
@Suite("The DEV-031 daily review surface", .serialized)
struct ReviewCenterTests {
    private func newService() -> Service { Service() }

    private func makeReview(
        service: Service,
        store: CredentialStore = InMemoryCredentialStore()
    ) async throws -> ReviewCenter {
        let session = try makeChatSession(service: service, store: store)
        _ = try await session.enroll(code: "code", displayName: "iPhone")
        return ReviewCenter(backend: session)
    }

    private func summaryBody(
        _ id: String,
        date: String = "2026-07-29",
        status: String = "pending",
        count: Int = 2,
        reviewedAt: Any = NSNull()
    ) -> [String: Any] {
        [
            "review_id": id,
            "review_date": date,
            "status": status,
            "item_count": count,
            "created_at": "2026-07-30T00:00:05+00:00",
            "reviewed_at": reviewedAt,
        ]
    }

    private func itemBody(
        _ recordID: String,
        tool: String = "finance.log_expense",
        values: Any = NSNull(),
        unreadable: Any = NSNull(),
        unavailable: Any = NSNull(),
        tableKind: Any = "expense"
    ) -> [String: Any] {
        var body: [String: Any] = [
            "record_id": recordID,
            "tool": tool,
            "committed_at": "2026-07-29T12:00:00+00:00",
        ]
        if !(tableKind is NSNull) { body["table_kind"] = tableKind }
        if !(values is NSNull) { body["values"] = values }
        if !(unreadable is NSNull) { body["unreadable_fields"] = unreadable }
        if !(unavailable is NSNull) { body["unavailable"] = unavailable }
        return body
    }

    // --- the list --------------------------------------------------------------

    @Test("the list keeps the server's order and a status this build does not know")
    func listKeepsOrderAndUnknownStatus() async throws {
        let service = newService()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("GET", "/v1/daily-reviews"):
                return .ok([
                    "reviews": [
                        summaryBody("rev-2", date: "2026-07-29"),
                        summaryBody("rev-1", date: "2026-07-28", status: "frozen"),
                    ]
                ])
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let center = try await makeReview(service: service)

        let summaries = try await center.loadList()

        #expect(summaries.map(\.reviewID) == ["rev-2", "rev-1"])
        #expect(summaries[1].status == .unrecognised("frozen"))
        #expect(summaries[1].status.wire == "frozen")
    }

    @Test("a status this build does not know is visible but read-only")
    func unknownStatusIsReadOnly() async throws {
        let service = newService()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("GET", "/v1/daily-reviews"):
                return .ok([
                    "reviews": [summaryBody("rev-frozen", status: "frozen")]
                ])
            default:
                return .error(500, "SHOULD_NOT_BE_CALLED")
            }
        }
        let center = try await makeReview(service: service)
        _ = try await center.loadList()

        await #expect(
            throws: ReviewCenterError.unrecognisedStatus("frozen")
        ) {
            _ = try await center.ack(reviewID: "rev-frozen")
        }
        #expect(service.calls("POST", "/v1/daily-reviews/rev-frozen/ack").isEmpty)
        #expect(!ReviewStatus.unrecognised("future").allowsReviewActions)
        #expect(ReviewStatus.pending.allowsReviewActions)
        #expect(ReviewStatus.deferred.allowsReviewActions)
    }

    @Test("a list filter travels as the server's own vocabulary")
    func listFilterTravels() async throws {
        let service = newService()
        service.answer { call, _ in
            call.path == "/v1/daily-reviews" ? .ok(["reviews": []]) : .error(404, "NOT_FOUND")
        }
        let center = try await makeReview(service: service)

        _ = try await center.loadList(status: .pending)

        let call = try #require(service.calls("GET", "/v1/daily-reviews").first)
        #expect(call.query["status"] == "pending")
    }

    @Test("a summary missing its id is an error, never an empty list")
    func malformedSummaryIsRefused() async throws {
        let service = newService()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("GET", "/v1/daily-reviews"):
                return .ok([
                    "reviews": [[
                        "review_date": "2026-07-29",
                        "status": "pending",
                        "item_count": 2,
                        "created_at": "2026-07-30T00:00:05+00:00",
                    ]]
                ])
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let center = try await makeReview(service: service)

        await #expect(throws: AgentClientError.malformedResponse) {
            _ = try await center.loadList()
        }
        #expect(service.count("GET", "/v1/daily-reviews") == 1)
    }

    // --- the opened card ---------------------------------------------------------

    @Test("an unreadable item stays on the card with its reason")
    func unavailableItemStaysVisible() async throws {
        let service = newService()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("GET", "/v1/daily-reviews/rev-1"):
                var body = summaryBody("rev-1")
                body["items"] = [
                    itemBody(
                        "rec-1",
                        values: ["名称": "咖啡", "金额": 18.0, "归属": "个人"],
                        unreadable: ["分类"]
                    ),
                    itemBody(
                        "rec-2",
                        unavailable: "source_unavailable"
                    ),
                ]
                return .ok(body)
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let center = try await makeReview(service: service)

        let detail = try await center.open(reviewID: "rev-1")

        #expect(detail.items.count == 2)
        let readable = try #require(detail.items.first)
        #expect(readable.values?["金额"] == .number(18.0))
        #expect(readable.values?["名称"] == .string("咖啡"))
        #expect(readable.unreadableFields == ["分类"])
        #expect(readable.unavailable == nil)
        let unreadableItem = try #require(detail.items.dropFirst().first)
        #expect(unreadableItem.unavailable == "source_unavailable")
        #expect(unreadableItem.values == nil)
    }

    @Test("an item for a tool the server does not know says so, without a table kind")
    func unknownToolItem() async throws {
        let service = newService()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("GET", "/v1/daily-reviews/rev-1"):
                var body = summaryBody("rev-1", count: 1)
                body["items"] = [
                    itemBody("rec-9", tool: "finance.teleport", unavailable: "unknown_tool", tableKind: NSNull())
                ]
                return .ok(body)
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let center = try await makeReview(service: service)

        let detail = try await center.open(reviewID: "rev-1")

        let item = try #require(detail.items.first)
        #expect(item.tool == "finance.teleport")
        #expect(item.tableKind == nil)
        #expect(item.unavailable == "unknown_tool")
    }

    @Test("reopening replaces the card, because the values are live reads")
    func reopenReplacesCard() async throws {
        let service = newService()
        service.answer { call, seen in
            switch (call.method, call.path) {
            case ("GET", "/v1/daily-reviews/rev-1"):
                var body = summaryBody("rev-1", count: 1)
                // The second open reads the ledger again — this is Henson fixing
                // a value on the computer between the two opens.
                let amount = seen == 0 ? 18.0 : 20.0
                body["items"] = [itemBody("rec-1", values: ["金额": amount])]
                return .ok(body)
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let center = try await makeReview(service: service)

        _ = try await center.open(reviewID: "rev-1")
        let detail = try await center.open(reviewID: "rev-1")

        #expect(detail.items.first?.values?["金额"] == .number(20.0))
        #expect(service.count("GET", "/v1/daily-reviews/rev-1") == 2)
    }

    // --- ack and defer: the server's reply is the state ---------------------------

    @Test("ack updates the list from the server's reply, not from the tap")
    func ackAdoptsServerReply() async throws {
        let service = newService()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("GET", "/v1/daily-reviews"):
                return .ok(["reviews": [summaryBody("rev-1")]])
            case ("POST", "/v1/daily-reviews/rev-1/ack"):
                return .ok(
                    summaryBody("rev-1", status: "reviewed", reviewedAt: "2026-07-30T08:00:00+00:00")
                )
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let center = try await makeReview(service: service)
        _ = try await center.loadList()

        let summary = try await center.ack(reviewID: "rev-1")

        #expect(summary.status == .reviewed)
        #expect(summary.reviewedAt == "2026-07-30T08:00:00+00:00")
        #expect(await center.summaries.first?.status == .reviewed)
        #expect(service.count("POST", "/v1/daily-reviews/rev-1/ack") == 1)
    }

    @Test("a refused defer changes nothing locally and says why")
    func refusedDeferKeepsState() async throws {
        let service = newService()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("GET", "/v1/daily-reviews"):
                return .ok(["reviews": [summaryBody("rev-1")]])
            case ("POST", "/v1/daily-reviews/rev-1/defer"):
                // The card was reviewed elsewhere since the list loaded.
                return .error(400, "INVALID_ARGUMENT")
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let center = try await makeReview(service: service)
        _ = try await center.loadList()

        await #expect(throws: AgentClientError.badRequest(code: "INVALID_ARGUMENT")) {
            _ = try await center.deferCard(reviewID: "rev-1")
        }
        // No local pretending: the stale pending stays until a reload tells the
        // truth.
        #expect(await center.summaries.first?.status == .pending)
    }

    @Test("defer updates the list from the server's reply")
    func deferAdoptsServerReply() async throws {
        let service = newService()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("GET", "/v1/daily-reviews"):
                return .ok(["reviews": [summaryBody("rev-1")]])
            case ("POST", "/v1/daily-reviews/rev-1/defer"):
                return .ok(summaryBody("rev-1", status: "deferred"))
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let center = try await makeReview(service: service)
        _ = try await center.loadList()

        let summary = try await center.deferCard(reviewID: "rev-1")

        #expect(summary.status == .deferred)
        #expect(await center.summaries.first?.status == .deferred)
    }

    @Test("ack and defer for one card cannot cross the same async boundary")
    func mutationsAreSerialised() async throws {
        let backend = BlockingReviewBackend()
        let center = ReviewCenter(backend: backend)
        let ack = Task { try await center.ack(reviewID: "rev-1") }
        await backend.waitUntilAckStarted()

        await #expect(
            throws: ReviewCenterError.mutationInProgress("rev-1")
        ) {
            _ = try await center.deferCard(reviewID: "rev-1")
        }
        await backend.releaseAck()
        #expect(try await ack.value.status == .reviewed)
        #expect(await backend.deferCalls == 0)
    }

    // --- presentation -------------------------------------------------------------

    @Test("ledger values render as themselves")
    func valueDisplayText() {
        #expect(JSONScalar.number(18.0).displayText == "18")
        #expect(JSONScalar.number(18.5).displayText == "18.5")
        #expect(JSONScalar.string("咖啡").displayText == "咖啡")
        #expect(JSONScalar.bool(true).displayText == "true")
        #expect(JSONScalar.null.displayText == "—")
        #expect(JSONScalar.unsupported.displayText == "（本客户端无法显示的字段值）")
    }

    @Test("a ledger number too large for Int renders instead of trapping")
    func valueDisplayTextSurvivesOutOfRangeNumbers() {
        // `1e300` is integral, so the previous `Int(value)` was reached and
        // *trapped* — one out-of-range field crashed the whole card, which is
        // the opposite of "an unreadable row stays on the card". These render
        // as themselves rather than as an integer.
        #expect(JSONScalar.number(1e300).displayText == "1e+300")
        #expect(JSONScalar.number(-1e300).displayText == "-1e+300")
        // Exactly on the Int64 boundary: 2^63 is not representable, 2^63 - 1024
        // is the largest Double below it that is.
        #expect(JSONScalar.number(9_223_372_036_854_775_808.0).displayText.isEmpty == false)
        #expect(JSONScalar.number(9_223_372_036_854_774_784.0).displayText
            == "9223372036854774784")
        // Still exact for the values a ledger actually holds.
        #expect(JSONScalar.number(-18.0).displayText == "-18")
        #expect(JSONScalar.number(0.0).displayText == "0")
    }

    @Test("review item identity includes the ledger table")
    func reviewItemIdentityIsTableScoped() {
        let expense = ReviewItem(
            recordID: "recSame",
            tool: "finance.log_expense",
            committedAt: "2026-07-29T12:00:00+00:00",
            tableKind: "expense",
            values: nil,
            unreadableFields: [],
            unavailable: nil
        )
        let income = ReviewItem(
            recordID: "recSame",
            tool: "finance.log_income",
            committedAt: "2026-07-29T12:01:00+00:00",
            tableKind: "income",
            values: nil,
            unreadableFields: [],
            unavailable: nil
        )

        #expect(expense.id != income.id)
    }
}

private actor BlockingReviewBackend: ReviewBackend {
    private var ackStarted = false
    private var ackContinuation: CheckedContinuation<Void, Never>?
    private(set) var deferCalls = 0

    func dailyReviews(status: String?) async throws -> ReviewListResponse {
        ReviewListResponse(reviews: [])
    }

    func dailyReview(reviewID: String) async throws -> ReviewDetail {
        ReviewDetail(summary: summary(.pending), items: [])
    }

    func ackReview(reviewID: String) async throws -> ReviewSummary {
        ackStarted = true
        await withCheckedContinuation { continuation in
            ackContinuation = continuation
        }
        return summary(.reviewed)
    }

    func deferReview(reviewID: String) async throws -> ReviewSummary {
        deferCalls += 1
        return summary(.deferred)
    }

    func waitUntilAckStarted() async {
        while !ackStarted {
            await Task.yield()
        }
    }

    func releaseAck() {
        ackContinuation?.resume()
        ackContinuation = nil
    }

    private func summary(_ status: ReviewStatus) -> ReviewSummary {
        ReviewSummary(
            reviewID: "rev-1",
            reviewDate: "2026-07-29",
            status: status,
            itemCount: 1,
            createdAt: "2026-07-30T00:00:05+00:00",
            reviewedAt: status == .reviewed ? "2026-07-30T08:00:00+00:00" : nil
        )
    }
}
