import Foundation

/// The daily-review wire types of `DEV-031`: the card list and the opened card
/// of technical design 5.3 and 7.7.
///
/// The two rules are the server's own, restated for the client:
///
/// - **an unreadable row stays on the card.** An item marked `unavailable` is
///   decoded and rendered with its reason; dropping it would make the count lie
///   about what was written, and failing the whole card would hide four good
///   rows because of one.
/// - **ack and defer never touch the ledger.** They move a status the server
///   owns, so the client updates its list only from the server's reply, never
///   from its own assumption about what the call must have done.
///
/// A card's `values` are the record's *current* ledger fields, read live when
/// the card was opened. The client renders them as given and never reconciles
/// them against anything: Feishu is the fact source, this screen is a window.

// --- status ------------------------------------------------------------------

/// The review card states the server can report.
///
/// `unrecognised` exists for the same reason as `OperationState.unrecognised`:
/// a status this build does not know must stay visible as itself, not read as
/// "pending" (it might be reviewed) or be dropped from the list (a card that
/// vanishes is a review that never happened).
public enum ReviewStatus: Sendable, Equatable {
    case pending
    case reviewed
    case deferred
    case unrecognised(String)

    public init(wire: String) {
        switch wire {
        case "pending": self = .pending
        case "reviewed": self = .reviewed
        case "deferred": self = .deferred
        default: self = .unrecognised(wire)
        }
    }

    public var wire: String {
        switch self {
        case .pending: return "pending"
        case .reviewed: return "reviewed"
        case .deferred: return "deferred"
        case .unrecognised(let raw): return raw
        }
    }

    /// Only states whose transition semantics this build knows may expose ack
    /// or defer. A future status stays visible but read-only: treating it as
    /// pending would defeat the point of preserving it as `unrecognised`.
    public var allowsReviewActions: Bool {
        switch self {
        case .pending, .deferred: return true
        case .reviewed, .unrecognised: return false
        }
    }
}

// --- summaries ---------------------------------------------------------------

/// One card in the list. Cheap and local: no ledger read stands behind it.
public struct ReviewSummary: Sendable, Equatable, Identifiable {
    public let reviewID: String
    public let reviewDate: String
    public let status: ReviewStatus
    public let itemCount: Int
    public let createdAt: String
    public let reviewedAt: String?

    public var id: String { reviewID }

    public init(
        reviewID: String,
        reviewDate: String,
        status: ReviewStatus,
        itemCount: Int,
        createdAt: String,
        reviewedAt: String?
    ) {
        self.reviewID = reviewID
        self.reviewDate = reviewDate
        self.status = status
        self.itemCount = itemCount
        self.createdAt = createdAt
        self.reviewedAt = reviewedAt
    }
}

extension ReviewSummary: Decodable {
    private enum CodingKeys: String, CodingKey {
        case reviewID = "review_id"
        case reviewDate = "review_date"
        case status
        case itemCount = "item_count"
        case createdAt = "created_at"
        case reviewedAt = "reviewed_at"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        // `review_id`, `review_date`, `status` and `item_count` are required: a
        // body without them is not this contract, and defaulting a status is how
        // a reviewed card comes back asking to be reviewed.
        reviewID = try container.decode(String.self, forKey: .reviewID)
        reviewDate = try container.decode(String.self, forKey: .reviewDate)
        status = ReviewStatus(wire: try container.decode(String.self, forKey: .status))
        itemCount = try container.decode(Int.self, forKey: .itemCount)
        createdAt = try container.decode(String.self, forKey: .createdAt)
        reviewedAt = try container.decodeIfPresent(String.self, forKey: .reviewedAt)
    }
}

public struct ReviewListResponse: Sendable, Equatable, Decodable {
    public let reviews: [ReviewSummary]
}

// --- the opened card ---------------------------------------------------------

/// One row of an opened card: a record pointer plus its current ledger values,
/// or the reason they could not be read.
public struct ReviewItem: Sendable, Equatable, Identifiable {
    public let recordID: String
    public let tool: String
    public let committedAt: String
    /// The ledger table kind (`expense` / `income` / `family_fund`), when the
    /// server knows the tool. Absent together with `unavailable` otherwise.
    public let tableKind: String?
    /// Current field values, exactly as read from the ledger when the card
    /// opened. Absent when the record could not be read.
    public let values: [String: JSONScalar]?
    /// Configured fields Finance could not parse. Shown as such, never quietly
    /// dropped: a missing field and an unreadable one are not the same thing.
    public let unreadableFields: [String]
    /// Why this row has no values (`unknown_tool` / `source_unavailable` /
    /// `no_receipt`). The row stays on the card either way.
    public let unavailable: String?

    /// Feishu record ids are table-scoped. The server deliberately keeps an
    /// expense and an income with the same `record_id` as two review items, so
    /// SwiftUI identity must carry the same table dimension.
    public var id: String { "\(tableKind ?? tool)\u{1f}\(recordID)" }

    public init(
        recordID: String,
        tool: String,
        committedAt: String,
        tableKind: String?,
        values: [String: JSONScalar]?,
        unreadableFields: [String],
        unavailable: String?
    ) {
        self.recordID = recordID
        self.tool = tool
        self.committedAt = committedAt
        self.tableKind = tableKind
        self.values = values
        self.unreadableFields = unreadableFields
        self.unavailable = unavailable
    }
}

extension ReviewItem: Decodable {
    private enum CodingKeys: String, CodingKey {
        case recordID = "record_id"
        case tool
        case committedAt = "committed_at"
        case tableKind = "table_kind"
        case values
        case unreadableFields = "unreadable_fields"
        case unavailable
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        recordID = try container.decode(String.self, forKey: .recordID)
        tool = try container.decode(String.self, forKey: .tool)
        committedAt = try container.decode(String.self, forKey: .committedAt)
        tableKind = try container.decodeIfPresent(String.self, forKey: .tableKind)
        values = try container.decodeIfPresent([String: JSONScalar].self, forKey: .values)
        unreadableFields =
            try container.decodeIfPresent([String].self, forKey: .unreadableFields) ?? []
        unavailable = try container.decodeIfPresent(String.self, forKey: .unavailable)
    }
}

/// The opened card: the summary plus every item's current values.
public struct ReviewDetail: Sendable, Equatable {
    public let summary: ReviewSummary
    public let items: [ReviewItem]

    public init(summary: ReviewSummary, items: [ReviewItem]) {
        self.summary = summary
        self.items = items
    }
}

extension ReviewDetail: Decodable {
    private enum CodingKeys: String, CodingKey {
        case items
    }

    public init(from decoder: Decoder) throws {
        // The detail body is the summary's fields plus `items`, so the summary
        // half decodes from the same payload rather than duplicating its keys.
        summary = try ReviewSummary(from: decoder)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        items = try container.decodeIfPresent([ReviewItem].self, forKey: .items) ?? []
    }
}

// --- 待办 counting (§3h 第 4 条) ----------------------------------------------

/// How many review cards are still awaiting Henson.
///
/// `reviewed` is the system's conclusion and counts never. `pending` obviously
/// counts, and so does `unrecognised`: a status this build cannot interpret is
/// precisely the case a person should look at, and quietly leaving it out of the
/// count would hide it.
///
/// **`deferred` is a 贪睡, not an answer (§3h 第 4 条).** On the day the card was
/// written it does not count — that is what 稍后处理 means, and counting it would
/// make the indicator unable to reach zero. But a deferred card is not finished
/// work, and it must not disappear silently; at the next 0:00 — that is, once the
/// card's `review_date` is behind today — it is counted again, exactly as the
/// day's review would have re-included it.
///
/// The client cannot know *when* a card was deferred (the server sets no
/// timestamp on defer), so `review_date` is the honest anchor: the card belongs
/// to that day, and 贪睡 lasts until that day ends.
public enum ReviewPendingCount {
    /// Count with the real current date as the 贪睡 anchor.
    public static func count(_ summaries: [ReviewSummary]) -> Int {
        count(summaries, today: today())
    }

    /// Count against an explicit date, so tests can pin the anchor.
    public static func count(
        _ summaries: [ReviewSummary],
        today: String
    ) -> Int {
        summaries.filter { summary in
            switch summary.status {
            case .pending, .unrecognised:
                return true
            case .reviewed:
                return false
            case .deferred:
                // §3h 第 4 条: 贪睡到 review_date 当天结束；次日重新计入。
                return summary.reviewDate < today
            }
        }.count
    }

    /// Today's date in the same `YYYY-MM-DD` form the server's `review_date` uses,
    /// so the `<` comparison above is a lexicographic order over ISO dates.
    private static func today() -> String {
        // The server's `review_date` is an Asia/Shanghai calendar date — the
        // daily-review job runs at 00:05 Asia/Shanghai (`DEV-028`). The 贪睡
        // boundary must be evaluated against that same timezone, not the device's
        // local one, or a device in another timezone compares against the wrong
        // midnight and the deferred card re-enters (or fails to re-enter) a day
        // early or late.
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        let components = calendar.dateComponents(
            [.year, .month, .day], from: Date()
        )
        return String(
            format: "%04d-%02d-%02d",
            components.year ?? 0,
            components.month ?? 0,
            components.day ?? 0
        )
    }
}

// --- presentation helpers ----------------------------------------------------

extension JSONScalar {
    /// What one ledger value looks like on the card. Raw and honest: an
    /// integral number renders without a trailing `.0`, anything else renders
    /// as itself, and an unsupported shape says so rather than vanishing.
    ///
    /// The integral case goes through `Int(exactly:)`, never `Int(_:)`. A
    /// `Double` beyond `Int64` still satisfies `truncatingRemainder(...) == 0`,
    /// and the unlabelled initialiser *traps* on it — a whole card would crash
    /// on one out-of-range ledger field, which is the opposite of this file's
    /// rule that an unreadable row stays on the card.
    public var displayText: String {
        switch self {
        case .string(let value): return value
        case .number(let value):
            if let integral = Int(exactly: value) { return String(integral) }
            return String(value)
        case .bool(let value): return value ? "true" : "false"
        case .null: return "—"
        case .unsupported: return "（本客户端无法显示的字段值）"
        }
    }
}
