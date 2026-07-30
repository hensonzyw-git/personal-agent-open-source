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
enum IdempotencyKey {
    static func mint() -> String {
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
    case recorded(recordID: String, tool: String?)
    /// A no-side-effect answer.
    case answered(String)
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
             .failedSafe, .cancelledBeforeSubmit:
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
        answer: String?
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
    }

    /// The tools whose success is a ledger row. Kept here so `succeeded` for one
    /// of them without a `record_id` fails closed instead of being displayed as a
    /// recorded expense. It mirrors the server's `_RECORD_ID_RESULT_TOOLS`; the
    /// client uses it only to *refuse*, never to grant.
    public static let recordEvidenceTools: Set<String> = [
        "finance.log_expense",
        "finance.log_income",
        "finance.update_family_fund",
    ]

    public var outcome: OperationOutcome {
        Self.project(
            state: state,
            tool: tool,
            recordID: recordID,
            failureReason: failureReason,
            duplicateCheckID: duplicateCheckID,
            clarification: clarification,
            duplicateExisting: duplicateExisting,
            answer: answer
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
    static func project(
        state: OperationState,
        tool: String?,
        recordID: String?,
        failureReason: String?,
        duplicateCheckID: String?,
        clarification: String?,
        duplicateExisting: String?,
        answer: String?
    ) -> OperationOutcome {
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
                return .recorded(recordID: recordID, tool: tool)
            }
            if let tool, Self.recordEvidenceTools.contains(tool) {
                // A governed write that succeeded must carry its external
                // evidence. Anything else is unknown, not a recorded expense.
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

/// What one Timeline entry is, as far as the UI is concerned.
public enum TimelineEntryKind: Sendable, Equatable {
    case userMessage(text: String, clarificationOf: String?)
    /// The structured receipt as it was persisted. Projected by the same code as
    /// a live receipt.
    case operationResult(outcome: OperationOutcome, state: OperationState)
    /// A Session boundary. Presentation only — never dialogue, never an
    /// instruction, and the server's fixed wording is a `reason` code.
    case sessionDivider(reason: String?, corrected: Bool)
    /// `DEV-031`. The permanent marker that closes an earlier duplicate prompt.
    /// It is presentation state and never model dialogue.
    case duplicateDecision(checkID: String, decision: String)
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
    public let content: [String: JSONScalar]

    public var id: String { eventID }

    public init(
        eventID: String,
        eventType: String,
        operationID: String?,
        createdAt: String,
        content: [String: JSONScalar]
    ) {
        self.eventID = eventID
        self.eventType = eventType
        self.operationID = operationID
        self.createdAt = createdAt
        self.content = content
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
            return .operationResult(
                outcome: OperationReceipt.project(
                    state: state,
                    // The persisted event carries no `tool`; evidence is the
                    // `record_id`, and its absence stays indeterminate.
                    tool: nil,
                    recordID: content["record_id"]?.stringValue,
                    failureReason: content["failure_reason"]?.stringValue,
                    duplicateCheckID: content["duplicate_check_id"]?.stringValue,
                    clarification: content["clarification"]?.stringValue,
                    duplicateExisting: content["duplicate_existing"]?.stringValue,
                    answer: content["answer"]?.stringValue
                ),
                state: state
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
                [String: JSONScalar].self, forKey: .content
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
