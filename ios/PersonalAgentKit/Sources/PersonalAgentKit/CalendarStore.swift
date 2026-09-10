import Foundation
#if canImport(EventKit)
@preconcurrency import EventKit
#endif

/// One event as this device's calendar owns it — the shape both the device
/// action and the mirror upload travel in.
///
/// The v2 fields are the action's binding (§3.2), not EventKit's state: which
/// calendar the write is bound to, the zone a timed event displays in, and the
/// floating dates an all-day event is constructed from. They are `nil` for a v1
/// action, and every consumer degrades to what the v1 build did rather than
/// inventing a value — the delivery gate stops a v2 action reaching a v1
/// client, never the reverse.
public struct CalendarEventDraft: Sendable, Equatable {
    public var title: String
    /// The absolute instants the server authorised. For an all-day event these
    /// are the action's offsets, not the dates to write: `startDate`/`endDate`
    /// below are what the device constructs from (§3.2).
    public var start: Date
    public var end: Date
    public var allDay: Bool
    public var location: String?
    public var notes: String?

    /// The EventKit identifier the write is bound to. `nil` means the action
    /// predates the field: write to the default writable calendar, as v1 did.
    public var calendarIdentifier: String?
    /// The name the server's routing matched on, for the rename check.
    public var calendarTitle: String?
    /// The zone a **timed** event is displayed in. Never applied to an all-day
    /// event: EventKit flips `isAllDay` back to false when one is set.
    public var timeZoneIdentifier: String?
    /// All-day floating dates, exclusive end, `YYYY-MM-DD`.
    public var startDate: String?
    public var endDate: String?
    /// The 「仍要创建」 override (§3.3). Only the override endpoint sets it.
    public var skipLocalDedup: Bool

    public init(
        title: String, start: Date, end: Date, allDay: Bool,
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
}

/// One event as the mirror upload carries it — exactly the server's
/// `calendar.ingest_events` item schema. The identifiers and `lastModified`
/// are the arbitration fields, so they are non-optional: a mirror row without
/// them is not constructible, rather than a 400 at upload time.
///
/// `title` stays optional because EventKit's own title can be nil, and the
/// schema accepts null — guessing `""` would be the device inventing content.
///
/// Every v2 field is optional here, and absent means **the absence of a
/// claim**, not a default: `timeZoneIdentifier` nil uploads no `timezone` key
/// (the server reads that as the v1 shape), `dateAnchorUnknown` nil uploads no
/// `date_anchor_unknown` key (the server treats silence as unconfirmed), and
/// the three `*OverLimit` flags upload only when true. `allDayStartDate` /
/// `allDayEndDate` are the only v2 fields an all-day event must carry, and
/// `CalendarMirrorRules.mirrorEvent` refuses to build one that lacks them.
public struct CalendarMirrorEvent: Sendable, Equatable {
    public let eventIdentifier: String
    public let calendarIdentifier: String
    public let title: String?
    public let start: Date
    public let end: Date
    public let allDay: Bool
    public let location: String?
    public let notes: String?
    /// The device-side last-modified instant; the upsert arbitrates out-of-
    /// order batches with it.
    public let lastModified: Date

    /// The event's IANA zone, for a timed event EventKit states one for.
    /// Never a device-local guess.
    public let timeZoneIdentifier: String?
    /// All-day events only: the frozen dates the wire carries, and whether the
    /// device can vouch for their anchor.
    public let allDayStartDate: String?
    public let allDayEndDate: String?
    public let dateAnchorUnknown: Bool
    /// Whether this device's persisted record says the agent created it.
    public let createdByAgent: Bool
    /// A field over its code-point threshold was nulled, per design §6.
    public let titleOverLimit: Bool
    public let locationOverLimit: Bool
    public let notesOverLimit: Bool

    public init(
        eventIdentifier: String, calendarIdentifier: String, title: String?,
        start: Date, end: Date, allDay: Bool, location: String?, notes: String?,
        lastModified: Date,
        timeZoneIdentifier: String? = nil,
        allDayStartDate: String? = nil,
        allDayEndDate: String? = nil,
        dateAnchorUnknown: Bool = false,
        createdByAgent: Bool = false,
        titleOverLimit: Bool = false,
        locationOverLimit: Bool = false,
        notesOverLimit: Bool = false
    ) {
        self.eventIdentifier = eventIdentifier
        self.calendarIdentifier = calendarIdentifier
        self.title = title
        self.start = start
        self.end = end
        self.allDay = allDay
        self.location = location
        self.notes = notes
        self.lastModified = lastModified
        self.timeZoneIdentifier = timeZoneIdentifier
        self.allDayStartDate = allDayStartDate
        self.allDayEndDate = allDayEndDate
        self.dateAnchorUnknown = dateAnchorUnknown
        self.createdByAgent = createdByAgent
        self.titleOverLimit = titleOverLimit
        self.locationOverLimit = locationOverLimit
        self.notesOverLimit = notesOverLimit
    }
}

/// What a save produced. `created` and `duplicate` are both success evidence;
/// the eventID is what the server records as the write's external evidence.
public enum CalendarSaveOutcome: Sendable, Equatable {
    case created(eventID: String)
    /// A same-title event within ±5 minutes already exists; nothing was saved.
    case duplicate(existingID: String)
    /// The user refused calendar access. The device's own zero-write testimony.
    case denied
    case failed(detail: String)
}

/// The seam between the device-action executor and EventKit.
///
/// A protocol because the failure shapes of §5.1 must be testable without a
/// real calendar: the fake counterpart is written from the contract, and the
/// real one is what the device reports against. The v1 write scope is
/// create-only (v1 仅创建) and single-occurrence events only (v1 仅单次).
public protocol CalendarStore: Sendable {
    /// Save one single-occurrence event, with local duplicate detection:
    /// a same-title event within ±5 minutes of the proposed start makes this
    /// a `duplicate` rather than a second event.
    ///
    /// A draft that names its calendar (§3.2) is bound to it: the duplicate
    /// check runs only inside that calendar (§3.1), and every way the calendar
    /// can be unusable — gone, read-only, renamed — is a `failed` with a
    /// reason. The store never silently writes somewhere else.
    func save(_ draft: CalendarEventDraft) async -> CalendarSaveOutcome

    /// The device's calendar directory (design §2.1): every ordinary event
    /// calendar, **including subscribed ones**, because the server needs their
    /// metadata to recognise and refuse them. Birthday sources are not
    /// ordinary event calendars and are absent from both the directory and the
    /// snapshot (§2.4).
    func calendarDirectory() async throws -> [CalendarDirectoryEntry]

    /// Remember one event this device created for the agent (§8, Q10). The
    /// record is local and app-scoped: a restored or replaced phone has no
    /// record, and the event uploads as external — which is the honest
    /// degradation the schema's default asks for, not a defect to paper over.
    func recordAgentCreated(_ record: AgentCreatedEvent) async

    /// Snapshot the events overlapping [since, until) that the mirror uploads:
    /// only the non-subscribed calendars of the directory (§2.4), with §3.2's
    /// read-back algorithm and §6's thresholds applied per event.
    ///
    /// `asOf` is the snapshot instant: EventKit exposes no per-event
    /// last-modified, so the snapshot itself is what the upsert arbitrates on
    /// — a later snapshot has a later stamp and wins, which is honest because
    /// the batch, not the row, is what the device vouches for.
    ///
    /// Throws rather than omitting an event it cannot describe. A batch that
    /// leaves an event out and still carries `window_complete` tells the server
    /// the user deleted it, and the server would tombstone a mirror row for an
    /// event sitting on the phone. An unrepresentable event fails the window
    /// instead: nothing is uploaded, nothing is deleted, and the staleness is
    /// visible in `mirror_stale`.
    func snapshot(since: Date, until: Date, asOf: Date) async throws -> [CalendarMirrorEvent]
}

/// Where the device keeps its record of agent-created events. A seam so the
/// snapshot's use of it is testable without a real defaults database.
public protocol AgentCreatedEventLog: Sendable {
    func all() async -> [String: AgentCreatedEvent]
    func record(_ record: AgentCreatedEvent) async
}

/// The real log: a small array in the app's own defaults, namespaced under one
/// key. It is not the ledger and carries no calendar content — only the ids
/// and, for all-day events, the two dates the action carried.
///
/// Growth is bounded by `limit`, oldest first. Nothing prunes it by asking the
/// calendar what still exists: "the event is gone" and "the event is outside
/// the window the caller happened to snapshot" are indistinguishable from
/// inside the log, and a record dropped for the second reason would silently
/// relabel a live agent-created event as external. The bound plus the schema's
/// documented default is the whole reclamation story.
public struct UserDefaultsAgentCreatedEventLog: AgentCreatedEventLog {
    // `UserDefaults` is documented thread-safe and is not `Sendable` only
    // because it has no Swift-level annotation. The annotation belongs here
    // rather than on the struct: everything else in it is a value.
    private nonisolated(unsafe) let defaults: UserDefaults
    private let key: String
    private let limit: Int

    public init(
        defaults: UserDefaults = .standard,
        key: String = "calendar.agentCreatedEvents",
        limit: Int = 2_000
    ) {
        self.defaults = defaults
        self.key = key
        self.limit = limit
    }

    public func all() async -> [String: AgentCreatedEvent] {
        Dictionary(rows().map { ($0.eventIdentifier, $0) }, uniquingKeysWith: { _, new in new })
    }

    public func record(_ record: AgentCreatedEvent) async {
        var stored = rows().filter { $0.eventIdentifier != record.eventIdentifier }
        stored.append(record)
        // Insertion order, trimmed from the front: the array is the store's
        // real shape, so the eviction is deterministic rather than whatever
        // order a dictionary happened to hand back.
        write(Array(stored.suffix(limit)))
    }

    /// The stored rows, in the order they were written.
    private func rows() -> [AgentCreatedEvent] {
        guard let data = defaults.data(forKey: key),
              let rows = try? JSONDecoder().decode([AgentCreatedEvent].self, from: data)
        else { return [] }
        return rows
    }

    /// A write that fails to encode leaves the previous value in place: the
    /// log is an optimisation for `created_by_agent`, and losing it degrades
    /// to the schema's documented default rather than corrupting state. An
    /// empty log is stored as no key at all.
    private func write(_ events: [AgentCreatedEvent]) {
        guard !events.isEmpty else {
            defaults.removeObject(forKey: key)
            return
        }
        guard let data = try? JSONEncoder().encode(events) else { return }
        defaults.set(data, forKey: key)
    }
}

#if canImport(EventKit)

/// The EventKit implementation.
///
/// iOS 17+ asks `requestFullAccessToEvents` (the sync upload needs read
/// access too, and full access is the only scope that grants both); iOS ≤16
/// falls back to `requestAccess(to: .event)`. A refusal resolves `denied` —
/// it is the user's own answer on this device, which is exactly the
/// zero-write evidence the server's `failed_safe` expects.
public struct EventKitCalendarStore: CalendarStore {
    private let store: EKEventStore
    private let log: any AgentCreatedEventLog
    /// The calendar the mirror's date arithmetic runs in. `Calendar.current`
    /// by design (§3.2): an all-day event's date is a floating calendar date,
    /// and the device calendar is the only frame that renders it the way the
    /// user sees it. Not `TimeZone.current` — the two differ in the details
    /// that matter here, so the frame is passed in rather than re-derived.
    private let calendar: Calendar

    public init(
        store: EKEventStore = EKEventStore(),
        log: any AgentCreatedEventLog = UserDefaultsAgentCreatedEventLog(),
        calendar: Calendar = .current
    ) {
        self.store = store
        self.log = log
        self.calendar = calendar
    }

    // MARK: authorisation

    private func requestAccess() async -> Bool {
        if #available(iOS 17.0, *) {
            return (try? await store.requestFullAccessToEvents()) ?? false
        } else {
            return await withCheckedContinuation { continuation in
                store.requestAccess(to: .event) { granted, _ in
                    continuation.resume(returning: granted)
                }
            }
        }
    }

    // MARK: save

    public func save(_ draft: CalendarEventDraft) async -> CalendarSaveOutcome {
        guard await requestAccess() else {
            return .denied
        }

        // §3.2: a v2 action names its calendar, and the write is bound to it.
        // A v1 action names none and keeps the pre-v2 behaviour — the delivery
        // gate stops a v2 action reaching a v1 client, never the reverse, so
        // this build still meets actions that carry no binding.
        let target: EKCalendar?
        if let identifier = draft.calendarIdentifier {
            let directory: [CalendarDirectoryEntry]
            do {
                directory = try await calendarDirectory()
            } catch {
                return .denied
            }
            switch CalendarWriteRules.target(
                identifier: identifier, title: draft.calendarTitle, in: directory
            ) {
            case .refuse(let detail):
                return .failed(detail: detail)
            case .write(let entry):
                guard let resolved = store.calendar(
                    withIdentifier: entry.calendarIdentifier
                ) else {
                    // The directory listed it a moment ago; nothing this build
                    // does can account for it vanishing between two reads.
                    return .failed(detail: "the target calendar is not on this device")
                }
                target = resolved
            }
        } else {
            target = defaultWritableCalendar()
            guard target != nil else {
                return .failed(detail: "no writable calendar")
            }
        }

        // Local duplicate detection before any write: the same title within
        // ±5 minutes of the proposed start means the user (or an earlier
        // attempt) already put this event there. Auto-retry is off at the
        // server precisely because it could double-create; this is the
        // mitigation, not a guarantee.
        //
        // §3.1 narrows the scan to the target calendar. The predicate is the
        // optimisation; `CalendarWriteRules.duplicate` is the rule and applies
        // the same scope, so the two cannot disagree about what was checked.
        let scope: [EKCalendar] = target.map { [$0] } ?? store.calendars(for: .event)
        let windowStart = draft.start.addingTimeInterval(-CalendarWriteRules.duplicateWindow)
        let windowEnd = draft.start.addingTimeInterval(CalendarWriteRules.duplicateWindow)
        let predicate = store.predicateForEvents(
            withStart: windowStart, end: windowEnd, calendars: scope
        )
        let candidates = store.events(matching: predicate).compactMap { event -> CalendarDuplicateCandidate? in
            guard let eventIdentifier = event.eventIdentifier,
                  let owner = event.calendar?.calendarIdentifier
            else { return nil }
            return CalendarDuplicateCandidate(
                eventIdentifier: eventIdentifier, calendarIdentifier: owner,
                title: event.title, start: event.startDate
            )
        }
        if let existing = CalendarWriteRules.duplicate(
            of: draft, in: draft.calendarIdentifier, among: candidates
        ) {
            return .duplicate(existingID: existing)
        }

        let event = EKEvent(eventStore: store)
        event.title = draft.title
        event.isAllDay = draft.allDay
        event.location = draft.location
        event.notes = draft.notes
        event.calendar = target
        // A v1 all-day action carries no dates at all, and keeps the v1
        // construction. *Half* a pair is a different thing — the decode layer
        // refuses it, and one arriving here anyway fails closed rather than
        // falling back to the absolute instants and writing the wrong day.
        let hasAllDayDates = draft.startDate != nil || draft.endDate != nil
        if draft.allDay, hasAllDayDates {
            // §3.2: the floating dates, constructed in the device calendar.
            // No `event.timeZone` is set — the probe showed it flips
            // `isAllDay` back to false.
            guard let startDate = draft.startDate, let endDate = draft.endDate,
                  let span = CalendarWriteRules.allDaySpan(
                      startDate: startDate, endDate: endDate, in: calendar
                  )
            else {
                return .failed(detail: "the authorised all-day dates are not usable")
            }
            event.startDate = span.start
            event.endDate = span.end
        } else {
            event.startDate = draft.start
            event.endDate = draft.end
            // Timed: the zone makes the event *display* by the local rules.
            // An identifier this device does not know is ignored rather than
            // refused — the instant is correct either way — and an all-day
            // draft yields none at all (§3.2; `displayZone` is the rule).
            if let identifier = CalendarWriteRules.displayZone(for: draft),
               let zone = TimeZone(identifier: identifier) {
                event.timeZone = zone
            }
        }
        do {
            try store.save(event, span: .thisEvent)
            return .created(eventID: event.eventIdentifier)
        } catch {
            return .failed(detail: error.localizedDescription)
        }
    }

    /// The first writable calendar, preferring the system default. Writing
    /// into a subscribed or read-only source would fail at `save` with an
    /// error that reads like a bug; choosing deliberately keeps the failure
    /// vocabulary small.
    private func defaultWritableCalendar() -> EKCalendar? {
        if let source = store.defaultCalendarForNewEvents?.source,
           let calendar = store.calendars(for: .event).first(where: {
               $0.source == source && $0.allowsContentModifications
           }) {
            return calendar
        }
        return store.defaultCalendarForNewEvents
    }

    // MARK: directory

    public func calendarDirectory() async throws -> [CalendarDirectoryEntry] {
        guard await requestAccess() else {
            throw CalendarSnapshotError.accessDenied
        }
        // Every ordinary event calendar, subscribed ones included (§2.1). The
        // birthday source is synthesised from contact data rather than being a
        // calendar the user keeps, so it is not part of the directory and its
        // events never leave the device (§2.4).
        return store.calendars(for: .event)
            .filter { $0.source?.sourceType != .birthdays }
            .compactMap { calendar in
                // A calendar with an empty identifier cannot be named in the
                // wire (the schema's `calendar_identifier` has minLength 1),
                // and the server resolves names against this directory — an
                // entry it cannot be addressed by is not an entry. EventKit
                // types this non-optional, so emptiness is the only case.
                let identifier = calendar.calendarIdentifier
                guard !identifier.isEmpty else { return nil }
                return CalendarDirectoryEntry(
                    calendarIdentifier: identifier,
                    title: calendar.title,
                    sourceTitle: calendar.source?.title,
                    allowsContentModifications: calendar.allowsContentModifications,
                    isSubscribed: calendar.isSubscribed
                )
            }
    }

    // MARK: agent-created record

    public func recordAgentCreated(_ record: AgentCreatedEvent) async {
        await log.record(record)
    }

    // MARK: snapshot

    public func snapshot(since: Date, until: Date, asOf: Date) async throws -> [CalendarMirrorEvent] {
        let directory = try await calendarDirectory()
        let mirrored = Set(
            directory.filter { $0.uploadsEvents }.map(\.calendarIdentifier)
        )
        let calendars = store.calendars(for: .event).filter {
            mirrored.contains($0.calendarIdentifier)
        }
        guard !calendars.isEmpty else { return [] }

        let predicate = store.predicateForEvents(
            withStart: since, end: until, calendars: calendars
        )
        let created = await log.all()

        var rows: [CalendarMirrorEvent] = []
        rows.reserveCapacity(store.events(matching: predicate).count)
        for event in store.events(matching: predicate) {
            // An event EventKit cannot name or place has never been in any
            // batch, so it has no mirror row to be tombstoned — skipping it
            // is safe in a way that skipping a describable event would not be.
            guard let eventIdentifier = event.eventIdentifier,
                  !eventIdentifier.isEmpty,
                  let owner = event.calendar?.calendarIdentifier
            else { continue }
            let source = CalendarMirrorSource(
                eventIdentifier: eventIdentifier,
                calendarIdentifier: owner,
                title: event.title,
                start: event.startDate,
                end: event.endDate,
                allDay: event.isAllDay,
                location: event.location,
                notes: event.notes,
                timeZoneIdentifier: event.timeZone?.identifier,
                lastModified: asOf
            )
            guard let row = CalendarMirrorRules.mirrorEvent(
                source, agentCreated: created[eventIdentifier], calendar: calendar
            ) else {
                throw CalendarSnapshotError.unrepresentable(eventID: eventIdentifier)
            }
            rows.append(row)
        }
        return rows
    }
}

public enum CalendarSnapshotError: Error, Equatable, Sendable {
    case accessDenied
    /// An event exists but cannot be described in the mirror wire. Fails the
    /// window on purpose — see `CalendarStore.snapshot`'s contract.
    case unrepresentable(eventID: String)
}

#endif
