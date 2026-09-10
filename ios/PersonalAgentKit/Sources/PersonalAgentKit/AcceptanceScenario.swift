import Foundation

/// The isolated acceptance build's fixed script: the synthetic facts a person
/// looks at on the phone, and the checklist that says what "pass" means for
/// each one.
///
/// ## What this is for
///
/// The 2026-09-10 review would not release the ordinary App build for
/// installation, because the cards it had changed could only be checked by
/// driving a real conversation against the real backend, with the real calendar
/// on the other end of one of them. The acceptance build removes that
/// dependency: it renders the *real* cards — the same `ChatView`, the same
/// `TimelineEvent` decoding, the same `OperationOutcome` projection — over
/// synthetic facts, so what is being looked at is the shipping rendering code
/// and not a mock of it.
///
/// ## Why it is inert in production
///
/// Two independent facts, and neither alone would be enough:
///
/// - **Nothing here is reachable from the App target's production
///   composition.** The type that builds this scenario into a running app
///   (`AcceptanceScene.swift`) is inside `#if ACCEPTANCE`, and the condition is
///   set by exactly one build configuration, which the production schemes do
///   not use. There is no run-time switch, no launch argument, and no default
///   that a shipped binary could fall into.
/// - **Nothing here touches a real fact source.** No EventKit, no network, no
///   model. The seeded events are `TimelineEvent` values the app's own history
///   decoder reads; the receipts are projections in the server's own JSON
///   shape, decoded by the shipping `OperationReceipt` initialiser.
///
/// The file itself is compiled in every configuration, deliberately: it is pure
/// data and pure logic with no side effects, it is what the acceptance suite
/// tests, and putting it behind the same `#if` would have made the one part of
/// this harness that can be verified the one part that is never compiled.
/// `ios/scripts/check_acceptance_isolation.sh` is what asserts the property
/// that actually matters — that no production product can select it.
///
/// **Why the Kit is the one place `#if ACCEPTANCE` does not appear.** The flag
/// is set on the App target's `Acceptance` build configuration, and a SwiftPM
/// package target does not inherit it. That was measured, not assumed: a
/// `#if ACCEPTANCE` / `#error` pair placed in this directory compiled cleanly
/// under `PersonalAgent-Acceptance`, which is what a block that is false in
/// every build looks like. So gating here would not narrow the acceptance build
/// — it would silently delete the harness from the product that needs it, and
/// the deletion would look exactly like successful isolation.
/// `tests/unit/test_acceptance_build_isolation.py` fails if anyone writes that
/// `#if` here anyway.
///
/// The consequence is that these types, and the seeded script with them, are
/// present in the production binaries too. They are inert there: nothing in the
/// production composition references them (`AcceptanceScene` is inside
/// `#if ACCEPTANCE` and is absent from the production product, verified by
/// symbol), none of them reaches a calendar, a socket, or a credential, and the
/// one file they do write — `AcceptanceBackend`'s seeded archive — is written
/// under the app's own Application Support directory, which is what makes the
/// kill-and-relaunch checklist item work. Compiling them in the App target
/// instead would have removed those bytes and cost the checklist-versus-script
/// tests, which is the invariant most likely to rot; this trade is recorded
/// rather than hidden.
public enum AcceptanceScenario {

    /// The Timeline the harness serves. Not a server id and not derived from
    /// one: nothing in the acceptance build talks to a server, so this value
    /// only has to be a stable name for the client to bind to.
    public static let conversationID = "tl_acceptance00000000000000000001"

    /// The tool the 「仍要创建」 button belongs to. Read from the shipping
    /// constant rather than written out a second time: a receipt this harness
    /// seeded under a renamed tool would stop offering the button, and the
    /// checklist would go red for a reason that has nothing to do with the
    /// button.
    public static let deviceTool = OperationReceipt.calendarDeviceTool

    // --- the seeded Timeline -------------------------------------------------

    /// One synthetic operation, described once and rendered into both shapes the
    /// server would have produced from it: a `TimelineEvent`'s content and an
    /// operation projection. Two shapes for one fact is the server's own
    /// arrangement (`_operation_event_content` beside `_operation_projection`),
    /// and deriving both from one description here is what stops the history
    /// card and the live receipt from describing different writes.
    public struct Operation: Sendable, Equatable {
        public let operationID: String
        public let tool: String?
        public let state: String
        /// The IR domain. `nil` is the honest shape of an operation recorded
        /// before step 5 wrote the field, and it is a *fact about the record*
        /// rather than a gap to fill in.
        public let domain: String?
        public let recordID: String?
        public let failureReason: String?
        public let deviceResult: String?
        public let deviceActionID: String?
        /// The calendar query projection, for the one operation that answered
        /// with a list instead of a write.
        public let queryResult: JSONValue?

        public init(
            operationID: String,
            tool: String?,
            state: String,
            domain: String? = nil,
            recordID: String? = nil,
            failureReason: String? = nil,
            deviceResult: String? = nil,
            deviceActionID: String? = nil,
            queryResult: JSONValue? = nil
        ) {
            self.operationID = operationID
            self.tool = tool
            self.state = state
            self.domain = domain
            self.recordID = recordID
            self.failureReason = failureReason
            self.deviceResult = deviceResult
            self.deviceActionID = deviceActionID
            self.queryResult = queryResult
        }
    }

    /// One entry of the seeded history, oldest first.
    public enum Step: Sendable, Equatable {
        case userMessage(text: String)
        case result(Operation)
        /// A `manual_review_resolved` marker. `domain` is the field the server
        /// froze into the event when it was appended, so `nil` here reproduces a
        /// marker written before step 5 — the case that must render neutrally
        /// rather than borrowing the ledger's words.
        case resolved(operationID: String, resolution: String, domain: String?)
    }

    /// The whole script. Every card the checklist names is produced by one of
    /// these steps, and `AcceptanceChecklist` holds the two together.
    public static let steps: [Step] = [
        .userMessage(text: "帮我在工作日程里加一条全天日程：10-01 至 10-03 东京出差"),
        .result(
            Operation(
                operationID: "op-accept-created",
                tool: deviceTool,
                state: "succeeded",
                domain: OperationReceipt.calendarDomain,
                recordID: "EKA-ACCEPT-0001",
                deviceResult: "created",
                deviceActionID: "act-accept-created"
            )
        ),
        .userMessage(text: "再帮我把同一条日程加一次"),
        .result(
            Operation(
                operationID: "op-accept-duplicate",
                tool: deviceTool,
                state: "succeeded",
                domain: OperationReceipt.calendarDomain,
                recordID: "EKA-ACCEPT-0002",
                deviceResult: "duplicate",
                deviceActionID: "act-accept-duplicate"
            )
        ),
        .userMessage(text: "下周有什么安排？"),
        .result(
            Operation(
                operationID: "op-accept-list",
                tool: "calendar.query_events",
                state: "succeeded",
                domain: OperationReceipt.calendarDomain,
                queryResult: calendarQueryProjection
            )
        ),
        .userMessage(text: "帮我加一条 10-05 上午 10 点的日程"),
        .result(
            Operation(
                operationID: "op-accept-review-calendar",
                tool: deviceTool,
                state: "needs_manual_review",
                domain: OperationReceipt.calendarDomain,
                recordID: "EKA-ACCEPT-0003",
                failureReason: "SOURCE_COMMIT_UNKNOWN"
            )
        ),
        .userMessage(text: "再帮我加一条 10-06 的日程"),
        .result(
            Operation(
                operationID: "op-accept-review-calendar-done",
                tool: deviceTool,
                state: "needs_manual_review",
                domain: OperationReceipt.calendarDomain,
                recordID: "EKA-ACCEPT-0004",
                failureReason: "SOURCE_COMMIT_UNKNOWN"
            )
        ),
        .resolved(
            operationID: "op-accept-review-calendar-done",
            resolution: ManualResolution.confirmedWritten.rawValue,
            domain: OperationReceipt.calendarDomain
        ),
        .userMessage(text: "把这笔记到账本里"),
        // The pre-step-5 shape on purpose, both halves of it: no `domain` on the
        // operation and none on the marker. This is the pair the client is not
        // allowed to word as a calendar write, and the pair the neutral marker
        // copy exists for.
        .result(
            Operation(
                operationID: "op-accept-review-legacy",
                tool: "finance.record_expense",
                state: "needs_manual_review",
                recordID: "REC-ACCEPT-0001",
                failureReason: "SOURCE_COMMIT_UNKNOWN"
            )
        ),
        .resolved(
            operationID: "op-accept-review-legacy",
            resolution: ManualResolution.confirmedWritten.rawValue,
            domain: nil
        ),
        // The **unanswered** legacy record, and the reason this script has two of
        // them.
        //
        // The 2026-09-10 review found the checklist asking a person to look for
        // 「请先在飞书账本里核对这一笔」 on a card that could not show it: the
        // one legacy record in this script was answered by the marker directly
        // above, and `ChatView` hides the guidance and both buttons once an
        // operation is answered. The item above the pair is now a separate
        // record with no marker after it, and `AcceptanceChecklist` carries a
        // flag saying so, so the two can no longer be collapsed into one.
        .userMessage(text: "上个月那笔停车费也记一笔"),
        .result(
            Operation(
                operationID: "op-accept-review-legacy-pending",
                tool: "finance.record_expense",
                state: "needs_manual_review",
                recordID: "REC-ACCEPT-0002",
                failureReason: "SOURCE_COMMIT_UNKNOWN"
            )
        ),
    ]

    // --- the seeded values ---------------------------------------------------

    /// The calendar mirror's answer to 「下周有什么安排？」, in
    /// `calendar_query_projection`'s own shape.
    ///
    /// Two rows and two rules: an all-day range whose dates — not a converted
    /// instant — are its display authority, and a timed event rendered in its
    /// own zone with that zone named. Both are the rows the batch-0 off-by-one
    /// lived on, so a card that adds a day here is visible without a real
    /// calendar anywhere near it.
    public static let calendarQueryProjection = JSONValue.object([
        "status": .string("ok"),
        "source_system": .string(CalendarQueryResult.mirrorSourceSystem),
        "record_count": .number(2),
        "data_as_of": .string("2026-09-10T09:00:00+08:00"),
        "mirror_stale": .bool(false),
        "events": .array([
            .object([
                "event_identifier": .string("EKA-ACCEPT-0007"),
                "calendar_identifier": .string("CAL-ACCEPT-WORK"),
                "calendar_title": .string("工作"),
                "title": .string("东京出差"),
                "start": .string("2026-10-01T00:00:00+09:00"),
                "end": .string("2026-10-04T00:00:00+09:00"),
                "all_day": .bool(true),
                "start_date": .string("2026-10-01"),
                "end_date": .string("2026-10-04"),
                "date_anchor_unknown": .bool(false),
                "title_over_limit": .bool(false),
                "location_over_limit": .bool(false),
                "notes_over_limit": .bool(false),
                "created_by_agent": .bool(true),
            ]),
            .object([
                "event_identifier": .string("EKA-ACCEPT-0008"),
                "calendar_identifier": .string("CAL-ACCEPT-WORK"),
                "calendar_title": .string("工作"),
                "title": .string("客户拜访"),
                "start": .string("2026-10-02T14:00:00+09:00"),
                "end": .string("2026-10-02T15:30:00+09:00"),
                "all_day": .bool(false),
                "timezone": .string("Asia/Tokyo"),
                "date_anchor_unknown": .bool(false),
                "title_over_limit": .bool(false),
                "location_over_limit": .bool(false),
                "notes_over_limit": .bool(false),
                "created_by_agent": .bool(false),
            ]),
        ]),
    ])

    /// A fixed clock. Every seeded row is stamped from this rather than from
    /// `Date()`, so two runs of the acceptance suite describe the same archive
    /// and a diff between them means a real change.
    public static let seededAt = "2026-09-10T09:00:00+08:00"
}

// MARK: - rendering the script

extension AcceptanceScenario {

    /// The seeded archive: the events the client will load, and the projections
    /// it will read for operations it has to ask about.
    public struct Seed: Sendable, Equatable {
        public let conversationID: String
        public let events: [TimelineEvent]
        /// `operation_id` → the operation projection body, in the server's own
        /// JSON shape. Kept as JSON rather than as an `OperationReceipt` so that
        /// persistence round-trips through the shipping decoder: a harness that
        /// stored already-decoded values would prove the decoder on a path
        /// nothing else uses.
        public let projections: [String: JSONValue]
    }

    /// Build the archive.
    ///
    /// Deterministic: the same script always produces the same event ids and the
    /// same order, which is what lets the checklist name an event and the tests
    /// hold the two together.
    public static func seed(
        from steps: [Step] = AcceptanceScenario.steps
    ) -> Seed {
        var events: [TimelineEvent] = []
        var projections: [String: JSONValue] = [:]
        for (index, step) in steps.enumerated() {
            let eventID = String(format: "evt_accept_%03d", index + 1)
            switch step {
            case .userMessage(let text):
                events.append(
                    TimelineEvent(
                        eventID: eventID,
                        eventType: "user_message",
                        operationID: nil,
                        createdAt: seededAt,
                        content: ["text": .string(text)]
                    )
                )
            case .result(let operation):
                events.append(
                    TimelineEvent(
                        eventID: eventID,
                        eventType: "operation_result",
                        operationID: operation.operationID,
                        createdAt: seededAt,
                        content: eventContent(operation)
                    )
                )
                projections[operation.operationID] = projection(operation)
            case .resolved(let operationID, let resolution, let domain):
                var content: [String: JSONValue] = ["resolution": .string(resolution)]
                // Absent, not null: that is the shape an event written before
                // step 5 has, and it is the shape the client's neutral marker
                // copy exists for.
                if let domain { content["domain"] = .string(domain) }
                events.append(
                    TimelineEvent(
                        eventID: eventID,
                        eventType: "manual_review_resolved",
                        operationID: operationID,
                        createdAt: seededAt,
                        content: content
                    )
                )
            }
        }
        return Seed(
            conversationID: conversationID,
            events: events,
            projections: projections
        )
    }

    /// The `operation_result` event's content, matching
    /// `app.py:_operation_event_content` key for key — including the rule that a
    /// null field is *omitted* rather than written as `null`.
    static func eventContent(_ operation: Operation) -> [String: JSONValue] {
        var content: [String: JSONValue] = [
            "state": .string(operation.state),
            // Written even when null: a new event states which tool ran, and the
            // client reads presence — not non-null-ness — to tell "no tool" from
            // "an event that predates tool recording".
            "tool": operation.tool.map { .string($0) } ?? .null,
        ]
        let optional: [(String, JSONValue?)] = [
            ("domain", operation.domain.map { .string($0) }),
            ("record_id", operation.recordID.map { .string($0) }),
            ("failure_reason", operation.failureReason.map { .string($0) }),
            ("device_result", operation.deviceResult.map { .string($0) }),
            ("device_action_id", operation.deviceActionID.map { .string($0) }),
            ("query_result", operation.queryResult),
        ]
        for (key, value) in optional {
            if let value { content[key] = value }
        }
        return content
    }

    /// The operation projection, matching `app.py:_operation_projection` — the
    /// shape the live receipt is decoded from, `cancel_requested` and
    /// `client_detached` included, because `OperationReceipt` requires both.
    static func projection(_ operation: Operation) -> JSONValue {
        var body: [String: JSONValue] = [
            "operation_id": .string(operation.operationID),
            "state": .string(operation.state),
            "cancel_requested": .bool(false),
            "client_detached": .bool(false),
            "tool": operation.tool.map { .string($0) } ?? .null,
        ]
        // The same fields the event content carries, from the same description:
        // a card drawn live and the same card drawn from history describe one
        // write here, and a projection that disagreed with its own event would
        // be a defect this harness invented.
        for (key, value) in eventContent(operation)
        where key != "state" && key != "tool" {
            body[key] = value
        }
        return .object(body)
    }

    /// Decode one seeded projection through the shipping receipt decoder.
    ///
    /// The path the acceptance build itself uses, so a checklist item asserted
    /// through this helper is asserting what the phone will draw.
    public static func receipt(
        _ operationID: String, in seed: Seed = AcceptanceScenario.seed()
    ) -> OperationReceipt? {
        guard case .object(let body)? = seed.projections[operationID],
              let data = try? JSONEncoder().encode(body)
        else { return nil }
        return try? JSONDecoder().decode(OperationReceipt.self, from: data)
    }
}
