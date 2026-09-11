import Foundation

/// The chat wire types of `DEV-030`: the operation projection of technical design
/// 5.2 and the Timeline page of `CAP-001` design 14.
///
/// One rule shapes this whole file, and it is the task's own constraint: **the
/// client never reverse-parses success out of natural language.** Whether an
/// expense was recorded is read from `state` plus an external `record_id`, and
/// from nothing else. A model sentence saying 已记录 next to `failed_safe` is
/// rendered as a failure; a `succeeded` with no evidence this client can name is
/// rendered as *unknown*, never as a write.
///
/// The second rule is the server's own: an unreadable answer is never an empty
/// one. An unknown `state` decodes into `unrecognised` and is projected as
/// indeterminate — it stops the poll loop and says so, rather than polling
/// forever or quietly reading as "not succeeded, so failed".

// --- idempotency keys --------------------------------------------------------

/// The only place this client mints an `Idempotency-Key`.
///
/// The server requires the **canonical** RFC 4122 form and compares the header
/// against `str(uuid.UUID(key))`, which is lower-case. Foundation's
/// `UUID.uuidString` is upper-case, so every write this client sent was refused
/// with `INVALID_ARGUMENT` — chat messages and duplicate decisions alike. The
/// offline suite could not see it: the stub accepted any key, because it was
/// written from the same assumption as the code it was checking.
///
/// The server is right to be strict. If it accepted both cases, the same logical
/// key in two spellings would become two rows, and one expense would be recorded
/// twice — the exact failure the idempotency key exists to prevent. So the fix
/// belongs here, and it is a single named mint rather than a `.lowercased()` at
/// each call site: adapting at the call site leaves the next write path to
/// rediscover this by being refused in production.
public enum IdempotencyKey {
    public static func mint() -> String {
        UUID().uuidString.lowercased()
    }

    /// Canonical lower-case UUIDv4, as the server parses it. Used by the tests to
    /// hold the stub to the real server's contract.
    static func isCanonical(_ key: String) -> Bool {
        guard let parsed = UUID(uuidString: key) else { return false }
        // `UUID(uuidString:)` accepts either case, so the round-trip is what
        // actually pins the spelling.
        guard parsed.uuidString.lowercased() == key else { return false }
        // Version 4, RFC 4122 variant: byte 6 high nibble is 4, byte 8 is 10xx.
        let bytes = withUnsafeBytes(of: parsed.uuid) { Array($0) }
        return bytes[6] >> 4 == 4 && bytes[8] >> 6 == 0b10
    }
}

// --- operation state ---------------------------------------------------------

/// The client-facing operation states of design 5.2.
///
/// `unrecognised` exists because the two ways of handling an unknown state are
/// both worse: refusing to decode loses the `operation_id` the user needs to see,
/// and silently mapping it onto an in-flight state polls until the battery dies.
public enum OperationState: Sendable, Equatable {
    case accepted
    case interpreting
    case waitingForClarification
    case dispatching
    case waitingForDuplicateDecision
    case sourceInProgress
    case verifying
    case succeeded
    case failedSafe
    case needsManualReview
    case cancelledPreSubmit
    /// A state this build does not know. Never treated as success or as failure.
    case unrecognised(String)

    public init(wire: String) {
        switch wire {
        case "accepted": self = .accepted
        case "interpreting": self = .interpreting
        case "waiting_for_clarification": self = .waitingForClarification
        case "dispatching": self = .dispatching
        case "waiting_for_duplicate_decision": self = .waitingForDuplicateDecision
        case "source_in_progress": self = .sourceInProgress
        case "verifying": self = .verifying
        case "succeeded": self = .succeeded
        case "failed_safe": self = .failedSafe
        case "needs_manual_review": self = .needsManualReview
        case "cancelled_pre_submit": self = .cancelledPreSubmit
        default: self = .unrecognised(wire)
        }
    }

    public var wire: String {
        switch self {
        case .accepted: return "accepted"
        case .interpreting: return "interpreting"
        case .waitingForClarification: return "waiting_for_clarification"
        case .dispatching: return "dispatching"
        case .waitingForDuplicateDecision: return "waiting_for_duplicate_decision"
        case .sourceInProgress: return "source_in_progress"
        case .verifying: return "verifying"
        case .succeeded: return "succeeded"
        case .failedSafe: return "failed_safe"
        case .needsManualReview: return "needs_manual_review"
        case .cancelledPreSubmit: return "cancelled_pre_submit"
        case .unrecognised(let raw): return raw
        }
    }

    // Whether an operation is still worth polling is deliberately **not** decided
    // here. `OperationOutcome.isSettled` is the single answer, because two
    // definitions of "still running" drift, and the one that drifts wrong either
    // polls a parked operation forever or stops watching a live write.
}

// --- the Finance query result ------------------------------------------------

/// The ledger's 分类 single-select options, as the picker may offer them.
///
/// Hard-coded here and held against the server by
/// `chat_receipt_vectors.json`'s `expense_categories`, exactly as the query and
/// record evidence tool sets are. The connector never *creates* a select option
/// (design 9.2), so an option this client invented would not appear in the
/// ledger -- it would be a refused write. Reading them from the server at
/// runtime instead would mean a picker that is empty until some other request
/// succeeds, and this list changes about once a year.
public enum ExpenseCategory {
    public static let all: [String] = [
        "出行", "餐饮", "游戏", "日常生活", "玩乐", "购物", "旅行", "房租",
    ]

    /// Whether a value is one this build may send. Used to refuse before the
    /// network rather than to let the server refuse -- the round trip would be
    /// a governed write attempt for a value that was never valid.
    public static func isKnown(_ value: String) -> Bool {
        all.contains(value)
    }
}

/// The written ledger row a governed write's receipt carries (`G1`).
///
/// Until `chat_receipt_projection_v5` the receipt carried a `record_id` and no
/// business fields at all, which is why `ChatView.receiptFields` returned an
/// empty array and every write rendered as the lightest status row. This is the
/// object that fills it.
///
/// Two rules the decoder enforces rather than trusts:
///
/// - **money stays a string.** `amount` and `personalSpend` are the decimal text
///   the ledger stated. Decoding them as `Double` would make ¥0.10 render as
///   ¥0.10000000000000001 on a receipt whose entire job is to be checkable.
/// - **the family flag has no default.** A missing `is_family_expense` refuses
///   the whole record instead of defaulting to `false`, because that default
///   would quietly turn a family expense into a personal one on screen -- the
///   one field where a wrong default is a wrong accounting fact.
///
/// `category` is optional because the write contract allows it: a refund or AA
/// reimbursement may carry none. `personalSpend` is optional because 个人支出 is
/// a Base formula, so it exists only when the ledger had evaluated it.
public struct FinanceExpenseRecord: Sendable, Equatable {
    public let name: String
    /// 原始金额, as the ledger's own decimal string. Negative for a refund.
    public let amount: String
    /// The ledger day, `yyyy-MM-dd`.
    public let occurredOn: String
    public let isFamilyExpense: Bool
    public let category: String?
    /// 个人支出: the Base formula's answer, never computed on this side.
    public let personalSpend: String?
    /// Set once a category correction has been verified against the ledger.
    ///
    /// This is what keeps the card honest under Henson's 2026-08-15 decision
    /// that the card follows the ledger's *current* value: past this point the
    /// card is no longer literally the write receipt, and this timestamp says
    /// so on the card instead of hiding it.
    public let categoryUpdatedAt: String?

    public init(
        name: String,
        amount: String,
        occurredOn: String,
        isFamilyExpense: Bool,
        category: String?,
        personalSpend: String?,
        categoryUpdatedAt: String?
    ) {
        self.name = name
        self.amount = amount
        self.occurredOn = occurredOn
        self.isFamilyExpense = isFamilyExpense
        self.category = category
        self.personalSpend = personalSpend
        self.categoryUpdatedAt = categoryUpdatedAt
    }
}

extension FinanceExpenseRecord: Decodable {
    private enum CodingKeys: String, CodingKey {
        case name
        case amount = "amount_cny"
        case occurredOn = "occurred_on"
        case isFamilyExpense = "is_family_expense"
        case category
        case personalSpend = "personal_spend_cny"
        case categoryUpdatedAt = "category_updated_at"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        name = try container.decode(String.self, forKey: .name)
        amount = try container.decode(String.self, forKey: .amount)
        occurredOn = try container.decode(String.self, forKey: .occurredOn)
        // No `decodeIfPresent ?? false` here, deliberately. See the type's note.
        isFamilyExpense = try container.decode(Bool.self, forKey: .isFamilyExpense)
        category = try container.decodeIfPresent(String.self, forKey: .category)
        personalSpend = try container.decodeIfPresent(
            String.self, forKey: .personalSpend
        )
        categoryUpdatedAt = try container.decodeIfPresent(
            String.self, forKey: .categoryUpdatedAt
        )
        if name.isEmpty || amount.isEmpty || occurredOn.isEmpty {
            throw DecodingError.dataCorruptedError(
                forKey: .name,
                in: container,
                debugDescription: "a receipt record needs a name, amount and date"
            )
        }
        if let category, category.isEmpty {
            // Null means "this row legitimately has no category"; empty string
            // is a server that lost one. They must not collapse.
            throw DecodingError.dataCorruptedError(
                forKey: .category,
                in: container,
                debugDescription: "category is null or a non-empty string"
            )
        }
    }
}

/// The structured `finance.query_expenses` projection, decoded strictly.
///
/// The server projects exactly the whitelisted fields of its query contract
/// into `query_result`; this type reads that object and nothing else. An
/// unknown `view` refuses to decode -- the receipt then has no query result and
/// the screen says this build cannot show it, rather than rendering a JSON
/// string or claiming a success. `amount` is `nil` only for the `records` view.
public struct FinanceQueryResult: Sendable, Equatable {
    public enum View: String, Sendable, Equatable {
        case total
        case byCategory = "by_category"
        case records
    }

    /// One `by_category` bucket.
    public struct CategoryBucket: Sendable, Equatable {
        public let category: String?
        public let amount: String
        public let recordCount: Int
        public let share: String?
    }

    /// One `records` row, bounded by the server's page size -- a page is never
    /// presented as the whole answer, which is what `nextCursor` exists for.
    public struct RecordRow: Sendable, Equatable {
        public let recordID: String
        public let name: String
        public let occurredOn: String?
        public let category: String?
        public let isFamilyExpense: Bool
        public let amount: String
    }

    public let view: View
    public let recordCount: Int
    public let filtersApplied: [String: JSONValue]
    public let sourceSystem: String
    /// `personal_spend_total_cny`; present for `total` and `by_category`.
    public let amount: String?
    public let byCategory: [CategoryBucket]
    public let records: [RecordRow]
    /// Present only when a `records` page has a next page to continue into.
    public let nextCursor: String?
}

extension FinanceQueryResult: Decodable {
    private enum CodingKeys: String, CodingKey {
        case view
        case recordCount = "record_count"
        case filtersApplied = "filters_applied"
        case sourceSystem = "source_system"
        case amount = "personal_spend_total_cny"
        case byCategory = "by_category"
        case records
        case nextCursor = "next_cursor"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let rawView = try container.decode(String.self, forKey: .view)
        guard let view = View(rawValue: rawView) else {
            // A view this build cannot render is not a card. Refusing the whole
            // projection keeps the raw result off the screen.
            throw DecodingError.dataCorruptedError(
                forKey: .view,
                in: container,
                debugDescription: "unknown query view \(rawView)"
            )
        }
        self.view = view
        self.recordCount = try container.decode(Int.self, forKey: .recordCount)
        self.filtersApplied =
            try container.decodeIfPresent(
                [String: JSONValue].self, forKey: .filtersApplied
            ) ?? [:]
        self.sourceSystem =
            try container.decodeIfPresent(String.self, forKey: .sourceSystem) ?? ""
        self.amount = try container.decodeIfPresent(String.self, forKey: .amount)
        self.byCategory =
            try container.decodeIfPresent([CategoryBucket].self, forKey: .byCategory) ?? []
        self.records =
            try container.decodeIfPresent([RecordRow].self, forKey: .records) ?? []
        self.nextCursor = try container.decodeIfPresent(String.self, forKey: .nextCursor)
    }
}

extension FinanceQueryResult.CategoryBucket: Decodable {
    private enum CodingKeys: String, CodingKey {
        case category
        case amount = "personal_spend_total_cny"
        case recordCount = "record_count"
        case share = "share_of_total"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        category = try container.decodeIfPresent(String.self, forKey: .category)
        amount = try container.decode(String.self, forKey: .amount)
        recordCount = try container.decode(Int.self, forKey: .recordCount)
        share = try container.decodeIfPresent(String.self, forKey: .share)
    }
}

extension FinanceQueryResult.RecordRow: Decodable {
    private enum CodingKeys: String, CodingKey {
        case recordID = "record_id"
        case name
        case occurredOn = "occurred_on"
        case category
        case isFamilyExpense = "is_family_expense"
        case amount = "personal_spend_cny"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        recordID = try container.decode(String.self, forKey: .recordID)
        name = try container.decode(String.self, forKey: .name)
        occurredOn = try container.decodeIfPresent(String.self, forKey: .occurredOn)
        category = try container.decodeIfPresent(String.self, forKey: .category)
        isFamilyExpense = try container.decode(Bool.self, forKey: .isFamilyExpense)
        amount = try container.decode(String.self, forKey: .amount)
    }
}

// --- the receipt -------------------------------------------------------------

/// What the app is allowed to tell the user about one operation.
///
/// Every case is reachable only from structured fields. There is no case built
/// from prose, and `recorded` is the only case that claims a ledger row exists.
public enum OperationOutcome: Sendable, Equatable {
    /// The server is still working. Keep polling.
    case running
    /// Parked on a question. Answering it is a *new* message carrying
    /// `clarification_of`; the client does not resume this operation.
    case needsClarification(question: String?)
    /// Parked on a possible duplicate. The decision surface is `DEV-031`.
    case needsDuplicateDecision(checkID: String, existing: String?)
    /// A ledger row exists, and this is its external evidence.
    ///
    /// `record` is the row's business fields when the server projected them
    /// (`G1`), and `nil` when it could not -- an `idempotent_replay`, an older
    /// receipt, or a payload that failed projection. The card degrades to its
    /// status row in that case; the write is still proven by `recordID`, which
    /// is why the fields are allowed to be absent at all.
    case recorded(recordID: String, tool: String?, record: FinanceExpenseRecord?)
    /// A no-side-effect answer.
    case answered(String)
    /// A structured Finance query result, read-only, rendered as a card rather
    /// than as prose. `tool` is the recorded query tool, shown on the card.
    case answeredWithQuery(result: FinanceQueryResult, tool: String?)
    /// Nothing was written.
    case failedSafe(reason: String?)
    /// Something may have been written and could not be verified. Never shown as
    /// success and never shown as a clean failure.
    case needsManualReview(reason: String?, recordID: String?)
    /// Cancelled before any external submit could have happened.
    case cancelledBeforeSubmit
    /// The server reported success but gave this client nothing it can present as
    /// evidence, or reported a state this build does not know. Fail closed.
    case indeterminate(state: String)

    /// Whether the client should stop polling. A parked operation is settled: it
    /// waits for the user, not for the server.
    public var isSettled: Bool {
        if case .running = self { return false }
        return true
    }

    /// Whether it is safe to forget the durable idempotency slot.
    ///
    /// This is deliberately not the same question as `isSettled`. An unknown
    /// state or `needs_manual_review` should stop automatic polling, but either
    /// may still represent a committed write. Keeping the slot blocks a second
    /// key for the same intent until the user verifies and explicitly discards it.
    public var releasesPendingSlot: Bool {
        switch self {
        case .running, .needsManualReview, .indeterminate:
            return false
        case .needsClarification, .needsDuplicateDecision, .recorded, .answered,
             .answeredWithQuery, .failedSafe, .cancelledBeforeSubmit:
            return true
        }
    }

    /// True only where an external ledger row is proven to exist.
    public var provesWrite: Bool {
        if case .recorded = self { return true }
        return false
    }
}

/// How a cancel request must be presented.
///
/// Design 5.2 keeps `cancel_requested` a flag and the accounting result a state,
/// precisely so "the app disconnected" can never be projected as a rollback. This
/// enum is that rule in the client: only `cancelledPreSubmit` may say nothing was
/// written.
public enum CancellationNote: Sendable, Equatable {
    case none
    /// Cancel (or a detached client) was recorded while the write could already be
    /// in flight. The outcome is still whatever the server proves it to be.
    case requestedOutcomeStillAuthoritative
    case cancelledBeforeSubmit
}

public struct OperationReceipt: Sendable, Equatable {
    public let operationID: String
    public let state: OperationState
    public let cancelRequested: Bool
    public let clientDetached: Bool
    public let tool: String?
    public let recordID: String?
    public let failureReason: String?
    public let duplicateCheckID: String?
    /// Transient fields: the server returns them on the immediate reply only.
    public let clarification: String?
    public let duplicateExisting: String?
    public let answer: String?
    /// The structured `finance.query_expenses` projection, when the tool was a
    /// query and the result decoded. `nil` for every other tool.
    public let queryResult: FinanceQueryResult?
    /// The written ledger row, when this was a governed write the server could
    /// project (`G1`). `nil` for every other tool and for a replay.
    public let record: FinanceExpenseRecord?

    public init(
        operationID: String,
        state: OperationState,
        cancelRequested: Bool,
        clientDetached: Bool,
        tool: String?,
        recordID: String?,
        failureReason: String?,
        duplicateCheckID: String?,
        clarification: String?,
        duplicateExisting: String?,
        answer: String?,
        queryResult: FinanceQueryResult? = nil,
        record: FinanceExpenseRecord? = nil
    ) {
        self.operationID = operationID
        self.state = state
        self.cancelRequested = cancelRequested
        self.clientDetached = clientDetached
        self.tool = tool
        self.recordID = recordID
        self.failureReason = failureReason
        self.duplicateCheckID = duplicateCheckID
        self.clarification = clarification
        self.duplicateExisting = duplicateExisting
        self.answer = answer
        self.queryResult = queryResult
        self.record = record
    }

    /// The tools whose success is a ledger row. Kept here so `succeeded` for one
    /// of them without a `record_id` fails closed instead of being displayed as a
    /// recorded expense. It mirrors the server's `_RECORD_ID_RESULT_TOOLS`; the
    /// client uses it only to *refuse*, never to grant.
    ///
    /// The server derives its set from the IR's `risk_level == "R2"`, so this
    /// list carries every governed write the IR declares, including one that is
    /// not enabled yet (`finance.log_expense_batch`). Listing a disabled tool
    /// costs nothing — the client only ever refuses with it — while omitting it
    /// would mean the day it ships, an unproven batch write renders as a clean
    /// receipt. `chat_receipt_vectors.json` is what holds the two sides equal.
    public static let recordEvidenceTools: Set<String> = [
        "finance.log_expense",
        "finance.log_expense_batch",
        "finance.log_income",
        "finance.update_family_fund",
        // `G1`. A category correction is an R2 write like the rest, so a
        // `succeeded` for it without a `record_id` is refused here too. It
        // matters more than for a create, not less: the row it claims to have
        // changed already existed, so "succeeded" with no evidence would read as
        // a correction that landed on a row nobody can point at.
        "finance.update_expense_category",
    ]

    /// The governed read tools whose success is a structured query card, never a
    /// prose answer. It mirrors the server's IR-derived `_QUERY_RESULT_TOOLS`;
    /// the client uses it only to decide a `succeeded` must carry a decodable
    /// `query_result`, never to grant anything. `chat_receipt_vectors.json`
    /// (`query_evidence_tools`, v4) holds the two sides equal, so a renamed or
    /// newly shipped governed query fails both suites before it reaches a user.
    public static let queryEvidenceTools: Set<String> = [
        "finance.query_expenses",
    ]

    public var outcome: OperationOutcome {
        Self.project(
            state: state,
            toolEvidence: .known(tool),
            recordID: recordID,
            failureReason: failureReason,
            duplicateCheckID: duplicateCheckID,
            clarification: clarification,
            duplicateExisting: duplicateExisting,
            answer: answer,
            queryResult: queryResult,
            record: record
        )
    }

    public var cancellation: CancellationNote {
        if case .cancelledPreSubmit = state { return .cancelledBeforeSubmit }
        if cancelRequested || clientDetached {
            return .requestedOutcomeStillAuthoritative
        }
        return .none
    }

    /// The one projection. A Timeline `operation_result` event goes through the
    /// same function as a live receipt, so history and the live reply can never
    /// disagree about whether something was written.
    ///
    /// `toolEvidence` is the tool fact as it was actually recorded: the live
    /// receipt always carries it (possibly `null`), while an old Timeline event
    /// may predate tool recording entirely. That distinction is what stops a
    /// missing fact from being read as "no tool was called".
    static func project(
        state: OperationState,
        toolEvidence: ToolEvidence,
        recordID: String?,
        failureReason: String?,
        duplicateCheckID: String?,
        clarification: String?,
        duplicateExisting: String?,
        answer: String?,
        queryResult: FinanceQueryResult? = nil,
        record: FinanceExpenseRecord? = nil
    ) -> OperationOutcome {
        let tool: String?
        if case .known(let value) = toolEvidence { tool = value } else { tool = nil }
        switch state {
        case .accepted, .interpreting, .dispatching, .sourceInProgress, .verifying:
            return .running
        case .waitingForClarification:
            return .needsClarification(question: clarification)
        case .waitingForDuplicateDecision:
            guard let duplicateCheckID, !duplicateCheckID.isEmpty else {
                // Without the check id there is nothing the user could decide,
                // and the entry is not a failure either.
                return .indeterminate(state: state.wire)
            }
            return .needsDuplicateDecision(
                checkID: duplicateCheckID, existing: duplicateExisting
            )
        case .succeeded:
            if let recordID, !recordID.isEmpty {
                return .recorded(recordID: recordID, tool: tool, record: record)
            }
            if let tool, Self.recordEvidenceTools.contains(tool) {
                // A governed write that succeeded must carry its external
                // evidence. Anything else is unknown, not a recorded expense.
                return .indeterminate(state: state.wire)
            }
            if let tool, Self.queryEvidenceTools.contains(tool) {
                if let queryResult {
                    return .answeredWithQuery(result: queryResult, tool: tool)
                }
                // A query that succeeded without a projectable result is not a
                // success this client can present.
                return .indeterminate(state: state.wire)
            }
            // Without tool evidence an `answer` cannot be trusted as a clean
            // direct reply: history recorded before tool recording carried raw
            // query JSON in `answer`. That case is `.unknown`, and it stays
            // unknown rather than being read as "no tool was called".
            if case .unknown = toolEvidence {
                return .indeterminate(state: state.wire)
            }
            if let answer, !answer.isEmpty {
                return .answered(answer)
            }
            return .indeterminate(state: state.wire)
        case .failedSafe:
            return .failedSafe(reason: failureReason)
        case .needsManualReview:
            return .needsManualReview(reason: failureReason, recordID: recordID)
        case .cancelledPreSubmit:
            return .cancelledBeforeSubmit
        case .unrecognised(let raw):
            return .indeterminate(state: raw)
        }
    }
}

extension OperationReceipt: Decodable {
    private enum CodingKeys: String, CodingKey {
        case operationID = "operation_id"
        case state
        case cancelRequested = "cancel_requested"
        case clientDetached = "client_detached"
        case tool
        case recordID = "record_id"
        case failureReason = "failure_reason"
        case duplicateCheckID = "duplicate_check_id"
        case clarification
        case duplicateExisting = "duplicate_existing"
        case answer
        case queryResult = "query_result"
        case record
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        // `operation_id`, `state` and the two flags are required: a body without
        // them is not this contract, and guessing a default for either flag is
        // how a detached client becomes a rollback.
        operationID = try container.decode(String.self, forKey: .operationID)
        state = OperationState(wire: try container.decode(String.self, forKey: .state))
        cancelRequested = try container.decode(Bool.self, forKey: .cancelRequested)
        clientDetached = try container.decode(Bool.self, forKey: .clientDetached)
        tool = try container.decodeIfPresent(String.self, forKey: .tool)
        recordID = try container.decodeIfPresent(String.self, forKey: .recordID)
        failureReason = try container.decodeIfPresent(
            String.self, forKey: .failureReason
        )
        duplicateCheckID = try container.decodeIfPresent(
            String.self, forKey: .duplicateCheckID
        )
        clarification = try container.decodeIfPresent(
            String.self, forKey: .clarification
        )
        duplicateExisting = try container.decodeIfPresent(
            String.self, forKey: .duplicateExisting
        )
        answer = try container.decodeIfPresent(String.self, forKey: .answer)
        // A malformed `query_result` is a query this build cannot render, not a
        // reason to lose the whole receipt: it decodes to `nil` and the screen
        // fails closed on the query card while everything else still works.
        queryResult = try? container.decodeIfPresent(
            FinanceQueryResult.self, forKey: .queryResult
        )
        // Same fail-closed shape as `query_result`, and for a stronger reason: a
        // malformed record is a card this build cannot draw, never a reason to
        // lose the receipt that proves the write. It decodes to `nil` and the
        // card falls back to the status row.
        record = try? container.decodeIfPresent(
            FinanceExpenseRecord.self, forKey: .record
        )
    }
}

// --- the duplicate decision (`DEV-031`) ----------------------------------------

/// The only two resolutions a parked duplicate accepts, per technical design 5.2.
///
/// The wire values are the server's own vocabulary; a case this build does not
/// know can therefore never be sent, and the server refuses anything else anyway.
public enum DuplicateDecision: String, Sendable, Equatable, Codable {
    /// The parked operation ends as `cancelled_pre_submit`. Nothing is written.
    case dismiss
    /// A *new* operation is created carrying the check id as its override
    /// authorisation, and the write happens under it.
    case writeAnyway = "write_anyway"
}

// --- the manual-review resolution (`DEV-040`) ---------------------------------

/// What a person reports having found in the ledger for an operation that ended
/// at `needs_manual_review`.
///
/// The server's vocabulary, so a build that does not know a future value can
/// never send one. It is deliberately **not** a claim about `state`: the server
/// records this beside the state, never instead of it, because a mistaken tap
/// must stay distinguishable from verified evidence forever after. This client
/// keeps the same separation — nothing here can turn a resolution into
/// `OperationOutcome.recorded`.
public enum ManualResolution: String, Sendable, Equatable, Codable {
    /// The row is in the ledger. The write happened; the system simply could not
    /// prove it at the time.
    case confirmedWritten = "confirmed_written"
    /// The row is not in the ledger. Nothing was written, so re-entering it is
    /// safe.
    case confirmedNotWritten = "confirmed_not_written"
}

/// The reply to `POST /v1/operations/{id}/resolution`.
///
/// Deliberately *not* an `OperationReceipt`. The server refused to widen
/// `chat_receipt_projection_v4` for this — a manual resolution is not a chat
/// receipt — and decoding it as one here would quietly re-couple the two
/// contracts from the client side. `state` is carried so the screen can still
/// see what the system proved, and it is never overwritten by `resolution`.
public struct ManualResolutionReceipt: Sendable, Equatable {
    public let operationID: String
    public let state: OperationState
    /// The raw wire value, not `ManualResolution`. A value this build cannot name
    /// is still a resolution that was recorded, and dropping it would render an
    /// answered card as unanswered — which invites a second, contradicting tap
    /// that the server would then refuse.
    public let resolution: String
    public let resolvedAt: String?
    /// False when this call replayed an identical, already-recorded resolution.
    /// Not a failure: it is what a retried tap is supposed to return.
    public let recorded: Bool
}

extension ManualResolutionReceipt: Decodable {
    private enum CodingKeys: String, CodingKey {
        case operationID = "operation_id"
        case state
        case resolution = "manual_resolution"
        case resolvedAt = "manual_resolved_at"
        case recorded
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        operationID = try container.decode(String.self, forKey: .operationID)
        state = OperationState(wire: try container.decode(String.self, forKey: .state))
        // Required, all three. A body without `manual_resolution` is not evidence
        // that anything was recorded, and defaulting `recorded` to either value
        // would make "did this land?" answerable from a body that never said.
        resolution = try container.decode(String.self, forKey: .resolution)
        recorded = try container.decode(Bool.self, forKey: .recorded)
        resolvedAt = try container.decodeIfPresent(String.self, forKey: .resolvedAt)
    }
}

// --- Timeline ----------------------------------------------------------------

/// One JSON scalar from an event's `content`.
///
/// Decoding `content` as `[String: String]` would have thrown on one unexpected
/// number and blanked a whole page of history. This keeps the page readable while
/// still refusing to read a non-string as a string: `string(_:)` returns nil for
/// anything else, so an unexpected shape becomes a missing field, never a value.
public enum JSONScalar: Sendable, Equatable, Decodable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case null
    /// An object or array. Present, but not something this contract reads.
    case unsupported

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else {
            self = .unsupported
        }
    }

    public var stringValue: String? {
        if case .string(let value) = self { return value }
        return nil
    }
}

/// One JSON value from an event's `content`, including nested structures.
///
/// `JSONScalar` above stays for the review surface, whose ledger field values
/// are all scalars. Timeline content needs more: a Finance query projection is
/// a nested object, and a page of records is a nested array. This recursive
/// value keeps those structures intact so history can be decoded back into the
/// same structured result as the live receipt, instead of degrading a nested
/// object to `unsupported` and losing the whole card.
public enum JSONValue: Sendable, Equatable, Codable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case null
    case object([String: JSONValue])
    case array([JSONValue])

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else {
            throw DecodingError.dataCorruptedError(
                in: container, debugDescription: "unsupported JSON value"
            )
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .string(let value): try container.encode(value)
        case .number(let value): try container.encode(value)
        case .bool(let value): try container.encode(value)
        case .null: try container.encodeNil()
        case .object(let value): try container.encode(value)
        case .array(let value): try container.encode(value)
        }
    }

    public var stringValue: String? {
        if case .string(let value) = self { return value }
        return nil
    }

    public var boolValue: Bool? {
        if case .bool(let value) = self { return value }
        return nil
    }

    public var objectValue: [String: JSONValue]? {
        if case .object(let value) = self { return value }
        return nil
    }

    public var arrayValue: [JSONValue]? {
        if case .array(let value) = self { return value }
        return nil
    }
}

/// Whether a Timeline `operation_result` event actually recorded which tool ran.
///
/// The live receipt always carries `tool` (possibly `null` for an explicit
/// no-tool direct answer). History before this change did not record it, and
/// "the event predates tool recording" is a different fact from "the server
/// recorded no tool". Distinguishing them is what keeps 无工具调用 from being
/// claimed for an event that never said.
public enum ToolEvidence: Sendable, Equatable {
    /// The server recorded a tool. `nil` means an explicit no-tool direct answer.
    case known(String?)
    /// The event carries no tool fact; it is unknown, not "no tool".
    case unknown
}

/// What one Timeline entry is, as far as the UI is concerned.
public enum TimelineEntryKind: Sendable, Equatable {
    case userMessage(text: String, clarificationOf: String?)
    /// The structured receipt as it was persisted. Projected by the same code as
    /// a live receipt. `toolEvidence` records whether the persisted event
    /// actually carried a tool fact, so a missing fact can never be read as
    /// "no tool was called".
    case operationResult(
        outcome: OperationOutcome, state: OperationState, toolEvidence: ToolEvidence
    )
    /// A Session boundary. Presentation only — never dialogue, never an
    /// instruction, and the server's fixed wording is a `reason` code.
    case sessionDivider(reason: String?, corrected: Bool)
    /// `DEV-031`. The permanent marker that closes an earlier duplicate prompt.
    /// It is presentation state and never model dialogue.
    case duplicateDecision(checkID: String, decision: String)
    /// `G1`. A verified current-value revision for one expense row. It is a
    /// separate append-only Timeline fact; the original write receipt remains
    /// sealed and is never rewritten.
    case expenseCategoryCorrected(recordID: String, record: FinanceExpenseRecord)
    /// `DEV-040`. The permanent marker recording what a person found in the
    /// ledger for an operation parked at `needs_manual_review`. Presentation
    /// state, never dialogue — and never evidence that a write happened.
    case manualReviewResolved(resolution: String)
    /// `1j`. The daily review card, sealed by the nightly job with the ledger
    /// values read once at build time. The `snapshot` is frozen; the card's
    /// status is read live because ack/defer keep changing it after the seal.
    case dailyReview(snapshot: ReviewCardSnapshot)
    /// The systemic-risk daily card, sealed by the risk-monitor job with the
    /// scores read once at build time. Presentation, never dialogue.
    case riskReport(snapshot: RiskReportSnapshot)
    /// An event type this build does not know. Kept visible rather than dropped:
    /// a silently-missing entry is a history that lies about what happened.
    case unrecognised(eventType: String)
}

public struct TimelineEvent: Sendable, Equatable, Identifiable {
    public let eventID: String
    public let eventType: String
    public let operationID: String?
    /// The server's own timestamp, kept as text. Ordering comes from the server's
    /// page order and never from parsing this.
    public let createdAt: String
    public let content: [String: JSONValue]

    public var id: String { eventID }

    public init(
        eventID: String,
        eventType: String,
        operationID: String?,
        createdAt: String,
        content: [String: JSONValue]
    ) {
        self.eventID = eventID
        self.eventType = eventType
        self.operationID = operationID
        self.createdAt = createdAt
        self.content = content
    }

    public var imageMediaIDs: [String] {
        guard eventType == "user_message" else { return [] }
        return (content["parts"]?.arrayValue ?? []).compactMap { part in
            guard let fields = part.objectValue,
                  fields["type"]?.stringValue == "image_ref",
                  let id = fields["media_id"]?.stringValue,
                  UUID(uuidString: id) != nil else { return nil }
            return id
        }
    }

    public var kind: TimelineEntryKind {
        switch eventType {
        case "user_message":
            // A user message with no readable text is not silently blank: it is
            // an entry this client could not read.
            guard let text = content["text"]?.stringValue else {
                return .unrecognised(eventType: eventType)
            }
            return .userMessage(
                text: text,
                clarificationOf: content["clarification_of"]?.stringValue
            )
        case "operation_result":
            guard let wire = content["state"]?.stringValue else {
                return .unrecognised(eventType: eventType)
            }
            let state = OperationState(wire: wire)
            // Whether the event itself recorded which tool ran. New events carry
            // `tool` (possibly `null` for an explicit no-tool direct answer);
            // old events predate it and stay `.unknown`, which is a different
            // fact from "no tool was called".
            let toolEvidence: ToolEvidence
            if let toolScalar = content["tool"] {
                toolEvidence = .known(toolScalar.stringValue)
            } else {
                toolEvidence = .unknown
            }
            // A structured Finance query projection, when the event carried one.
            // Decoded from the nested object so history renders the same card as
            // the live receipt.
            let queryResult: FinanceQueryResult?
            if let object = content["query_result"]?.objectValue,
               let data = try? JSONEncoder().encode(object) {
                queryResult = try? JSONDecoder().decode(
                    FinanceQueryResult.self, from: data
                )
            } else {
                queryResult = nil
            }
            // `G1`'s business fields, read the same way and for the same
            // reason: scrolling back must draw the same card the live receipt
            // drew, not a demoted one.
            let record: FinanceExpenseRecord?
            if let object = content["record"]?.objectValue,
               let data = try? JSONEncoder().encode(object) {
                record = try? JSONDecoder().decode(
                    FinanceExpenseRecord.self, from: data
                )
            } else {
                record = nil
            }
            return .operationResult(
                outcome: OperationReceipt.project(
                    state: state,
                    toolEvidence: toolEvidence,
                    recordID: content["record_id"]?.stringValue,
                    failureReason: content["failure_reason"]?.stringValue,
                    duplicateCheckID: content["duplicate_check_id"]?.stringValue,
                    clarification: content["clarification"]?.stringValue,
                    duplicateExisting: content["duplicate_existing"]?.stringValue,
                    answer: content["answer"]?.stringValue,
                    queryResult: queryResult,
                    record: record
                ),
                state: state,
                toolEvidence: toolEvidence
            )
        case "duplicate_decision":
            guard
                let checkID = content["duplicate_check_id"]?.stringValue,
                !checkID.isEmpty,
                let decision = content["decision"]?.stringValue,
                !decision.isEmpty
            else {
                return .unrecognised(eventType: eventType)
            }
            return .duplicateDecision(checkID: checkID, decision: decision)
        case "expense_category_corrected":
            guard
                let recordID = content["record_id"]?.stringValue,
                !recordID.isEmpty,
                let object = content["record"]?.objectValue,
                let data = try? JSONEncoder().encode(object),
                let record = try? JSONDecoder().decode(
                    FinanceExpenseRecord.self, from: data
                ),
                record.categoryUpdatedAt != nil
            else {
                return .unrecognised(eventType: eventType)
            }
            return .expenseCategoryCorrected(
                recordID: recordID, record: record
            )
        case "manual_review_resolved":
            guard
                let resolution = content["resolution"]?.stringValue,
                !resolution.isEmpty
            else {
                // An unreadable marker is not a blank one. Rendering it as
                // "resolved" with no conclusion would be worse than saying this
                // client could not read the entry.
                return .unrecognised(eventType: eventType)
            }
            return .manualReviewResolved(resolution: resolution)
        case "daily_review":
            // The whole sealed content is the snapshot. Re-encoding the nested
            // `JSONValue` object and decoding it back is the same path the query
            // and receipt projections use, so history draws the same card the
            // live build carried. A snapshot this build cannot read refuses the
            // entry rather than rendering a partial, possibly-lying card.
            guard
                let data = try? JSONEncoder().encode(content),
                let snapshot = try? JSONDecoder().decode(
                    ReviewCardSnapshot.self, from: data
                )
            else {
                return .unrecognised(eventType: eventType)
            }
            return .dailyReview(snapshot: snapshot)
        case "risk_report":
            guard
                let data = try? JSONEncoder().encode(content),
                let snapshot = try? JSONDecoder().decode(
                    RiskReportSnapshot.self, from: data
                )
            else {
                return .unrecognised(eventType: eventType)
            }
            return .riskReport(snapshot: snapshot)
        case "session_divider", "session_boundary_corrected":
            return .sessionDivider(
                reason: content["reason"]?.stringValue,
                corrected: eventType == "session_boundary_corrected"
            )
        default:
            return .unrecognised(eventType: eventType)
        }
    }
}

extension TimelineEvent: Decodable {
    private enum CodingKeys: String, CodingKey {
        case eventID = "event_id"
        case eventType = "event_type"
        case operationID = "operation_id"
        case createdAt = "created_at"
        case content
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        eventID = try container.decode(String.self, forKey: .eventID)
        eventType = try container.decode(String.self, forKey: .eventType)
        operationID = try container.decodeIfPresent(
            String.self, forKey: .operationID
        )
        createdAt = try container.decode(String.self, forKey: .createdAt)
        content =
            try container.decodeIfPresent(
                [String: JSONValue].self, forKey: .content
            ) ?? [:]
    }
}

/// One page of `GET /v1/conversations/{id}/events`.
///
/// `conversationID` is echoed by the server as the *canonical* Timeline id, which
/// may differ from the alias that was requested. The client follows the server's
/// answer; it never invents or renames a Timeline.
public struct TimelinePageResponse: Sendable, Equatable, Decodable {
    public let conversationID: String
    public let events: [TimelineEvent]
    public let olderCursor: String?
    public let newerCursor: String?
    public let hasOlder: Bool
    public let hasNewer: Bool

    private enum CodingKeys: String, CodingKey {
        case conversationID = "conversation_id"
        case events
        case olderCursor = "older_cursor"
        case newerCursor = "newer_cursor"
        case hasOlder = "has_older"
        case hasNewer = "has_newer"
    }
}

public enum TimelineDirection: String, Sendable {
    case older
    case newer
}
