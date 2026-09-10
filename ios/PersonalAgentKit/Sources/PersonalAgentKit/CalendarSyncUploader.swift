import Foundation

/// One RFC 3339 instant on the wire. The server's ingest schema declares
/// `format: date-time`, so the device sends what EventKit owns as absolute
/// instants, not local wall-clock text.
///
/// `Date` renders in UTC with a fixed formatter — no locale, no timezone
/// database — because a locale-dependent formatter would produce a string the
/// server's format checker may refuse, and a wall-clock string would change
/// meaning across a timezone change between upload attempts.
public enum RFC3339 {
    /// `ISO8601DateFormatter` is documented thread-safe for formatting once
    /// configured, but it is not `Sendable`, so access goes through this lock
    /// rather than hoping the warning is wrong.
    private static let lock = NSLock()
    private nonisolated(unsafe) static let _formatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()

    public static func string(from date: Date) -> String {
        lock.lock(); defer { lock.unlock() }
        return _formatter.string(from: date)
    }

    public static func parse(_ string: String) -> Date? {
        lock.lock(); defer { lock.unlock() }
        return _formatter.date(from: string)
    }
}

/// One mirror row, in the server's `calendar.ingest_events` item vocabulary.
///
/// The single encoder: the upload body and the batch's byte measurement are
/// the same bytes, or the budget would be measuring a shape nobody sends.
///
/// **Absent is a claim, and it is not the same claim as null.** The server
/// reads a *missing* `timezone` key as the v1 shape for that aspect and
/// accepts it (rendered as it always was), while an explicit null on a timed
/// event is refused. Same for `start_date`/`end_date`, and
/// `date_anchor_unknown` is documented as "省略视为未确认". So every v2 field
/// is inserted conditionally: `JSONSerialization` would happily write
/// `"timezone": null` for a nil, which is the one thing that must not happen.
///
/// The three `*_over_limit` flags and `created_by_agent` are booleans with
/// defined defaults; they go out only when true, so a row that is entirely
/// v1-shaped still encodes as one.
public enum CalendarMirrorWire {
    public static func event(_ event: CalendarMirrorEvent) -> [String: Any] {
        var fields: [String: Any] = [
            "event_identifier": event.eventIdentifier,
            "calendar_identifier": event.calendarIdentifier,
            "title": event.title as Any?,
            "start": RFC3339.string(from: event.start),
            "end": RFC3339.string(from: event.end),
            "all_day": event.allDay,
            "location": event.location as Any?,
            "notes": event.notes as Any?,
            "last_modified": RFC3339.string(from: event.lastModified),
        ]
        if event.createdByAgent { fields["created_by_agent"] = true }
        if let timeZone = event.timeZoneIdentifier { fields["timezone"] = timeZone }
        if let startDate = event.allDayStartDate { fields["start_date"] = startDate }
        if let endDate = event.allDayEndDate { fields["end_date"] = endDate }
        if event.dateAnchorUnknown { fields["date_anchor_unknown"] = true }
        if event.titleOverLimit { fields["title_over_limit"] = true }
        if event.locationOverLimit { fields["location_over_limit"] = true }
        if event.notesOverLimit { fields["notes_over_limit"] = true }
        return fields
    }

    public static func directoryEntry(
        _ entry: CalendarDirectoryEntry
    ) -> [String: Any] {
        var fields: [String: Any] = [
            "calendar_identifier": entry.calendarIdentifier,
            "title": entry.title,
            "allows_content_modifications": entry.allowsContentModifications,
            "is_subscribed": entry.isSubscribed,
        ]
        if let sourceTitle = entry.sourceTitle { fields["source_title"] = sourceTitle }
        return fields
    }

    /// Every row this encoder can build is made of strings, booleans and no
    /// optionals, so serialisation does not fail in practice. If it somehow
    /// did, the count must not be 0: under-counting is what puts a batch over
    /// the server's cap, so the fallback over-counts instead, using design
    /// §6's own bound for one event — title 200 + location 500 + notes 4096
    /// code points at ~3 bytes each ≈ 14 KiB, rounded up.
    static let maximumRowBytes = 16 * 1024

    /// The encoded size of one event's row. Measured through the real encoder
    /// rather than estimated from the text lengths: the row carries six
    /// timestamps, an id and the JSON punctuation, and an estimate that
    /// ignores them would under-count exactly when a batch is largest.
    static func encodedSize(of event: CalendarMirrorEvent) -> Int {
        guard let data = try? JSONSerialization.data(
            withJSONObject: Self.event(event)
        ) else { return maximumRowBytes }
        return data.count
    }
}

/// The device-side half of the calendar mirror (`POST /v1/calendar/sync`).
///
/// The iPhone is the fact source; the server holds a mirror it can query and
/// summarise. Upload semantics, in the server's own vocabulary:
///
/// - the whole window is declared up front (`window_start`/`window_end`);
/// - every event intersecting the window goes in **one or more whole batches**,
///   each ≤ 200 events (the server's schema limit);
/// - the **last** batch of a window sets `window_complete = true`, which
///   authorises the server to mark window events absent from the upload as
///   deleted. That flag is what makes the device the fact source — and it is
///   why a window is never split lazily: an upload that stops without the
///   complete flag leaves the mirror honestly stale, never falsely current.
///
/// The window itself (回看 90 天 / 前瞻 180 天) covers both past and future
/// events: summaries about 明天/本周 read the mirror, so future events must
/// mirror too.
public struct CalendarSyncUploader: Sendable {
    /// How far back the mirror reaches.
    public let lookbackDays: Int
    /// How far ahead it reaches.
    public let lookaheadDays: Int
    /// The server's per-batch limit; the schema refuses more.
    public let batchSize: Int
    /// The batch's byte budget. Whichever of the two bounds is reached first
    /// closes the batch (design §6).
    public let byteBudget: Int

    /// Sizes one event's wire row. Injected so the budget can be tested with
    /// synthetic rows instead of 16 KiB of real notes.
    private let measure: @Sendable (CalendarMirrorEvent) -> Int

    public init(
        lookbackDays: Int = 90,
        lookaheadDays: Int = 180,
        batchSize: Int = 200,
        byteBudget: Int = 128 * 1024
    ) {
        self.init(
            lookbackDays: lookbackDays, lookaheadDays: lookaheadDays,
            batchSize: batchSize, byteBudget: byteBudget,
            measure: CalendarMirrorWire.encodedSize
        )
    }

    /// The sizing rule is injectable so the budget can be tested by counting
    /// rows rather than by writing 128 KiB of real notes into a fixture. It is
    /// not part of the public API: production has exactly one honest answer to
    /// "how big is this row", and it is the encoder's.
    init(
        lookbackDays: Int = 90,
        lookaheadDays: Int = 180,
        batchSize: Int = 200,
        byteBudget: Int = 128 * 1024,
        measure: @escaping @Sendable (CalendarMirrorEvent) -> Int
    ) {
        self.lookbackDays = lookbackDays
        self.lookaheadDays = lookaheadDays
        self.batchSize = batchSize
        self.byteBudget = byteBudget
        self.measure = measure
    }

    /// One window's worth of events, chunked for upload. `last_batch` marks
    /// the chunk that carries `window_complete`.
    ///
    /// A batch closes when either bound is reached, checked *before* the next
    /// event is added, so no batch exceeds either. A single event larger than
    /// the whole budget still goes out alone rather than being dropped: the
    /// schema's per-field maximum puts one row near 14 KiB against a 128 KiB
    /// budget, so this is unreachable in practice, and if it ever were
    /// reachable the server's refusal is a better answer than a silently
    /// missing event.
    public func chunk(
        _ events: [CalendarMirrorEvent], now: Date
    ) -> [(windowStart: Date, windowEnd: Date, events: [CalendarMirrorEvent], lastBatch: Bool)] {
        let windowStart = now.addingTimeInterval(Double(-lookbackDays) * 86_400)
        let windowEnd = now.addingTimeInterval(Double(lookaheadDays) * 86_400)
        guard !events.isEmpty else {
            // An empty window still gets one complete batch: the device has
            // seen the window and says there is nothing in it. That is a fact
            // the server may act on (mark everything in the window deleted),
            // not an absence of evidence.
            return [(windowStart, windowEnd, [], true)]
        }
        var groups: [[CalendarMirrorEvent]] = []
        var current: [CalendarMirrorEvent] = []
        // The separators and brackets of the row array, plus the enclosing
        // object's four fixed fields and the directory. A few hundred bytes
        // against a budget with 4× headroom below the endpoint cap — this is
        // a bound on a batch, not a computation of an HTTP body length.
        var currentBytes = 64
        for event in events {
            let size = measure(event)
            if !current.isEmpty, current.count >= batchSize || currentBytes + size > byteBudget {
                groups.append(current)
                current = []
                currentBytes = 64
            }
            current.append(event)
            currentBytes += size + 1
        }
        if !current.isEmpty { groups.append(current) }

        return groups.enumerated().map { index, group in
            (windowStart, windowEnd, group, index == groups.count - 1)
        }
    }
}

/// A mirror sync the send path may briefly wait on, bounded (review F8,
/// second round; fixed in the third round, G2).
///
/// The composition starts the sync on its own task. `wait()` gives it a
/// budget; past the budget the sender proceeds and the sync keeps running —
/// a permission prompt, a slow network or a multi-batch upload never blocks
/// an unrelated message.
///
/// Implementation note (G2): the wait must be a **one-way race between the
/// timer and the sync's completion** — never a task group holding both. A
/// task group waits for *all* its children before returning, and
/// `cancelAll()` cannot interrupt a child that is `await`-ing an
/// unstructured task's `value`, so the "budget" of the first implementation
/// silently waited for the full sync (reproduced: 50 ms budget, 900 ms sync,
/// ~900 ms wait). Here only the timer runs inside the group; the sync task
/// lives outside it and signals completion through a one-shot gate, so the
/// group returns the moment either the gate opens or the timer fires.
public struct MirrorSyncHandle: Sendable {
    private let task: Task<Void, Never>
    private let budget: Duration

    public init(
        run: @escaping @Sendable () async -> Void, budget: Duration = .seconds(2)
    ) {
        self.budget = budget
        self.task = Task { await run() }
    }

    /// A handle for a sync that already finished (tests and no-op paths).
    public static func done() -> MirrorSyncHandle {
        MirrorSyncHandle(run: {}, budget: .zero)
    }

    /// Wait for the sync, but no longer than the budget. A timeout is not an
    /// error and does not cancel the sync: it keeps running in the
    /// background, and the next send's staleness check (or the refresh
    /// trigger) reaps its result through the engine's own marker.
    public func wait() async {
        // The gate opens exactly once, whichever side wins; the other side's
        // late arrival is a no-op resume on an already-finished continuation.
        let gate = Gate()
        let timer = Task {
            try? await Task.sleep(for: budget)
            gate.open()
        }
        let observer = Task { [task] in
            await task.value
            gate.open()
        }
        await gate.wait()
        timer.cancel()
        observer.cancel()
    }
}

/// A one-shot, one-waiter gate. Resuming a finished continuation more than
/// once traps, so the box guards the transition with a lock and tolerates
/// the losing side's arrival after the winner already woke the waiter.
private final class Gate: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Void, Never>?
    private var opened = false

    func open() {
        let toResume: CheckedContinuation<Void, Never>? = lock.withLock {
            guard !opened else { return nil }
            opened = true
            let pending = continuation
            continuation = nil
            return pending
        }
        toResume?.resume()
    }

    func wait() async {
        // The continuation's body runs synchronously *before* the suspend:
        // it either finds the gate already open and resumes immediately, or
        // registers itself for the first `open()`. Both paths hold the lock,
        // so a racing `open()` can never miss a registered waiter and never
        // resumes twice.
        await withCheckedContinuation { cont in
            let toResumeNow = lock.withLock { () -> Bool in
                if opened { return true }
                continuation = cont
                return false
            }
            if toResumeNow {
                cont.resume()
            }
        }
    }
}
/// snapshot the whole window, upload it in batches, and stamp the durable
/// marker **only** when the last batch — the one carrying
/// `window_complete` — was accepted. Composed into `AppModel` and triggered
/// from the foreground path and before chat sends that read the mirror.
///
/// The failure posture is "degrade, never lie":
///
/// - a snapshot that throws uploads **nothing**. Uploading an empty complete
///   batch after a failed snapshot would tell the server "the user's calendar
///   emptied out", which is a false deletion of the whole mirror window —
///   the one error this engine must be incapable of.
/// - a batch that throws leaves the marker at its previous value: the server's
///   `mirror_stale` is what then answers 「数据截至」 honestly, and the next
///   attempt re-runs the whole window (the window derives from `now`, not
///   from the marker, so a partially-uploaded window is restarted in full —
///   `window_complete` is what makes a window authoritative, and it exists
///   only at the end of a full pass).
/// - callers that cannot sync (no store access, offline) surface the failure
///   to their own degrade path; the engine never swallows into a fake success.
///
/// Concurrency: the engine is an actor, so overlapping triggers coalesce on
/// an in-flight flag rather than racing two full windows.
public actor CalendarMirrorSyncEngine {
    private let store: any CalendarStore
    private let backend: any ChatBackend
    private let storage: CredentialStore
    private let now: @Sendable () -> Date
    private let stalenessThreshold: TimeInterval
    private let uploader: CalendarSyncUploader
    private var inFlight = false

    public init(
        store: any CalendarStore,
        backend: any ChatBackend,
        storage: CredentialStore,
        uploader: CalendarSyncUploader = CalendarSyncUploader(),
        now: @escaping @Sendable () -> Date = { Date() },
        stalenessThreshold: TimeInterval = 25 * 3600
    ) {
        self.store = store
        self.backend = backend
        self.storage = storage
        self.uploader = uploader
        self.now = now
        // The server answers `mirror_stale` past 25h; syncing before that is
        // wasted upload, syncing after it is required for honest「数据截至」.
        self.stalenessThreshold = stalenessThreshold
    }

    /// Sync when the marker is older than the threshold (or absent). Returns
    /// whether a sync actually ran; throws what the snapshot or an upload
    /// threw, for the caller's degrade path.
    @discardableResult
    public func syncIfNeeded() async throws -> Bool {
        if inFlight { return false }
        guard try Self.isStale(storage: storage, now: now(), threshold: stalenessThreshold) else {
            return false
        }
        inFlight = true
        defer { inFlight = false }

        let instant = now()
        let events = try await store.snapshot(
            since: instant.addingTimeInterval(Double(-uploader.lookbackDays) * 86_400),
            until: instant.addingTimeInterval(Double(uploader.lookaheadDays) * 86_400),
            asOf: instant
        )
        // `asOf` is the device's stamp of vouching: the batch, not the row, is
        // what the upsert arbitrates on (CalendarStore.snapshot's contract),
        // and it is the *version* every batch of this window shares — the
        // server's schema requires it on the wire (second review F1).
        // The directory is read once and rides on every batch (§2.1): the
        // server resolves a calendar name to an EventKit identifier against
        // it before issuing a create, so a batch that carried events but no
        // directory would leave the issuance gate unable to resolve anything
        // until the next sync. Failure here fails the window exactly as a
        // snapshot failure does — an upload without it is a half-truth, and
        // re-running the whole window is how the engine already recovers.
        let directory = try await store.calendarDirectory()
        var lastError: Error?
        for chunk in uploader.chunk(events, now: instant) where lastError == nil {
            do {
                _ = try await backend.uploadCalendarSync(
                    windowStart: chunk.windowStart,
                    windowEnd: chunk.windowEnd,
                    events: chunk.events,
                    calendars: directory,
                    windowComplete: chunk.lastBatch,
                    snapshotAsOf: instant
                )
            } catch {
                lastError = error
            }
        }
        if let lastError { throw lastError }
        // Only here: every batch — including window_complete — was accepted.
        let encoded = String(instant.timeIntervalSince1970).data(using: .utf8)!
        try storage.write(CredentialKey.calendarMirrorSyncedAt, value: encoded)
        return true
    }

    /// Read the durable marker and decide staleness. A missing or unreadable
    /// marker reads as stale — a first sync must run, and an unparseable one
    /// is exactly the shape that must not silently disable syncing forever.
    private static func isStale(
        storage: CredentialStore, now: Date, threshold: TimeInterval
    ) throws -> Bool {
        guard let data = try storage.read(CredentialKey.calendarMirrorSyncedAt),
              let text = String(data: data, encoding: .utf8),
              let interval = Double(text) else {
            return true
        }
        return now.timeIntervalSince1970 - interval > threshold
    }
}
