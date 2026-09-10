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

    // --- the v2 shape (§2.2) ---------------------------------------------
    //
    // `action_fields` writes these out explicitly, nulls included, so a client
    // can tell "absent" from "not applicable". They are optional *here* only
    // because a v1 action does not carry them at all, and that direction has
    // to keep working: the delivery gate stops a v2 action from reaching a v1
    // client, never the reverse, so this build can meet an action from a
    // server that predates the fields. Absent then means "this action cannot
    // say", and every consumer below degrades to what the v1 build did rather
    // than inventing a value.

    /// The EventKit identifier the server resolved the calendar name to — the
    /// field the write is bound to (§3.2). Absent on a v1 action.
    public let calendarIdentifier: String?
    /// The name the routing matched on, carried so the device can detect a
    /// rename between issuance and execution (§3.2, PRD §8).
    public let calendarTitle: String?
    public let timeZoneIdentifier: String?
    /// The all-day dates, exclusive end. Null (not absent) on a timed event.
    public let startDate: String?
    public let endDate: String?
    /// The 「仍要创建」 override (§3.3). Only the override endpoint can set it;
    /// absence means "run the local duplicate check", which is the safe
    /// default for an action that predates overrides.
    public let skipLocalDedup: Bool

    public init(
        title: String, start: String, end: String, allDay: Bool,
        location: String? = nil, notes: String? = nil,
        calendarIdentifier: String? = nil, calendarTitle: String? = nil,
        timeZoneIdentifier: String? = nil,
        startDate: String? = nil, endDate: String? = nil,
        skipLocalDedup: Bool = false
    ) {
        self.title = title
        self.start = start
        self.end = end
        self.allDay = allDay
        self.location = location
        self.notes = notes
        self.calendarIdentifier = calendarIdentifier
        self.calendarTitle = calendarTitle
        self.timeZoneIdentifier = timeZoneIdentifier
        self.startDate = startDate
        self.endDate = endDate
        self.skipLocalDedup = skipLocalDedup
    }

    /// Convert to the executor's draft. `start`/`end` arrive as RFC 3339
    /// instants; the ISO8601DateFormatter parses them back into `Date`.
    ///
    /// Still the pre-v2 construction for an all-day event, and deliberately
    /// untouched here: §3.2 replaces it with the floating dates built from
    /// `start_date`/`end_date` in the device calendar, and that change belongs
    /// with the write binding this action's `calendarIdentifier` exists for
    /// (§3.1, §3.2). Doing it here would leave the calendar binding behind —
    /// a write that knows the right dates but still lands in whichever
    /// calendar the device defaults to.
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
        switch Self.validated(RawActionFields(
            actionID: payload["action_id"] as? String,
            tool: payload["tool"] as? String,
            title: rawEvent?["title"] as? String,
            start: rawEvent?["start"] as? String,
            end: rawEvent?["end"] as? String,
            allDay: rawEvent?["all_day"] as? Bool,
            location: rawEvent?["location"] as? String,
            notes: rawEvent?["notes"] as? String,
            calendarIdentifier: rawEvent?["calendar_identifier"] as? String,
            calendarTitle: rawEvent?["calendar_title"] as? String,
            timeZoneIdentifier: rawEvent?["timezone"] as? String,
            startDate: rawEvent?["start_date"] as? String,
            endDate: rawEvent?["end_date"] as? String,
            skipLocalDedup: rawEvent?["skip_local_dedup"] as? Bool ?? false
        )) {
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
    ///
    /// It takes one parameter object rather than one argument per field. That
    /// is §5.2's rule applied to its own failure mode: the rule is that
    /// co-dispatched functions share a signature, and its worked example is a
    /// dispatch site that *adapted* two mismatched signatures. Fourteen
    /// positional arguments across two call sites invites exactly that, and a
    /// swapped pair here is silent — `title` and `startDate` are both strings.
    /// One struct passed by both callers cannot drift positionally.
    fileprivate static func validated(
        _ raw: RawActionFields
    ) -> Result<DeviceEventAction, DeviceActionError> {
        guard let actionID = raw.actionID, !actionID.isEmpty else {
            return .failure(.malformed("device_action is missing action_id"))
        }
        guard let tool = raw.tool, !tool.isEmpty else {
            return .failure(.malformed("device_action is missing tool"))
        }
        guard tool == Self.supportedTool else {
            return .failure(.unsupportedTool(tool))
        }
        guard let title = raw.title, !title.isEmpty else {
            return .failure(.malformed("device_action event is missing title"))
        }
        guard let start = raw.start, !start.isEmpty else {
            return .failure(.malformed("device_action event is missing start"))
        }
        guard let end = raw.end, !end.isEmpty else {
            return .failure(.malformed("device_action event is missing end"))
        }
        guard let allDay = raw.allDay else {
            return .failure(.malformed("device_action event is missing all_day"))
        }
        // Unparseable times are a malformed action, not an event to save at
        // midnight: a date the executor cannot read has no honest fallback.
        guard RFC3339.parse(start) != nil, RFC3339.parse(end) != nil else {
            return .failure(.malformed(
                "device_action event start/end are not RFC 3339 instants"
            ))
        }
        // The v2 dates are optional because a v1 action has none. A date that
        // *is* present and does not match the schema's `^\d{4}-\d{2}-\d{2}$`
        // is a different thing: the server authorised a shape it promised to
        // validate, so a malformed one means the hand-off cannot be trusted,
        // not that the field was omitted. Refusing is the fail-closed read.
        for (name, value) in [("start_date", raw.startDate), ("end_date", raw.endDate)] {
            guard let value else { continue }
            guard Self.isCalendarDate(value) else {
                return .failure(.malformed(
                    "device_action event \(name) is not YYYY-MM-DD"
                ))
            }
        }
        // An all-day action states both dates or neither; one of a pair is a
        // half-truth about which day the event is on, and §3.2 constructs the
        // event from exactly these. (The server writes both as null for a
        // timed event and both as dates for an all-day one.)
        guard (raw.startDate == nil) == (raw.endDate == nil) else {
            return .failure(.malformed(
                "device_action event carries only one all-day date"
            ))
        }
        return .success(DeviceEventAction(
            actionID: actionID,
            tool: tool,
            event: DeviceEventFields(
                title: title, start: start, end: end, allDay: allDay,
                location: raw.location, notes: raw.notes,
                calendarIdentifier: raw.calendarIdentifier,
                calendarTitle: raw.calendarTitle,
                timeZoneIdentifier: raw.timeZoneIdentifier,
                startDate: raw.startDate,
                endDate: raw.endDate,
                skipLocalDedup: raw.skipLocalDedup
            )
        ))
    }

    /// `YYYY-MM-DD`, the schema's own pattern for the all-day dates.
    static func isCalendarDate(_ value: String) -> Bool {
        let parts = value.split(separator: "-", omittingEmptySubsequences: false)
        guard parts.count == 3, parts[0].count == 4, parts[1].count == 2,
              parts[2].count == 2
        else { return false }
        // Calendar-valid, not merely digit-shaped: `2026-13-99` passes a
        // length check and would become a silently wrong date.
        guard let year = Int(parts[0]), let month = Int(parts[1]),
              let day = Int(parts[2]), year >= 1
        else { return false }
        var components = DateComponents()
        components.year = year
        components.month = month
        components.day = day
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0) ?? .gmt
        guard let date = calendar.date(from: components) else { return false }
        let round = calendar.dateComponents([.year, .month, .day], from: date)
        return round.year == year && round.month == month && round.day == day
    }
}

/// One action's raw fields, as whichever door read them. See `validated`.
struct RawActionFields: Sendable, Equatable {
    var actionID: String?
    var tool: String?
    var title: String?
    var start: String?
    var end: String?
    var allDay: Bool?
    var location: String?
    var notes: String?
    var calendarIdentifier: String?
    var calendarTitle: String?
    var timeZoneIdentifier: String?
    var startDate: String?
    var endDate: String?
    var skipLocalDedup: Bool
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
        // The v2 shape. All optional for the same reason `DeviceEventFields`
        // makes them optional — this decode reads a v1 action too.
        let calendarIdentifier: String?
        let calendarTitle: String?
        let timeZoneIdentifier: String?
        let startDate: String?
        let endDate: String?
        let skipLocalDedup: Bool?

        private enum CodingKeys: String, CodingKey {
            case title, start, end
            case allDay = "all_day"
            case location, notes
            case calendarIdentifier = "calendar_identifier"
            case calendarTitle = "calendar_title"
            case timeZoneIdentifier = "timezone"
            case startDate = "start_date"
            case endDate = "end_date"
            case skipLocalDedup = "skip_local_dedup"
        }
    }

    public enum Resolution: Sendable, Equatable {
        case execute(DeviceEventAction)
        /// The action id travels whenever it was readable: a refusal that can
        /// name its action is a reportable `failed`, not silence.
        case refuse(actionID: String?, error: DeviceActionError)
    }

    /// One element of `device_actions` this build could not read as an object
    /// at all — a string where an action should be, say. It keeps its place in
    /// the list so the plan's length still describes the reply, and it resolves
    /// to a refusal naming no action: nothing here says which operation is
    /// waiting, so nothing is reportable and the sweep owns the outcome.
    static let unreadable = DeviceActionEnvelope(
        actionID: nil, tool: nil, event: nil
    )

    private init(actionID: String?, tool: String?, event: RawEvent?) {
        self.actionID = actionID
        self.tool = tool
        self.event = event
    }

    public func resolve() -> Resolution {
        let fields = RawActionFields(
            actionID: actionID,
            tool: tool,
            title: event?.title,
            start: event?.start,
            end: event?.end,
            allDay: event?.allDay,
            location: event?.location,
            notes: event?.notes,
            calendarIdentifier: event?.calendarIdentifier,
            calendarTitle: event?.calendarTitle,
            timeZoneIdentifier: event?.timeZoneIdentifier,
            startDate: event?.startDate,
            endDate: event?.endDate,
            skipLocalDedup: event?.skipLocalDedup ?? false
        )
        switch DeviceEventAction.validated(fields) {
        case .success(let action):
            return .execute(action)
        case .failure(let error):
            return .refuse(actionID: actionID, error: error)
        }
    }
}

/// The plural `device_actions` field: every action one reply hands over, in
/// plan order (design §2.5.4, §4.2).
///
/// The list is the shape a v2 client reads, and a single action is a list of
/// one — never a second decode path, because a client that switched on length
/// would read a single-action reply through code no multi-action reply ever
/// exercised. The singular `device_action` is read only as the historical
/// field older servers emitted; it is a separate branch, never a merge, so a
/// response carrying both can never smuggle a second action past the list.
///
/// **One unreadable element does not drop its siblings.** A malformed list
/// element becomes an envelope that resolves to a refusal naming no action —
/// the same "cannot name what it refuses, so nothing is reportable" state a
/// nameless singular refusal has, which the timeout sweep owns. Dropping it
/// instead would silently shrink the plan, and every sibling that *did* decode
/// still has to run: they are different operations on the server, and one bad
/// row is not a reason to leave the others parked forever.
public struct DeviceActionEnvelopes: Sendable, Equatable {
    public let envelopes: [DeviceActionEnvelope]

    public init(_ envelopes: [DeviceActionEnvelope]) {
        self.envelopes = envelopes
    }
}

extension DeviceActionEnvelopes: Decodable {
    /// A wrapper that absorbs one element's decode failure instead of letting
    /// it fail the whole array. It cannot throw, so the unkeyed container
    /// always advances and the loop always terminates.
    private struct Lenient: Decodable {
        let envelope: DeviceActionEnvelope?
        init(from decoder: Decoder) throws {
            envelope = try? DeviceActionEnvelope(from: decoder)
        }
    }

    public init(from decoder: Decoder) throws {
        var container = try decoder.unkeyedContainer()
        var parsed: [DeviceActionEnvelope] = []
        while !container.isAtEnd {
            // `?? .unreadable` keeps the element in the list so the count
            // still describes the reply. It is not a placeholder for an action
            // this build guesses at: an unreadable envelope resolves to a
            // refusal that names nothing, so nothing is executed or reported.
            parsed.append(try container.decode(Lenient.self).envelope ?? .unreadable)
        }
        envelopes = parsed
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
    /// **`nil` is the lost-report case, and it is not a failure.** The report
    /// either never left or its reply never arrived, so this device holds no
    /// settled projection to show; the operation stays parked and the server's
    /// 15-minute sweep is the witness. The caller already holds the parked
    /// receipt for the operation it is polling, so it degrades to *that* rather
    /// than to a receipt constructed here — which is why this method no longer
    /// takes an operation id at all. It used to, to rebuild a parked-shape
    /// receipt on this path; with several actions in one reply (design §4.2)
    /// there is no single id to take. Only the message's *own* operation is
    /// polled by the caller, and an action's id is the operation's idempotency
    /// key, not an operation id — so for a sibling action the parameter had no
    /// honest value left, and a constructed one would have named the wrong
    /// operation. The parked receipt the caller already holds names the right
    /// one by construction.
    func executeAndReport(_ action: DeviceEventAction) async -> OperationReceipt?

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
