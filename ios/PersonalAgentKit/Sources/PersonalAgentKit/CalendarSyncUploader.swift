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
    private let onBudgetExpired: (@Sendable () async -> Void)?

    /// - Parameter onBudgetExpired: fired when, and only when, the wait ended
    ///   because the budget ran out with the sync still running. This is
    ///   §9.1's fourth trigger, and it is the *race* that defines it — not any
    ///   state the caller can read afterwards. An implementation that instead
    ///   asked the engine "is a pass still in flight?" after the wait would be
    ///   wrong in both directions: the pass may have finished in the gap (no
    ///   trigger happened, yet the question answers yes if asked too early),
    ///   and the losing side of the race is exactly the case the question
    ///   cannot recover.
    public init(
        run: @escaping @Sendable () async -> Void,
        budget: Duration = .seconds(2),
        onBudgetExpired: (@Sendable () async -> Void)? = nil
    ) {
        self.budget = budget
        self.onBudgetExpired = onBudgetExpired
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
            gate.open(.budget)
        }
        let observer = Task { [task] in
            await task.value
            gate.open(.finished)
        }
        let winner = await gate.wait()
        timer.cancel()
        observer.cancel()
        if winner == .budget, let onBudgetExpired {
            await onBudgetExpired()
        }
    }
}

/// Which side of the wait opened the gate. `finished` means the sync completed
/// inside the budget; `budget` means the wait gave up while it kept running.
private enum GateOutcome: Sendable {
    case finished
    case budget
}

/// A one-shot, one-waiter gate. Resuming a finished continuation more than
/// once traps, so the box guards the transition with a lock and tolerates
/// the losing side's arrival after the winner already woke the waiter.
private final class Gate: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<GateOutcome, Never>?
    private var outcome: GateOutcome?

    /// Records which side won, once. A late `open()` from the losing side is a
    /// no-op: the winner's outcome is already stored and its waiter already
    /// resumed, so the loser cannot overwrite the answer.
    func open(_ outcome: GateOutcome) {
        let toResume: CheckedContinuation<GateOutcome, Never>? = lock.withLock {
            guard self.outcome == nil else { return nil }
            self.outcome = outcome
            let pending = continuation
            continuation = nil
            return pending
        }
        toResume?.resume(returning: outcome)
    }

    func wait() async -> GateOutcome {
        // The continuation's body runs synchronously *before* the suspend:
        // it either finds the gate already open and resumes immediately, or
        // registers itself for the first `open()`. Both paths hold the lock,
        // so a racing `open()` can never miss a registered waiter and never
        // resumes twice.
        await withCheckedContinuation { cont in
            let decided = lock.withLock { () -> GateOutcome? in
                if let outcome { return outcome }
                continuation = cont
                return nil
            }
            if let decided {
                cont.resume(returning: decided)
            }
        }
    }
}

/// Why a sync is being asked for (design §9.1).
///
/// The distinction exists because "is the marker stale?" is the wrong question
/// for a change the user just made. Asked seconds after a create, it answers
/// *no* — so the pass is skipped, the mirror still lacks the event, and the
/// query the user makes next reads it back to them as missing.
public enum SyncReason: Sendable {
    /// The standing question: "has the marker gone stale?" — the foreground
    /// return, pull-to-refresh, and the pre-send budget.
    case gated
    /// "Something changed the calendar; upload now." Bypasses the staleness
    /// gate. The triggers are §9.1's: a successful device-action report, and
    /// `EKEventStoreChanged`.
    case forced
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
///
/// Design §9.1 adds two things on top of that. The first is a **reason**: a
/// change the user just made cannot wait for the marker to go stale, so
/// `.forced` bypasses the gate (§9.1's problem statement). The second is the
/// pair of persisted change sequences — because the marker answers "when may I
/// re-upload", which is *not* the same question as "is the query result I am
/// about to show missing something", and the user is owed an answer to the
/// second one.
public actor CalendarMirrorSyncEngine {
    private let store: any CalendarStore
    private let backend: any ChatBackend
    private let storage: CredentialStore
    private let now: @Sendable () -> Date
    private let stalenessThreshold: TimeInterval
    private let uploader: CalendarSyncUploader
    private var inFlight = false
    /// A `.forced` trigger arrived while a pass was running. That trigger is a
    /// change the running pass did not cover — it took its snapshot before the
    /// trigger existed — so a further pass is owed. Cleared when that pass
    /// starts, never after it ends: clearing afterwards would let the pass's own
    /// completion re-arm it, which is the spin §9.1 rules out.
    private var rerunRequested = false
    /// The single runner `armCalendarChanged` starts. Non-nil is what makes the
    /// next arm a bump without a second pass.
    private var passTask: Task<Void, Never>?

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

    /// Sync, saying *why*. Returns whether a pass actually ran; throws what the
    /// snapshot or an upload threw, for the caller's degrade path.
    ///
    /// Design §9.1. The reason is the whole point: the old engine answered one
    /// question — "is the marker old enough?" — and that question is wrong for
    /// the triggers §9 adds. Just after a create the marker is seconds old, so
    /// the staleness gate would skip the pass and the query the user makes next
    /// would read a mirror that does not contain what they just wrote.
    ///
    /// - `.gated` is the standing behaviour: foreground, refresh, and the
    ///   pre-send budget all ask "re-upload if the marker has gone stale".
    /// - `.forced` says "something the user did changed the calendar; upload
    ///   now". It bypasses the staleness gate, and if a pass is already
    ///   running it does not start a second one — it records that another is
    ///   owed, and that pass runs the moment this one finishes.
    ///
    /// The rerun flag is consumed **before** the pass it arms starts, never
    /// after. That is what keeps this from spinning: only a trigger that
    /// arrives *during* a pass can arm the next one, so every pass corresponds
    /// to a real change instead of to the flag it just cleared.
    @discardableResult
    public func sync(reason: SyncReason) async throws -> Bool {
        if inFlight {
            // A pass is running. `.gated` has nothing to add — it is asking the
            // same question the running pass is already answering. `.forced` is
            // a change that arrived after the running pass took its snapshot,
            // which is exactly what "this pass did not cover it" means.
            if reason == .forced { rerunRequested = true }
            return false
        }
        // An owed pass is taken here, before the gate, and it also overrides
        // the caller's reason. It exists only because something changed after
        // the last pass took its snapshot, so letting the staleness gate turn
        // it away would strand it: the mirror would be missing a change, the
        // EventKit notification that armed it has already been delivered so no
        // second one is coming, and `knownUnsynced` would stay true with
        // nothing left able to clear it.
        let owed = rerunRequested
        rerunRequested = false
        if reason == .gated, !owed {
            guard try Self.isStale(
                storage: storage, now: now(), threshold: stalenessThreshold
            ) else {
                return false
            }
        }
        inFlight = true
        defer { inFlight = false }

        try await runPass(reason: owed ? .forced : reason)
        // One pass now, plus at most one more. §9.1 bounds a trigger storm at
        // two: the N device-action reports of a multi-action reply, each a
        // `.forced` trigger, collapse into this single follow-up. If a trigger
        // arrives during the *follow-up*, its flag is left armed for the next
        // call rather than looped on here — so a pass can never chase its own
        // tail, and the change it stands for is still covered.
        if rerunRequested {
            rerunRequested = false
            try await runPass(reason: .forced)
        }
        return true
    }

    /// Sync when the marker is older than the threshold (or absent).
    ///
    /// The `.gated` entry point, named for the callers that mean exactly that:
    /// the foreground return, the refresh control, and the pre-send budget.
    @discardableResult
    public func syncIfNeeded() async throws -> Bool {
        try await sync(reason: .gated)
    }

    /// One whole window: snapshot, upload every batch, and settle the two
    /// change sequences. Throws what the snapshot or an upload threw.
    private func runPass(reason: SyncReason) async throws {
        // Captured **before** the fetch, and this ordering is the whole
        // mechanism: the snapshot's content is the world as of this number. A
        // change that lands after it belongs to the next pass, and a pass that
        // claimed it would mark uploaded rows it never saw (review R3-F13).
        let capturedSeq = try lastChangeSeq()
        let instant = now()
        let events: [CalendarMirrorEvent]
        let directory: [CalendarDirectoryEntry]
        do {
            events = try await store.snapshot(
                since: instant.addingTimeInterval(Double(-uploader.lookbackDays) * 86_400),
                until: instant.addingTimeInterval(Double(uploader.lookaheadDays) * 86_400),
                asOf: instant
            )
            // `asOf` is the device's stamp of vouching: the batch, not the row,
            // is what the upsert arbitrates on (CalendarStore.snapshot's
            // contract), and it is the *version* every batch of this window
            // shares — the server's schema requires it on the wire (second
            // review F1).
            // The directory is read once and rides on every batch (§2.1): the
            // server resolves a calendar name to an EventKit identifier against
            // it before issuing a create, so a batch that carried events but no
            // directory would leave the issuance gate unable to resolve
            // anything until the next sync. Failure here fails the window
            // exactly as a snapshot failure does — an upload without it is a
            // half-truth, and re-running the whole window is how the engine
            // already recovers.
            directory = try await store.calendarDirectory()
        } catch {
            // Nothing was uploaded, so a forced pass has nothing to show for
            // itself and drops the marker for the same reason a failed batch
            // does below. The sequence is deliberately *not* bumped: a read
            // failure is not evidence that the calendar changed, and a revoked
            // calendar permission never stops failing — bumping here would
            // leave the warning permanently true with no pass able to clear it.
            if reason == .forced { try? storage.delete(CredentialKey.calendarMirrorSyncedAt) }
            throw error
        }
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
        if let lastError {
            if reason == .forced {
                // §9.1: a forced pass that failed covers none of the sequence it
                // captured, so the device still cannot vouch for the mirror.
                try? bumpChangeSeq()
                // The marker is a different question — "when may I re-upload?"
                // — and deleting it is what re-arms the staleness gate so that
                // both `.gated` and `.forced` really do upload next time. The
                // truthfulness of the query result is `knownUnsynced`'s job,
                // not the marker's, so the two are set independently.
                try? storage.delete(CredentialKey.calendarMirrorSyncedAt)
            }
            throw lastError
        }
        // Only here: every batch — including window_complete — was accepted.
        let encoded = String(instant.timeIntervalSince1970).data(using: .utf8)!
        try storage.write(CredentialKey.calendarMirrorSyncedAt, value: encoded)
        // Confirmed up to the captured number, never beyond it: a change that
        // arrived mid-upload (B during A) was not in this snapshot, so A's
        // success confirms only what A actually saw. `knownUnsynced` stays true
        // on the comparison below — that is R3-F13's counterexample, and it is
        // the *intended* outcome.
        try writeSeq(
            max(try syncedChangeSeq(), capturedSeq), CredentialKey.calendarSyncedChangeSeq
        )
        // No arming here for a change that arrived mid-pass: both `note…`
        // methods bump and *then* ask for the pass that will cover it in the
        // same call, so the owed pass is already armed by the trigger. A bump
        // with nothing behind it cannot happen, and code for it would be a
        // mechanism no test could reach.
    }

    // --- the change sequence (design §9.1, review R3-F13) --------------------

    /// How much the local calendar has moved, as far as this device knows.
    ///
    /// Read from storage on every use rather than cached in the actor. "The app
    /// was killed and restarted" is a first-class case here — R3-F13 requires
    /// the persisted number to still say dirty — and a fresh engine over the
    /// same store is exactly how the offline tests model it.
    private func lastChangeSeq() throws -> Int {
        try readSeq(CredentialKey.calendarChangeSeq)
    }

    /// How much of that movement a completed window has confirmed.
    private func syncedChangeSeq() throws -> Int {
        try readSeq(CredentialKey.calendarSyncedChangeSeq)
    }

    /// Design §9.1's increment, available to clients through the `note…`
    /// methods below. Monotonic: it only ever adds one.
    private func bumpChangeSeq() throws {
        try writeSeq(try lastChangeSeq() + 1, CredentialKey.calendarChangeSeq)
    }

    /// A missing or unreadable sequence reads as 0. Both counters missing is the
    /// fresh-install shape, where there is nothing unsynced to warn about, and
    /// that is the same posture this engine had before it kept sequences at all
    /// — a degraded read falls back to the previous behaviour rather than
    /// inventing a warning that no pass could clear.
    private func readSeq(_ key: String) throws -> Int {
        guard let data = try storage.read(key),
              let text = String(data: data, encoding: .utf8),
              let value = Int(text) else { return 0 }
        return value
    }

    private func writeSeq(_ value: Int, _ key: String) throws {
        try storage.write(key, value: Data(String(value).utf8))
    }

    // --- §9.1's increment triggers -------------------------------------------

    /// The calendar moved: `EKEventStoreChanged`, or a device-action report
    /// that succeeded (a self-created event is a calendar change too).
    ///
    /// Bumps first, then runs a forced pass — the two are one operation on
    /// purpose, because a caller that bumped without syncing would leave a
    /// warning that nothing was going to clear.
    @discardableResult
    public func noteCalendarChanged() async throws -> Bool {
        try bumpChangeSeq()
        return try await sync(reason: .forced)
    }

    /// Record that the calendar changed and start the pass that covers it,
    /// without waiting for the upload (design §9.1's second `.forced` trigger,
    /// reached from the device-action report).
    ///
    /// The split from `noteCalendarChanged` is the whole point. §9.1 requires
    /// the change to be **armed** when the query window opens — `knownUnsynced`
    /// true, and a pass running that will clear it — and says nothing about the
    /// *upload* having finished. Awaiting the pass instead made one reply's N
    /// reports block the receipt on N uploads of the whole window, so a user
    /// watching a write land waited on a mirror sync that had nothing to do
    /// with it.
    ///
    /// The bump is committed before this call returns, so the armed state is a
    /// guarantee rather than a race with a `Task` the caller did not await.
    /// Failures inside the pass are swallowed here: there is no caller left to
    /// hand them to, and the engine already records the state a failure leaves
    /// behind (`runPass` drops the marker and keeps the sequence dirty, so the
    /// next `.gated` trigger retries and the query stays honest meanwhile).
    public func armCalendarChanged() throws {
        try bumpChangeSeq()
        // Load-bearing, not decorative: a pass that is already running has
        // taken its snapshot, so this bump is a change it did not see and one
        // more pass is owed for it. `rerunRequested` is the engine's existing
        // word for exactly that, and the running pass reads it on its way out
        // (`noteCalendarChanged` gets this for free by awaiting `sync`; the arm
        // does not, so it says so itself).
        rerunRequested = true
        // One runner, ever. N reports arriving as N calls must not become N
        // concurrent passes over the same window; the first call starts the
        // runner and the rest only leave the bump and the flag behind.
        guard passTask == nil else { return }
        passTask = Task { await self.runArmedPasses() }
    }

    /// The armed passes, drained one after another until nothing is owed.
    ///
    /// `sync` deliberately leaves `rerunRequested` armed when a trigger arrives
    /// during its follow-up pass rather than looping on it, because it assumes a
    /// caller will come back. With the send path no longer waiting, this runner
    /// is that caller — and if it is not, the flag would stay armed with no pass
    /// behind it, leaving `knownUnsynced` true forever with nothing able to
    /// clear it.
    private func runArmedPasses() async {
        while true {
            try? await sync(reason: .forced)
            // Each iteration is backed by a trigger that really happened during
            // the previous pass, so this cannot chase its own tail: a pass that
            // is not followed by another change leaves the flag down and the
            // loop ends.
            guard rerunRequested else { break }
        }
        passTask = nil
    }

    /// The pre-send budget ran out with the sync still running (design §9.1's
    /// fourth trigger).
    ///
    /// The wait was a ceiling, not a cancellation, so the pass is still going —
    /// but it took its snapshot before this point, so its success will confirm
    /// only the sequence it captured and the mirror may be missing whatever
    /// prompted the send. Recording that is the whole job.
    ///
    /// Guarded on `inFlight` because the trigger is defined by the *wait* having
    /// expired: if the pass already finished, the budget was not really
    /// exhausted, the pass confirmed everything, and a bump here would be a
    /// warning about nothing.
    public func noteSyncBudgetExhausted() throws {
        guard inFlight else { return }
        try bumpChangeSeq()
        // The running pass captured the sequence before this bump, so on
        // success it confirms a number below it. Arming the owed pass is what
        // makes the warning clearable — without it, no EventKit notification is
        // coming (nothing changed after this), and `knownUnsynced` would
        // outlive the truth it describes.
        rerunRequested = true
    }

    /// Design §9.1's client-local query gate: `knownUnsynced ≡ lastChangeSeq >
    /// syncedChangeSeq`, from the persisted numbers.
    ///
    /// Deliberately not reported to the server. `mirror_stale` is the server's
    /// own statement about its own watermark and stays independent; this is the
    /// device's statement about what it knows it has not uploaded. The two can
    /// both be true and neither replaces the other.
    public var knownUnsynced: Bool {
        get throws {
            let last = try lastChangeSeq()
            return last > (try syncedChangeSeq())
        }
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
