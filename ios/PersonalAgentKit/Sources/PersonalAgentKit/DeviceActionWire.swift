import Foundation

/// The device-executed write the chat response carries to this phone.
///
/// `calendar.create_event` never crosses the MCP bridge: this device's EventKit
/// is the executor. The server authorises exactly like any governed write,
/// parks the operation at `source_in_progress`, and hands the action to the
/// device as a transient field of the chat response — the response **is** the
/// hand-off. The phone builds the event from exactly the fields the server
/// authorised (`event`), reports the outcome under the action's own
/// `action_id`, and nothing about the action is persisted in the receipt.
///
/// Fail closed, three ways:
///
/// - **an unknown `tool`** is refused here rather than guessed at: the device
///   does not invent semantics for a tool it was not built to run.
/// - **a missing required field** is refused, not repaired — an event with a
///   defaulted start time is not the event the user asked for.
/// - **any decode failure** routes to a `failed` report, so the server learns
///   the action was not executed and its timeout sweep is not the only witness.
/// The event fields the server authorised, typed rather than carried as a
/// dictionary: a field the executor cannot name is a field it would ignore,
/// and a defaulted start time is not the event the user asked for.
public struct DeviceEventFields: Sendable, Equatable {
    public let title: String
    /// RFC 3339 absolute instants, as the server's create schema declares.
    public let start: String
    public let end: String
    public let allDay: Bool
    public let location: String?
    public let notes: String?

    public init(
        title: String, start: String, end: String, allDay: Bool,
        location: String? = nil, notes: String? = nil
    ) {
        self.title = title
        self.start = start
        self.end = end
        self.allDay = allDay
        self.location = location
        self.notes = notes
    }

    /// Convert to the executor's draft. `start`/`end` arrive as RFC 3339
    /// instants; the ISO8601DateFormatter parses them back into `Date`.
    public func draft() -> CalendarEventDraft? {
        guard let startDate = RFC3339.parse(start),
              let endDate = RFC3339.parse(end)
        else { return nil }
        return CalendarEventDraft(
            title: title, start: startDate, end: endDate, allDay: allDay,
            location: location, notes: notes
        )
    }
}

public struct DeviceEventAction: Sendable, Equatable {
    /// The operation's idempotency key. One message produces at most one
    /// device side effect, and this id is both what the event is built from
    /// and what the result report settles.
    public let actionID: String
    public let tool: String
    /// The event exactly as the server authorised it.
    public let event: DeviceEventFields

    /// The only tool this build can execute. The server derives executor
    /// forks from the IR; the client pins its executor capability by name.
    public static let supportedTool = "calendar.create_event"

    /// Decode and validate the server's `device_action` payload.
    ///
    /// Throws `DeviceActionError.unsupportedTool` for a tool this build does
    /// not run, and `DeviceActionError.malformed` for a payload missing what
    /// EventKit needs. Both must be reported as a `failed` result, never
    /// dropped silently: silence is what the server's timeout sweep reads as
    /// needs_manual_review, and a malformed action is a *known* failure, not
    /// an unknown state.
    public static func decode(from payload: [String: Any]) throws -> DeviceEventAction {
        let rawEvent = payload["event"] as? [String: Any]
        switch Self.validated(
            actionID: payload["action_id"] as? String,
            tool: payload["tool"] as? String,
            title: rawEvent?["title"] as? String,
            start: rawEvent?["start"] as? String,
            end: rawEvent?["end"] as? String,
            allDay: rawEvent?["all_day"] as? Bool,
            location: rawEvent?["location"] as? String,
            notes: rawEvent?["notes"] as? String
        ) {
        case .success(let action):
            return action
        case .failure(let error):
            throw error
        }
    }

    /// The one validation both decode paths share — the untyped dictionary
    /// form above and the `Decodable` envelope the receipt carries. A rule
    /// that lived twice could drift: the envelope would refuse a payload the
    /// dictionary form accepted, and the executor would depend on which door
    /// the action walked in through.
    fileprivate static func validated(
        actionID: String?, tool: String?, title: String?, start: String?,
        end: String?, allDay: Bool?, location: String?, notes: String?
    ) -> Result<DeviceEventAction, DeviceActionError> {
        guard let actionID, !actionID.isEmpty else {
            return .failure(.malformed("device_action is missing action_id"))
        }
        guard let tool, !tool.isEmpty else {
            return .failure(.malformed("device_action is missing tool"))
        }
        guard tool == Self.supportedTool else {
            return .failure(.unsupportedTool(tool))
        }
        guard let title, !title.isEmpty else {
            return .failure(.malformed("device_action event is missing title"))
        }
        guard let start, !start.isEmpty else {
            return .failure(.malformed("device_action event is missing start"))
        }
        guard let end, !end.isEmpty else {
            return .failure(.malformed("device_action event is missing end"))
        }
        guard let allDay else {
            return .failure(.malformed("device_action event is missing all_day"))
        }
        // Unparseable times are a malformed action, not an event to save at
        // midnight: a date the executor cannot read has no honest fallback.
        guard RFC3339.parse(start) != nil, RFC3339.parse(end) != nil else {
            return .failure(.malformed(
                "device_action event start/end are not RFC 3339 instants"
            ))
        }
        return .success(DeviceEventAction(
            actionID: actionID,
            tool: tool,
            event: DeviceEventFields(
                title: title, start: start, end: end, allDay: allDay,
                location: location, notes: notes
            )
        ))
    }
}

/// The transient `device_action` field as it travels on the receipt —
/// all-optional, because a shape this build cannot trust must not be able to
/// lose the whole reply. Resolution happens at execution time:
///
/// - a readable action resolves to `.execute`;
/// - a refusal **with** the action id is reportable now (`failed`), so the
///   server settles immediately instead of waiting out its sweep;
/// - a refusal **without** an action id (it was missing or empty) cannot name
///   what it refuses, so nothing is reportable and the server's timeout sweep
///   is the remaining witness — which is the honest state: the action is
///   unknown to this device.
public struct DeviceActionEnvelope: Decodable, Sendable, Equatable {
    private let actionID: String?
    private let tool: String?
    private let event: RawEvent?

    // Explicit keys, not the synthesised ones: the wire spells the id
    // `action_id`, and a synthesised key would silently decode it to `nil` —
    // making every well-formed action look like an unnameable refusal.
    private enum CodingKeys: String, CodingKey {
        case actionID = "action_id"
        case tool
        case event
    }

    private struct RawEvent: Decodable, Equatable {
        let title: String?
        let start: String?
        let end: String?
        let allDay: Bool?
        let location: String?
        let notes: String?

        private enum CodingKeys: String, CodingKey {
            case title, start, end
            case allDay = "all_day"
            case location, notes
        }
    }

    public enum Resolution: Sendable, Equatable {
        case execute(DeviceEventAction)
        /// The action id travels whenever it was readable: a refusal that can
        /// name its action is a reportable `failed`, not silence.
        case refuse(actionID: String?, error: DeviceActionError)
    }

    public func resolve() -> Resolution {
        let rawEvent = event.map { ($0.title, $0.start, $0.end, $0.allDay, $0.location, $0.notes) }
        switch DeviceEventAction.validated(
            actionID: actionID,
            tool: tool,
            title: rawEvent?.0,
            start: rawEvent?.1,
            end: rawEvent?.2,
            allDay: rawEvent?.3,
            location: rawEvent?.4,
            notes: rawEvent?.5
        ) {
        case .success(let action):
            return .execute(action)
        case .failure(let error):
            return .refuse(actionID: actionID, error: error)
        }
    }
}

/// Why an action could not be executed on this device.
public enum DeviceActionError: Error, Equatable, Sendable {
    /// A tool this build does not run. Never guessed at.
    case unsupportedTool(String)
    /// A payload missing required fields, or an unreadable one.
    case malformed(String)
}

/// The report the phone PATCHes back to
/// `/v1/device-actions/{action_id}/result`.
///
/// `created` and `duplicate` are both success evidence (the event exists);
/// `denied` and `failed` are the device's own zero-write testimony. The body
/// is closed: exactly these three fields, and `event_id` required for
/// created/duplicate — the server refuses anything else, so there is nothing
/// to lose by sending exactly this.
/// The seam that turns a handed action into a real EventKit write and its
/// report. `ChatTimeline` calls it the moment the chat reply lands, because
/// that reply is the only time the action exists on the wire — and the
/// operation the server parked at `source_in_progress` settles on this
/// report, not on a poll of anything.
public protocol DeviceActionExecuting: Sendable {
    /// Execute one action and report the outcome to the server. The returned
    /// receipt is the settled operation projection — executing *is* the
    /// settlement step, so the caller gets the final state in the same turn.
    ///
    /// `settlesOperationID` is the parked operation's *own* id, from the chat
    /// reply this action arrived on. The action id is the operation's
    /// idempotency key — the report endpoint's address, never an operation id —
    /// so when a report reply is lost, the parked-shape receipt this method
    /// degrades to must carry the real id: the caller's bounded poll reads
    /// `GET /v1/operations/{id}`, and polling the key would 404 on a write the
    /// server may well have settled.
    func executeAndReport(
        _ action: DeviceEventAction, settlesOperationID: String
    ) async -> OperationReceipt

    /// The one failure shape the executor cannot produce itself: an action
    /// this build decoded but cannot run at all (no executor composed, or a
    /// resolution that refused before naming real event fields). The report
    /// is the caller's job here — this call only decides the vocabulary.
    func failedReport(detail: String) -> DeviceActionResultBody
}

public struct DeviceActionResultBody: Sendable, Equatable, Encodable {
    public let result: String
    public let eventID: String?
    public let detail: String?

    private enum CodingKeys: String, CodingKey {
        case result
        case eventID = "event_id"
        case detail
    }

    public init(result: String, eventID: String?, detail: String?) {
        self.result = result
        self.eventID = eventID
        self.detail = detail
    }

    public static func created(eventID: String) -> DeviceActionResultBody {
        DeviceActionResultBody(result: "created", eventID: eventID, detail: nil)
    }

    public static func duplicate(eventID: String) -> DeviceActionResultBody {
        DeviceActionResultBody(result: "duplicate", eventID: eventID, detail: nil)
    }

    public static func denied(detail: String?) -> DeviceActionResultBody {
        DeviceActionResultBody(result: "denied", eventID: nil, detail: detail)
    }

    public static func failed(detail: String?) -> DeviceActionResultBody {
        DeviceActionResultBody(result: "failed", eventID: nil, detail: detail)
    }
}
