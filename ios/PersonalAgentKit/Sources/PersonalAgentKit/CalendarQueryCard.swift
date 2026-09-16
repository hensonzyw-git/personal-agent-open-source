import Foundation

/// The structured `calendar.query_events` projection, decoded strictly.
///
/// The server projects exactly the whitelisted fields of the mirror's read
/// contract into `query_result`; this type reads that object and nothing else.
/// It is a separate type from `FinanceQueryResult` for the same reason the two
/// are separate modules on the server: each keys on its own domain's const
/// (`source_system == "apple_calendar_mirror"` here, the expense metric const
/// there), so a result belonging to one domain can never be rendered as the
/// other's card. The tool decides which type is even attempted -- see
/// `OperationReceipt.calendarQueryEvidenceTools`.
///
/// **One deliberate asymmetry with the server's decoder**: unknown keys are
/// ignored rather than refused. Swift's `Decodable` does that unless a type
/// asks for the exact key set, and the server is the right side to be strict
/// on -- it is the writer and can refuse to emit. A client that refused would
/// go dark the day the server adds a field, which is a worse failure than
/// ignoring one it cannot use.
///
/// The invariants that *are* enforced are the ones the card would otherwise
/// render as a contradiction: an all-day row with no dates (its content *is*
/// the dates), a timed row carrying all-day dates, a timed row whose date
/// attribution is "unknown" (there is no date attribution to doubt), and a
/// truncation flag standing next to the very text it says was dropped.
public struct CalendarQueryResult: Sendable, Equatable {
    /// The mirror's own const. A projection under any other `source_system` is
    /// not this result, whatever else it looks like.
    static let mirrorSourceSystem = "apple_calendar_mirror"

    /// One mirror row, in the row vocabulary `design §9.2` fixes for the list
    /// card: 标题 · 日期时间（或全天日期区间）· 日历名 · [已创建].
    public struct EventRow: Sendable, Equatable {
        public let eventIdentifier: String
        public let calendarIdentifier: String
        /// The person's own name for the calendar. `nil` is a fact -- the
        /// device has no name for that identifier -- and is **not** a reason to
        /// show the EventKit UUID, which would put an identifier where a name
        /// belongs.
        public let calendarTitle: String?
        /// `nil` only when the mirror dropped the title for length; which of
        /// the two nulls it is, is exactly what `titleOverLimit` states.
        public let title: String?
        /// The absolute instants, as the device reported them. Never the
        /// authority for an all-day row.
        public let start: String
        public let end: String
        public let allDay: Bool
        /// The event's own zone, or `nil` for the v1 shape that predates it
        /// (the honest reading of which is Asia/Shanghai).
        public let timezone: String?
        /// The date columns -- the authority for an all-day row. `endDate` is
        /// **exclusive**, EventKit's own convention.
        public let startDate: String?
        public let endDate: String?
        /// The device could not confirm which day this all-day event belongs
        /// to. The date is still shown; it is annotated, not hidden.
        public let dateAnchorUnknown: Bool
        public let titleOverLimit: Bool
        public let locationOverLimit: Bool
        public let notesOverLimit: Bool
        public let location: String?
        public let notes: String?
        /// This row is an event this Agent created (design §9.2's 「已创建」).
        public let createdByAgent: Bool
    }

    /// One page of rows. Never the whole result when `nextCursor` is set --
    /// that is what 「看更多」 exists for.
    public let events: [EventRow]
    /// The whole window's total, which a page may be smaller than.
    public let recordCount: Int
    public let nextCursor: String?
    public let dataAsOf: String
    public let mirrorStale: Bool
    public let sourceSystem: String
}

extension CalendarQueryResult: Decodable {
    private enum CodingKeys: String, CodingKey {
        case status
        case events
        case recordCount = "record_count"
        case nextCursor = "next_cursor"
        case dataAsOf = "data_as_of"
        case mirrorStale = "mirror_stale"
        case sourceSystem = "source_system"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        // A body that says its own query failed must never render as a card of
        // zero events: 「这个时间段没有日程」 is a claim about the calendar, and
        // it is the wrong one to make from a failed read. The server refuses
        // this shape on the way out, so reaching it here means something else
        // wrote the body.
        let status = try container.decode(String.self, forKey: .status)
        guard status == "ok" else {
            throw DecodingError.dataCorruptedError(
                forKey: .status,
                in: container,
                debugDescription: "the query did not succeed"
            )
        }
        let source = try container.decode(String.self, forKey: .sourceSystem)
        guard source == Self.mirrorSourceSystem else {
            throw DecodingError.dataCorruptedError(
                forKey: .sourceSystem,
                in: container,
                debugDescription: "not the Apple calendar mirror's result"
            )
        }
        sourceSystem = source
        recordCount = try container.decode(Int.self, forKey: .recordCount)
        let rows = try container.decode([EventRow].self, forKey: .events)
        // The card states 「另有 N 条未列出」 from this difference, so a page
        // larger than the total is not a cosmetic problem: it renders a
        // negative count. The server refuses the same shape on the way out;
        // this is the display-side half of one rule, not a second rule.
        guard rows.count <= recordCount else {
            throw DecodingError.dataCorruptedError(
                forKey: .events,
                in: container,
                debugDescription: "the page carries more events than the total"
            )
        }
        events = rows
        nextCursor = try container.decodeIfPresent(String.self, forKey: .nextCursor)
        dataAsOf = try container.decode(String.self, forKey: .dataAsOf)
        mirrorStale = try container.decode(Bool.self, forKey: .mirrorStale)
    }
}

extension CalendarQueryResult.EventRow: Decodable {
    private enum CodingKeys: String, CodingKey {
        case eventIdentifier = "event_identifier"
        case calendarIdentifier = "calendar_identifier"
        case calendarTitle = "calendar_title"
        case title
        case start
        case end
        case allDay = "all_day"
        case timezone
        case startDate = "start_date"
        case endDate = "end_date"
        case dateAnchorUnknown = "date_anchor_unknown"
        case titleOverLimit = "title_over_limit"
        case locationOverLimit = "location_over_limit"
        case notesOverLimit = "notes_over_limit"
        case location
        case notes
        case createdByAgent = "created_by_agent"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        eventIdentifier = try container.decode(String.self, forKey: .eventIdentifier)
        calendarIdentifier = try container.decode(
            String.self, forKey: .calendarIdentifier
        )
        calendarTitle = try container.decodeIfPresent(
            String.self, forKey: .calendarTitle
        )
        title = try container.decodeIfPresent(String.self, forKey: .title)
        start = try container.decode(String.self, forKey: .start)
        end = try container.decode(String.self, forKey: .end)
        allDay = try container.decode(Bool.self, forKey: .allDay)
        timezone = try container.decodeIfPresent(String.self, forKey: .timezone)
        startDate = try container.decodeIfPresent(String.self, forKey: .startDate)
        endDate = try container.decodeIfPresent(String.self, forKey: .endDate)
        dateAnchorUnknown = try container.decode(
            Bool.self, forKey: .dateAnchorUnknown
        )
        titleOverLimit = try container.decode(Bool.self, forKey: .titleOverLimit)
        locationOverLimit = try container.decode(
            Bool.self, forKey: .locationOverLimit
        )
        notesOverLimit = try container.decode(Bool.self, forKey: .notesOverLimit)
        location = try container.decodeIfPresent(String.self, forKey: .location)
        notes = try container.decodeIfPresent(String.self, forKey: .notes)
        createdByAgent = try container.decode(Bool.self, forKey: .createdByAgent)

        if allDay {
            guard startDate != nil, endDate != nil else {
                throw DecodingError.dataCorruptedError(
                    forKey: .startDate,
                    in: container,
                    debugDescription: "an all-day event must carry both dates"
                )
            }
            guard timezone == nil else {
                throw DecodingError.dataCorruptedError(
                    forKey: .timezone,
                    in: container,
                    debugDescription: "an all-day event must not carry a timezone"
                )
            }
        } else {
            guard startDate == nil, endDate == nil else {
                throw DecodingError.dataCorruptedError(
                    forKey: .startDate,
                    in: container,
                    debugDescription: "a timed event must not carry all-day dates"
                )
            }
            guard !dateAnchorUnknown else {
                throw DecodingError.dataCorruptedError(
                    forKey: .dateAnchorUnknown,
                    in: container,
                    debugDescription: "a timed event has no date attribution to doubt"
                )
            }
        }
        // The flag is the only surviving evidence of text the mirror could not
        // keep. Flag and text together describe a state the mirror is not in.
        if titleOverLimit && title != nil {
            throw DecodingError.dataCorruptedError(
                forKey: .titleOverLimit,
                in: container,
                debugDescription: "title_over_limit is set but the title is present"
            )
        }
        if locationOverLimit && location != nil {
            throw DecodingError.dataCorruptedError(
                forKey: .locationOverLimit,
                in: container,
                debugDescription: "location_over_limit is set but the location is present"
            )
        }
        if notesOverLimit && notes != nil {
            throw DecodingError.dataCorruptedError(
                forKey: .notesOverLimit,
                in: container,
                debugDescription: "notes_over_limit is set but the notes are present"
            )
        }
    }
}

// MARK: - Presentation

/// The rules the row's line is rendered by.
///
/// These mirror `src/personal_agent/api/calendar_query_projection.py`'s
/// `_event_line` / `_all_day_span` / `_start_moment` / `_zone_label`, rule for
/// rule: an all-day event from its **dates** (never a converted instant, which
/// is how a Tokyo 10-01 became a 09-30), a timed event in **its own zone with
/// the zone named** (never folded to Shanghai), and the truncation and
/// uncertainty annotations (不静默). They are kept in step by hand, by design
/// -- the server's deterministic `answer` and this card are two renderings of
/// the same fields, and `ChatTimelineTests` holds the first three lines of the
/// vector's own summary against what this renders from the same row.
extension CalendarQueryResult.EventRow {
    /// The row's 标题 cell. A dropped title is named as such rather than shown
    /// as 「无标题」, which is a different fact about the same null.
    public var displayTitle: String {
        if let title { return title }
        return titleOverLimit ? "标题过长未同步" : "（无标题日程）"
    }

    /// The row's 日期时间 cell -- the all-day span, or the timed start in the
    /// event's own zone.
    public var when: String {
        allDay ? allDayLabel : startMoment
    }

    /// The honesty notes, in the server's order (`_event_line`).
    public var annotations: [String] {
        var notes: [String] = []
        if allDay && dateAnchorUnknown { notes.append("日期归属未确认") }
        if locationOverLimit { notes.append("地点过长未同步") }
        if notesOverLimit { notes.append("备注过长未同步") }
        return notes
    }

    /// The whole line: `标题（日期时间，标注）`. The list card draws the parts
    /// separately for layout; this is what the two sides are pinned against.
    public var line: String {
        let suffix = annotations.isEmpty ? "" : "，" + annotations.joined(separator: "、")
        return "\(displayTitle)（\(when)\(suffix)）"
    }

    /// `10-02 全天`, or `10-01 至 10-03 全天` for a range: the stored end date
    /// is exclusive, so the last day is the one before it.
    ///
    /// Named `allDayLabel` and not `allDaySpan`: the write side has a span too
    /// (`CalendarWriteRules.allDayWriteSpan`), and it is a *pair of instants*
    /// in the opposite direction from this *string*. Two things called
    /// `allDaySpan` meaning a date pair and a rendered label is how a day could
    /// be added on one side of the wire and not the other with nothing to
    /// notice it.
    var allDayLabel: String {
        // Unreachable from decoded wire -- the initialiser refuses an all-day
        // row without both dates -- and shaped like the server's own fallback
        // for the same reason: a row built by hand in a test should say what
        // it is missing rather than pretend to a date.
        guard let startDate, let endDate else {
            return "\(startDate ?? "") 至 \(endDate ?? "") 全天"
        }
        guard let first = Self.isoDate(startDate), let exclusive = Self.isoDate(endDate)
        else { return "\(startDate) 至 \(endDate) 全天" }
        let last = CalendarQueryResult.calendar.date(
            byAdding: .day, value: -1, to: exclusive
        ) ?? exclusive
        if last <= first { return "\(Self.monthDay(first)) 全天" }
        return "\(Self.monthDay(first)) 至 \(Self.monthDay(last)) 全天"
    }

    /// `09-13 10:00 开始`, with the zone named when it is not the reference.
    var startMoment: String {
        guard let instant = RFC3339.parse(start) else {
            // A start the device wrote in some other RFC 3339 shape is still a
            // fact; showing its own text beats showing nothing. Same slice the
            // server takes -- the date and the time, to the minute.
            let text = String(start.prefix(16)).replacingOccurrences(of: "T", with: " ")
            return "\(text) 开始"
        }
        guard let identifier = timezone, identifier != Self.referenceZone
        else { return "\(Self.clock(instant, in: Self.referenceTimeZone)) 开始" }
        guard let zone = TimeZone(identifier: identifier) else {
            // A zone this client cannot construct is still what the device
            // reported; naming it is more honest than showing Shanghai time.
            return "\(Self.clock(instant, in: Self.referenceTimeZone)) \(identifier) 开始"
        }
        let label = Self.zoneLabel(identifier, at: instant)
        let clock = Self.clock(instant, in: zone)
        return label.isEmpty ? "\(clock) 开始" : "\(clock) \(label) 开始"
    }

    /// The reference zone: an event with no zone of its own is read as
    /// Shanghai, which is what every upload before the v2 shape meant.
    static let referenceZone = "Asia/Shanghai"
    static let referenceTimeZone = TimeZone(identifier: referenceZone) ?? .gmt

    /// The Chinese short name for a zone, or its offset at that instant. A zone
    /// outside the table is still named -- exactly, as its UTC offset then --
    /// rather than silently rendered as if it were local time.
    static func zoneLabel(_ identifier: String, at instant: Date) -> String {
        if let label = zoneLabels[identifier] { return label }
        guard let zone = TimeZone(identifier: identifier) else { return identifier }
        let seconds = zone.secondsFromGMT(for: instant)
        if seconds == 0 { return "UTC" }
        let sign = seconds >= 0 ? "+" : "-"
        let (hours, minutes) = (abs(seconds) / 60).quotientAndRemainder(dividingBy: 60)
        return String(format: "UTC%@%02d:%02d", sign, hours, minutes)
    }

    /// The zones Henson's calendar actually holds (design §5.3).
    static let zoneLabels: [String: String] = [
        "Asia/Tokyo": "日本时间",
        "Asia/Seoul": "韩国时间",
        "Asia/Singapore": "新加坡时间",
        "Asia/Hong_Kong": "香港时间",
        "Asia/Bangkok": "泰国时间",
        "Asia/Kolkata": "印度时间",
        "Asia/Dubai": "迪拜时间",
        "Europe/London": "英国时间",
        "Europe/Paris": "法国时间",
        "Europe/Berlin": "德国时间",
        "Europe/Moscow": "莫斯科时间",
        "America/New_York": "纽约时间",
        "America/Chicago": "芝加哥时间",
        "America/Denver": "丹佛时间",
        "America/Los_Angeles": "洛杉矶时间",
        "America/Sao_Paulo": "圣保罗时间",
        "Australia/Sydney": "悉尼时间",
        "Pacific/Auckland": "奥克兰时间",
        "UTC": "UTC",
        "Etc/UTC": "UTC",
    ]

    static func clock(_ instant: Date, in zone: TimeZone) -> String {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        let parts = calendar.dateComponents(
            [.month, .day, .hour, .minute], from: instant
        )
        return String(
            format: "%02d-%02d %02d:%02d",
            parts.month ?? 0, parts.day ?? 0, parts.hour ?? 0, parts.minute ?? 0
        )
    }

    static func monthDay(_ day: Date) -> String {
        let parts = CalendarQueryResult.calendar.dateComponents(
            [.month, .day], from: day
        )
        return String(format: "%02d-%02d", parts.month ?? 0, parts.day ?? 0)
    }

    /// `YYYY-MM-DD`, and nothing else -- the shape the row decoder already
    /// proved. Parsed in a fixed calendar so no locale can move the day.
    static func isoDate(_ text: String) -> Date? {
        let parts = text.split(separator: "-").compactMap { Int($0) }
        guard parts.count == 3 else { return nil }
        return CalendarQueryResult.calendar.date(
            from: DateComponents(year: parts[0], month: parts[1], day: parts[2])
        )
    }
}

extension CalendarQueryResult {
    static let calendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "UTC") ?? .gmt
        return calendar
    }()
}
