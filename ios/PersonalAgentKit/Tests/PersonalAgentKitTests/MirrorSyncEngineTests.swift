import Foundation
import Testing
@testable import PersonalAgentKit

// Review R5 (2026-09-08): the mirror-sync engine existed in pieces —
// `CalendarSyncUploader.chunk`, `EventKitCalendarStore.snapshot`,
// `DeviceSession.uploadCalendarSync` — but nothing in production ever called
// them together, so the mirror never synced and every calendar query answered
// 「数据截至」 with a mirror that was empty forever. These tests pin the engine
// that composes them, including the §5.1 failure shapes: a snapshot that
// fails must never be papered over with an empty complete batch (that would
// erase the mirror), and a failed upload must leave the marker where it was
// so the server's own `mirror_stale` keeps answering honestly.

/// A calendar store stub: hands back what the test set, or throws what it
/// was told to throw.
private struct StubMirrorCalendarStore: CalendarStore {
    var events: [CalendarMirrorEvent]
    var error: Error?
    var directory: [CalendarDirectoryEntry] = []
    var directoryError: Error?

    func save(_ draft: CalendarEventDraft) async -> CalendarSaveOutcome {
        .failed(detail: "not used in these tests")
    }

    func calendarDirectory() async throws -> [CalendarDirectoryEntry] {
        if let directoryError { throw directoryError }
        return directory
    }

    func recordAgentCreated(_ record: AgentCreatedEvent) async {}

    func snapshot(since: Date, until: Date, asOf: Date) async throws -> [CalendarMirrorEvent] {
        if let error { throw error }
        return events
    }
}

/// A backend stub recording the batches it was asked to upload, with an
/// optional "lose the network after N batches" failure shape.
private final class StubMirrorBackend: ChatBackend, @unchecked Sendable {
    private let lock = NSLock()
    private var _uploads: [(windowStart: Date, windowEnd: Date, events: [CalendarMirrorEvent], calendars: [CalendarDirectoryEntry], windowComplete: Bool)] = []
    var uploads: [(Date, Date, [CalendarMirrorEvent], Bool)] {
        lock.withLock { _uploads.map { ($0.windowStart, $0.windowEnd, $0.events, $0.windowComplete) } }
    }
    /// The directory each batch carried, in upload order.
    var uploadedDirectories: [[CalendarDirectoryEntry]] {
        lock.withLock { _uploads.map(\.calendars) }
    }
    /// When > 0, batch number `failAfterBatches` (1-based) throws.
    var failAfterBatches = 0

    func uploadCalendarSync(
        windowStart: Date, windowEnd: Date, events: [CalendarMirrorEvent],
        calendars: [CalendarDirectoryEntry],
        windowComplete: Bool, snapshotAsOf: Date, syncEpoch: Int
    ) async throws -> CalendarSyncResponse {
        let (shouldFail, index) = lock.withLock {
            _uploads.append((windowStart, windowEnd, events, calendars, windowComplete))
            return (failAfterBatches > 0 && _uploads.count == failAfterBatches, _uploads.count)
        }
        if let onBatch { await onBatch(index) }
        if shouldFail {
            throw AgentClientError.transport("network lost mid-window")
        }
        return CalendarSyncResponse(status: "ok", upserted: events.count, skipped: 0, markedDeleted: 0, syncEpoch: syncEpoch)
    }

    /// Fired after the Nth batch (1-based) is recorded, *outside* the lock, so a
    /// test can make something happen in the middle of a pass — a change
    /// arriving while the upload is in flight (design §9.1) — and can observe
    /// the engine from a second task while the pass is suspended.
    var onBatch: (@Sendable (Int) async -> Void)?

    // The rest of the protocol is not this engine's business.
    func sendChatMessage(
        conversationID: String, text: String, clarificationOf: String?,
        startNewSession: Bool, idempotencyKey: String
    ) async throws -> OperationReceipt {
        throw AgentClientError.transport("unused")
    }
    func operation(operationID: String) async throws -> OperationReceipt { throw AgentClientError.transport("unused") }
    func operation(idempotencyKey: String) async throws -> OperationReceipt { throw AgentClientError.transport("unused") }
    func cancelOperation(operationID: String) async throws -> OperationReceipt { throw AgentClientError.transport("unused") }
    func timelinePage(conversationID: String, cursor: String?, direction: TimelineDirection, limit: Int?) async throws -> TimelinePageResponse { throw AgentClientError.transport("unused") }
    func decideDuplicate(checkID: String, decision: DuplicateDecision, idempotencyKey: String) async throws -> OperationReceipt { throw AgentClientError.transport("unused") }
    func resolveManualReview(operationID: String, resolution: ManualResolution) async throws -> ManualResolutionReceipt { throw AgentClientError.transport("unused") }
    func updateExpenseCategory(recordID: String, category: String, expectedCurrentCategory: String?, idempotencyKey: String) async throws -> OperationReceipt { throw AgentClientError.transport("unused") }
    func reportDeviceActionResult(actionID: String, body: DeviceActionResultBody) async throws -> OperationReceipt { throw AgentClientError.transport("unused") }
    func overrideDeviceAction(actionID: String) async throws -> OperationReceipt { throw AgentClientError.transport("unused") }
}

private let t0 = Date(timeIntervalSince1970: 1_783_000_000)

/// The marker's wire format is a UTF-8 `TimeInterval` string, so the tests
/// read it back through the same decode the engine writes — one helper so a
/// format change fails here first.
private func markerDate(_ data: Data) -> Date? {
    Double(String(data: data, encoding: .utf8) ?? "").map { Date(timeIntervalSince1970: $0) }
}

/// `try #require(storage.read(...))` trips the Testing macro's closure
/// expansion ("call can throw, not marked try"), so the read-and-unpack goes
/// through a plain throwing helper instead — a nil marker fails the test.
private func readMarker(_ storage: CredentialStore) throws -> Data {
    guard let data = try storage.read(CredentialKey.calendarMirrorSyncedAt) else {
        throw CalendarMarkerMissing()
    }
    return data
}

private struct CalendarMarkerMissing: Error {}

/// A counter a callback can bump from whatever task it runs on, read after.
private final class CounterBox: @unchecked Sendable {
    private let lock = NSLock()
    private var value = 0
    var count: Int { lock.withLock { value } }
    func bump() { lock.withLock { value += 1 } }
}

private func mirrorEvent(_ id: String, offset: TimeInterval) -> CalendarMirrorEvent {
    CalendarMirrorEvent(
        eventIdentifier: id, calendarIdentifier: "CAL-1", title: id,
        start: t0.addingTimeInterval(offset), end: t0.addingTimeInterval(offset + 3600),
        allDay: false, location: nil, notes: nil,
        lastModified: t0.addingTimeInterval(offset)
    )
}

@Suite("Calendar mirror sync engine", .serialized)
struct MirrorSyncEngineTests {

    private func makeEngine(
        store: CalendarStore,
        backend: ChatBackend,
        marker: Date? = nil,
        now: Date = t0,
        threshold: TimeInterval = 3600,
        batchSize: Int = 200
    ) throws -> (CalendarMirrorSyncEngine, InMemoryCredentialStore) {
        let storage = InMemoryCredentialStore()
        if let marker {
            let encoded = String(marker.timeIntervalSince1970).data(using: .utf8)!
            try storage.write(CredentialKey.calendarMirrorSyncedAt, value: encoded)
        }
        let engine = CalendarMirrorSyncEngine(
            store: store, backend: backend, storage: storage,
            uploader: CalendarSyncUploader(batchSize: batchSize),
            now: { now }, stalenessThreshold: threshold
        )
        return (engine, storage)
    }

    @Test("a stale mirror syncs the whole window and stamps the marker after the last batch")
    func fullWindowSync() async throws {
        let store = StubMirrorCalendarStore(
            events: [mirrorEvent("EK-1", offset: 0), mirrorEvent("EK-2", offset: 100)]
        )
        let backend = StubMirrorBackend()
        let (engine, storage) = try makeEngine(
            store: store, backend: backend, marker: t0.addingTimeInterval(-7200)
        )

        let synced = try await engine.syncIfNeeded()

        #expect(synced == true)
        let uploads = backend.uploads
        #expect(uploads.count == 1)
        #expect(uploads[0].3 == true)
        #expect(uploads[0].2.count == 2)
        // The marker is the last batch's *completion*, read back from storage.
        let stamped = try readMarker(storage)
        let marker = try #require(markerDate(stamped))
        #expect(marker == t0)
    }

    @Test("a fresh marker skips the sync entirely")
    func freshMarkerSkips() async throws {
        let store = StubMirrorCalendarStore(events: [mirrorEvent("EK-1", offset: 0)])
        let backend = StubMirrorBackend()
        let (engine, storage) = try makeEngine(store: store, backend: backend, marker: t0)

        let synced = try await engine.syncIfNeeded()

        #expect(synced == false)
        #expect(backend.uploads.isEmpty)
        let stamped = try readMarker(storage)
        #expect(markerDate(stamped) == t0)
    }

    @Test("no marker at all is stale: the first sync runs")
    func noMarkerRuns() async throws {
        let store = StubMirrorCalendarStore(events: [])
        let backend = StubMirrorBackend()
        let (engine, storage) = try makeEngine(store: store, backend: backend, marker: nil)

        let synced = try await engine.syncIfNeeded()

        #expect(synced == true)
        #expect(backend.uploads.count == 1)
        #expect(backend.uploads[0].2.isEmpty, "an empty window is a fact, still uploaded complete")
        #expect(backend.uploads[0].3 == true)
        let stamped = try readMarker(storage)
        #expect(markerDate(stamped) == t0)
    }

    @Test("a failing snapshot uploads nothing — never an empty complete batch that erases the mirror")
    func failingSnapshotUploadsNothing() async throws {
        let store = StubMirrorCalendarStore(events: [], error: CalendarSnapshotError.accessDenied)
        let backend = StubMirrorBackend()
        let before = t0.addingTimeInterval(-7200)
        let (engine, storage) = try makeEngine(store: store, backend: backend, marker: before)

        do {
            _ = try await engine.syncIfNeeded()
            Issue.record("a snapshot failure must surface, not vanish")
        } catch {
            // The caller degrades; the marker must not have moved and nothing
            // must have been uploaded.
        }
        #expect(backend.uploads.isEmpty)
        let stamped = try readMarker(storage)
        #expect(markerDate(stamped) == before)
    }

    @Test("a mid-window batch failure leaves the marker at its old value and the whole window restarts next time")
    func midWindowFailureKeepsOldMarker() async throws {
        let store = StubMirrorCalendarStore(
            events: (0..<5).map { mirrorEvent("EK-\($0)", offset: Double($0)) }
        )
        let backend = StubMirrorBackend()
        backend.failAfterBatches = 2
        let before = t0.addingTimeInterval(-7200)
        let (engine, storage) = try makeEngine(
            store: store, backend: backend, marker: before, threshold: 0, batchSize: 2
        )

        do {
            _ = try await engine.syncIfNeeded()
            Issue.record("an upload failure must surface, not vanish")
        } catch {}
        // Batch 1 landed; the attempt at batch 2 threw (failAfterBatches=2
        // means the second attempt throws), so `window_complete` never
        // travelled. The marker must NOT advance: the mirror is honestly
        // stale, and both attempts are recorded on the stub.
        #expect(backend.uploads.count == 2)
        let stamped = try readMarker(storage)
        #expect(markerDate(stamped) == before)

        // Next attempt re-runs the *whole window* — the window is only
        // complete once its last batch landed, and the window derives from
        // `now`, not from the marker.
        let (engine2, _) = try makeEngine(
            store: store, backend: backend, marker: before, threshold: 0, batchSize: 2
        )
        _ = try await engine2.syncIfNeeded()
        #expect(backend.uploads.count == 2 + 3, "the retry re-uploads the whole window, not the tail")
    }

    @Test("concurrent calls coalesce: one sync runs, the marker is written once")
    func concurrentCallsCoalesce() async throws {
        let store = StubMirrorCalendarStore(events: [mirrorEvent("EK-1", offset: 0)])
        let backend = StubMirrorBackend()
        let (engine, _) = try makeEngine(
            store: store, backend: backend, marker: t0.addingTimeInterval(-7200)
        )

        // All three fire at once; the actor's in-flight flag coalesces them.
        // Exactly one sync runs; the other two observe "in flight" and
        // return without uploading. None of them throw in this shape.
        async let a: Bool = engine.syncIfNeeded()
        async let b: Bool = engine.syncIfNeeded()
        async let c: Bool = engine.syncIfNeeded()
        _ = try await [a, b, c]

        #expect(backend.uploads.count == 1)
    }

    @Test("a failed sync does not poison the next attempt (no stuck in-flight flag)")
    func failedSyncResetsInFlight() async throws {
        let store = StubMirrorCalendarStore(events: [mirrorEvent("EK-1", offset: 0)])
        let backend = StubMirrorBackend()
        backend.failAfterBatches = 1
        let (engine, storage) = try makeEngine(
            store: store, backend: backend, marker: t0.addingTimeInterval(-7200)
        )

        _ = try? await engine.syncIfNeeded()
        backend.failAfterBatches = 0
        let synced = try await engine.syncIfNeeded()
        #expect(synced == true)
        #expect(backend.uploads.count == 2)
        let stamped = try readMarker(storage)
        #expect(markerDate(stamped) == t0)
    }

    // --- second review F1: the wire request must carry snapshot_as_of ------

    /// The request the *real* client sends must satisfy the real route's
    /// schema: `snapshot_as_of` is required, and one window's batches all
    /// carry the same instant. The unit stubs above cannot catch this — they
    /// take the field as a parameter and never read a wire body — so this
    /// test drives `AgentClient` itself through the URLProtocol stub and
    /// inspects the JSON the real encoder produced.
    @Test("the wire request carries snapshot_as_of, identical across batches")
    func wireRequestCarriesSnapshotAsOf() async throws {
        let service = Service()
        // JSONSerialization products are not Sendable, so the bodies collect
        // as `Data` inside a lock-protected box and are parsed on the test
        // side after the calls return.
        final class BodyBox: @unchecked Sendable {
            private let lock = NSLock()
            private var bodies: [Data] = []
            func append(_ data: Data) { lock.withLock { bodies.append(data) } }
            var all: [Data] { lock.withLock { bodies } }
        }
        let box = BodyBox()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/calendar/sync"):
                box.append(call.rawBody)
                return .ok([
                    "status": "ok", "upserted": 0, "skipped": 0, "marked_deleted": 0,
                    "sync_epoch": 1,
                ])
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ChatStub.self]
        let client = try AgentClient(
            baseURL: service.baseURL,
            session: URLSession(configuration: configuration)
        )
        let instant = Date(timeIntervalSince1970: 1_783_000_000)
        let window = DateInterval(
            start: instant.addingTimeInterval(-90 * 86_400),
            end: instant.addingTimeInterval(180 * 86_400)
        )
        let events = (0..<3).map { mirrorEvent("EK-\($0)", offset: Double($0)) }
        // Two batches of the same window: same snapshot instant.
        _ = try await client.uploadCalendarSync(
            windowStart: window.start, windowEnd: window.end,
            events: Array(events.prefix(2)), calendars: [],
            windowComplete: false,
            snapshotAsOf: instant, syncEpoch: 1, token: "token"
        )
        _ = try await client.uploadCalendarSync(
            windowStart: window.start, windowEnd: window.end,
            events: Array(events.suffix(1)), calendars: [],
            windowComplete: true,
            snapshotAsOf: instant, syncEpoch: 1, token: "token"
        )
        let bodies = box.all
            .compactMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
        #expect(bodies.count == 2)
        for body in bodies {
            #expect(body["snapshot_as_of"] != nil)
            #expect(body["window_complete"] != nil)
            #expect(body["sync_epoch"] as? Int == 1)
        }
        #expect(
            bodies[0]["snapshot_as_of"] as? String == bodies[1]["snapshot_as_of"] as? String
        )
    }

    // --- third review G2: the wait budget must actually bound the wait ------

    /// G2, reproduced against the first implementation: a task group holding
    /// both the timer and `await task.value` cannot return early — the group
    /// waits for all children and `cancelAll()` does not interrupt an
    /// unstructured task's `value` — so a 50 ms budget waited out a 900 ms
    /// sync. The wait must return within the budget while the sync itself
    /// still completes in the background (it is not cancelled).
    @Test("wait() returns within the budget while a slow sync keeps running")
    func waitIsBoundedByTheBudget() async throws {
        final class FlagBox: @unchecked Sendable {
            private let lock = NSLock()
            private var value = false
            var isSet: Bool { lock.withLock { value } }
            func set() { lock.withLock { value = true } }
        }
        let finished = FlagBox()
        let handle = MirrorSyncHandle(
            run: {
                try? await Task.sleep(for: .milliseconds(300))
                finished.set()
            },
            budget: .milliseconds(50)
        )
        let started = ContinuousClock.now
        await handle.wait()
        let waited = ContinuousClock.now - started
        // The wait returned within the budget (plus scheduling slop far below
        // the sync's own duration — the repro waited ~300 ms).
        #expect(waited < .milliseconds(200), "waited \(waited) for a 50 ms budget")
        // The timeout did not cancel the sync: it finishes on its own.
        waitForGate("the slow sync completes after the timeout") { finished.isSet }
        #expect(finished.isSet)
    }

    /// The other side of the race: a sync that finishes inside the budget
    /// wakes the waiter immediately — the budget is a ceiling, not a delay.
    @Test("wait() returns as soon as a fast sync finishes")
    func waitReturnsWhenTheSyncFinishes() async throws {
        let handle = MirrorSyncHandle(
            run: { try? await Task.sleep(for: .milliseconds(20)) },
            budget: .seconds(5)
        )
        let started = ContinuousClock.now
        await handle.wait()
        let waited = ContinuousClock.now - started
        #expect(waited < .seconds(1), "waited \(waited) for a 20 ms sync")
    }
}

// --- §9.1's fourth trigger: the wait that gave up --------------------------

/// The pre-send budget's expiry, and the note that records it.
///
/// The defect this suite exists for was structural rather than careless: the
/// note was reached only where the sync had *finished*, which is the one outcome
/// that means the budget did not expire — and by then the engine's own in-flight
/// flag was down, so the note's guard turned it into a no-op as well. It sat
/// beside the wait looking like a check on both sides of a race and was
/// unreachable on every path. So the tests below are about **which side of the
/// race the answer comes from**, not about the note's own body.
@Suite("The pre-send mirror budget", .serialized)
struct MirrorBudgetTests {

    @Test("the budget running out is recorded, from the losing side of the wait")
    func theBudgetExpiryIsRecorded() async {
        let fired = CounterBox()
        let handle = MirrorSyncHandle(
            run: { try? await Task.sleep(for: .milliseconds(300)) },
            budget: .milliseconds(50),
            onBudgetExpired: { fired.bump() }
        )

        await handle.wait()

        #expect(fired.count == 1)
    }

    @Test("a sync that finishes inside the budget records nothing")
    func aCompletedSyncRecordsNothing() async {
        let fired = CounterBox()
        let handle = MirrorSyncHandle(
            run: { try? await Task.sleep(for: .milliseconds(20)) },
            budget: .seconds(5),
            onBudgetExpired: { fired.bump() }
        )

        await handle.wait()

        // Not "usually": the callback is reachable only through the outcome the
        // waiter observed. A pass that finished inside the budget confirmed
        // everything, so a note for it would raise a warning about nothing —
        // with no pass left able to clear it.
        #expect(fired.count == 0)
    }

    @Test("a handle whose work is already done records nothing")
    func anInstantRunRecordsNothing() async {
        let fired = CounterBox()
        // The shape a device with no engine composes, and the shape `done()`
        // stands for: nothing was waited on, so nothing expired.
        let handle = MirrorSyncHandle(
            run: {}, budget: .seconds(30), onBudgetExpired: { fired.bump() }
        )
        await handle.wait()
        await MirrorSyncHandle.done().wait()

        #expect(fired.count == 0)
    }

    /// The composition `AppModel` performs, exercised end to end. `AppModel`
    /// itself is in the app target — it builds a device session and an EventKit
    /// store — so the wiring is reproduced here with the real engine, and the
    /// app's own body is one line of it. This is the test that would have failed
    /// for the placement defect: with the note after the pass instead of on the
    /// budget's side, the device below never says dirty.
    @Test("an expired budget leaves the device saying the mirror is unsynced")
    func theAppCompositionLeavesTheMirrorDirty() async throws {
        let store = StubMirrorCalendarStore(events: [mirrorEvent("EK-1", offset: 0)])
        let backend = StubMirrorBackend()
        let storage = InMemoryCredentialStore()
        try freshMarker(storage, at: t0)
        let engine = CalendarMirrorSyncEngine(
            store: store, backend: backend, storage: storage,
            uploader: CalendarSyncUploader(batchSize: 200),
            now: { t0 }, stalenessThreshold: 3600
        )
        // A pass that outlives any budget the send path would tolerate. The
        // budget below is far longer than the engine needs to reach its first
        // upload, so "the pass was still in flight" is a fact here rather than a
        // race the test hopes to win.
        backend.onBatch = { _ in try? await Task.sleep(for: .milliseconds(1000)) }

        let handle = MirrorSyncHandle(
            run: { try? await engine.sync(reason: .forced) },
            budget: .milliseconds(200),
            onBudgetExpired: { try? await engine.noteSyncBudgetExhausted() }
        )
        await handle.wait()

        // The receipt path has moved on and the device still says dirty — which
        // is the whole point of the note: the pass took its snapshot before this
        // change, so its success will confirm a number below the one now stored.
        #expect(try seq(storage, CredentialKey.calendarChangeSeq) == 1)
        #expect(try await engine.knownUnsynced)

        // And the pass really is still running, so the note was not a guess
        // about a pass that had already ended.
        #expect(backend.uploads.count == 1)
    }
}

// The reason parameter, the rerun flag and the two change sequences (design
// §9.1, review R3-F13). These are the cases that made the old engine wrong:
// 「刚同步→创建→触发同步→仍读不到新事件」 (the staleness gate) and 「上传期间发生
// 的变更丢失」 (the in-flight flag returning without recording that anything was
// owed). The sequence tests are written as the design states them — including
// the counterexample, where A's success must **not** clear the flag.

/// A box the test uses to reach an actor that is already mid-pass. The engine
/// is built after the backend (it takes the stub as a dependency), and the
/// hook that needs it is installed before, so the engine arrives through this.
private final class EngineBox: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: CalendarMirrorSyncEngine?
    var engine: CalendarMirrorSyncEngine? {
        get { lock.withLock { stored } }
        set { lock.withLock { stored = newValue } }
    }
}

/// Values recorded from inside a pass, read after the call returns.
private final class ObservationBox: @unchecked Sendable {
    private let lock = NSLock()
    private var values: [Bool] = []
    func append(_ value: Bool) { lock.withLock { values.append(value) } }
    var all: [Bool] { lock.withLock { values } }
}

private func seq(_ storage: CredentialStore, _ key: String) throws -> Int {
    guard let data = try storage.read(key),
          let text = String(data: data, encoding: .utf8),
          let value = Int(text) else { return 0 }
    return value
}

private func freshMarker(_ storage: CredentialStore, at date: Date) throws {
    try storage.write(
        CredentialKey.calendarMirrorSyncedAt,
        value: Data(String(date.timeIntervalSince1970).utf8)
    )
}

@Suite("Calendar mirror sync reasons and change sequences", .serialized)
struct MirrorSyncReasonTests {

    /// A second engine over the *same* storage: how "kill the app and relaunch"
    /// is modelled offline. §9.1 requires the persisted sequence to still say
    /// dirty after a restart, so the persistence has to be read by something
    /// other than the actor that wrote it.
    private func relaunch(
        store: CalendarStore, backend: ChatBackend, storage: CredentialStore
    ) -> CalendarMirrorSyncEngine {
        CalendarMirrorSyncEngine(
            store: store, backend: backend, storage: storage,
            uploader: CalendarSyncUploader(batchSize: 200),
            now: { t0 }, stalenessThreshold: 3600
        )
    }

    private func makeEngine(
        store: CalendarStore, backend: ChatBackend, marker: Date?
    ) throws -> (CalendarMirrorSyncEngine, InMemoryCredentialStore) {
        let storage = InMemoryCredentialStore()
        if let marker { try freshMarker(storage, at: marker) }
        return (relaunch(store: store, backend: backend, storage: storage), storage)
    }

    @Test("a forced sync uploads even though the marker is seconds old")
    func forcedBypassesTheStalenessGate() async throws {
        let store = StubMirrorCalendarStore(events: [mirrorEvent("EK-1", offset: 0)])
        let backend = StubMirrorBackend()
        let (engine, _) = try makeEngine(store: store, backend: backend, marker: t0)

        // The old engine's answer to "just synced, then the user created an
        // event": skip. That skip is the defect §9.1 was written for.
        #expect(try await engine.syncIfNeeded() == false)
        #expect(backend.uploads.isEmpty)

        #expect(try await engine.sync(reason: .forced) == true)
        #expect(backend.uploads.count == 1)
    }

    @Test("a forced trigger that lands mid-pass runs one follow-up pass, not two passes at once")
    func forcedDuringAPassRequestsARerun() async throws {
        let store = StubMirrorCalendarStore(events: [mirrorEvent("EK-1", offset: 0)])
        let backend = StubMirrorBackend()
        let (engine, storage) = try makeEngine(store: store, backend: backend, marker: nil)
        let box = EngineBox()
        backend.onBatch = { index in
            // A change lands while batch 1 is in flight: the pass took its
            // snapshot before this existed, so it does not cover it.
            if index == 1, let engine = box.engine {
                _ = try? await engine.noteCalendarChanged()
            }
        }
        box.engine = engine

        #expect(try await engine.sync(reason: .forced) == true)

        // Two passes: the one that was running, and the owed one. Never two
        // concurrent windows (§9.1's 「inFlight + 重跑合并为至多两趟」).
        #expect(backend.uploads.count == 2)
        #expect(try seq(storage, CredentialKey.calendarChangeSeq) == 1)
        #expect(try seq(storage, CredentialKey.calendarSyncedChangeSeq) == 1)
        #expect(try await engine.knownUnsynced == false)
    }

    @Test("B arriving during A's upload is not confirmed by A's success (R3-F13)")
    func aSuccessDoesNotConfirmBArrivingMidPass() async throws {
        let store = StubMirrorCalendarStore(events: [mirrorEvent("EK-1", offset: 0)])
        let backend = StubMirrorBackend()
        let (engine, storage) = try makeEngine(store: store, backend: backend, marker: nil)
        let box = EngineBox()
        // The relaunched app, already running while the first one syncs.
        let relaunched = relaunch(store: store, backend: backend, storage: storage)
        let observed = ObservationBox()
        backend.onBatch = { index in
            if index == 1, let engine = box.engine {
                // B arrives mid-upload.
                _ = try? await engine.noteCalendarChanged()
            }
            if index == 2 {
                // A has already succeeded and the follow-up pass is uploading.
                // This is the moment R3-F13 is about: A covered sequence 0,
                // B is sequence 1, and A's last batch landing must not have
                // confirmed B. Read from the *other* engine, so the answer is
                // the persisted one a restarted app would see.
                observed.append((try? await relaunched.knownUnsynced) ?? false)
            }
        }
        box.engine = engine

        #expect(try await engine.sync(reason: .forced) == true)

        #expect(observed.all == [true], "A's success cleared a sequence it never uploaded")
        // The follow-up pass captured B, so both engines now agree it is clean.
        #expect(try await engine.knownUnsynced == false)
        #expect(try await relaunched.knownUnsynced == false)
        #expect(try seq(storage, CredentialKey.calendarChangeSeq) == 1)
        #expect(try seq(storage, CredentialKey.calendarSyncedChangeSeq) == 1)
    }

    @Test("a failed forced pass drops the marker, keeps the sequence dirty, and re-arms the gate")
    func forcedFailureDropsTheMarkerAndStaysDirty() async throws {
        let store = StubMirrorCalendarStore(events: [mirrorEvent("EK-1", offset: 0)])
        let backend = StubMirrorBackend()
        let (engine, storage) = try makeEngine(store: store, backend: backend, marker: t0)
        backend.failAfterBatches = 1

        do {
            _ = try await engine.sync(reason: .forced)
            Issue.record("a failed upload must surface, not vanish")
        } catch {}

        // The marker answers "when may I re-upload?", and it is gone — so the
        // staleness gate is re-armed and both reasons really do upload next time.
        #expect(try storage.read(CredentialKey.calendarMirrorSyncedAt) == nil)
        // Truthfulness is a different question, answered by the sequence: this
        // pass covered none of it, so the device cannot vouch for the mirror.
        #expect(try await engine.knownUnsynced == true)
        #expect(try seq(storage, CredentialKey.calendarChangeSeq) == 1)

        // …and the re-armed gate really does run, even though the marker was
        // fresh before the failure.
        backend.failAfterBatches = 0
        #expect(try await engine.syncIfNeeded() == true)
        #expect(try await engine.knownUnsynced == false)
    }

    @Test("the change sequence is monotonic under concurrent sources")
    func changeSequenceIsMonotonicUnderConcurrency() async throws {
        let store = StubMirrorCalendarStore(events: [mirrorEvent("EK-1", offset: 0)])
        let backend = StubMirrorBackend()
        let (engine, storage) = try makeEngine(store: store, backend: backend, marker: nil)

        // Three sources at once — the N device-action reports of a multi-action
        // reply are exactly this shape. No increment may be lost: the
        // read-modify-write happens inside one actor call with no suspension
        // point in it.
        async let a = engine.noteCalendarChanged()
        async let b = engine.noteCalendarChanged()
        async let c = engine.noteCalendarChanged()
        _ = try await [a, b, c]

        #expect(try seq(storage, CredentialKey.calendarChangeSeq) == 3)
        // …and the passes that did run confirmed everything they captured, so
        // the three don't leave a warning behind.
        #expect(try seq(storage, CredentialKey.calendarSyncedChangeSeq) == 3)
        #expect(try await engine.knownUnsynced == false)
    }

    @Test("an exhausted pre-send budget is recorded, and a pass that catches up clears it")
    func exhaustedBudgetIsRecordedAndCleared() async throws {
        let store = StubMirrorCalendarStore(events: [mirrorEvent("EK-1", offset: 0)])
        let backend = StubMirrorBackend()
        let (engine, storage) = try makeEngine(store: store, backend: backend, marker: nil)
        let box = EngineBox()
        let relaunched = relaunch(store: store, backend: backend, storage: storage)
        let observed = ObservationBox()
        backend.onBatch = { index in
            if index == 1, let engine = box.engine {
                // The send gave up waiting; the pass is still running.
                try? await engine.noteSyncBudgetExhausted()
            }
            if index == 2 {
                observed.append((try? await relaunched.knownUnsynced) ?? false)
            }
        }
        box.engine = engine

        #expect(try await engine.syncIfNeeded() == true)

        // The pass that was interrupted by the budget confirmed only the
        // sequence it captured, so the warning is up until the owed pass runs.
        #expect(observed.all == [true])
        #expect(backend.uploads.count == 2)
        #expect(try seq(storage, CredentialKey.calendarChangeSeq) == 1)
        #expect(try await engine.knownUnsynced == false)
    }

    @Test("a budget note with no pass running records nothing")
    func budgetNoteWithoutAPassIsANoOp() async throws {
        let store = StubMirrorCalendarStore(events: [mirrorEvent("EK-1", offset: 0)])
        let backend = StubMirrorBackend()
        let (engine, storage) = try makeEngine(store: store, backend: backend, marker: t0)

        // The budget only means something as a *wait* that expired. If no pass
        // is in flight the wait did not expire, and bumping here would raise a
        // warning about nothing — with no pass left to clear it.
        try await engine.noteSyncBudgetExhausted()

        #expect(try seq(storage, CredentialKey.calendarChangeSeq) == 0)
        #expect(try await engine.knownUnsynced == false)
        #expect(backend.uploads.isEmpty)
    }

    // --- the device-action report's trigger: armed, not awaited -------------

    @Test("arming returns before the upload finishes, with the mirror already dirty")
    func armingReturnsBeforeTheUploadFinishes() async throws {
        let store = StubMirrorCalendarStore(events: [mirrorEvent("EK-1", offset: 0)])
        let backend = StubMirrorBackend()
        let (engine, storage) = try makeEngine(store: store, backend: backend, marker: t0)
        backend.onBatch = { _ in try? await Task.sleep(for: .milliseconds(600)) }

        let started = ContinuousClock.now
        try await engine.armCalendarChanged()
        let waited = ContinuousClock.now - started

        // The call is the arm, not the pass. This is what stops one reply's N
        // reports from putting N whole-window uploads in front of the receipt
        // the user is waiting for: the sink that calls this sits between a write
        // landing and its answer. (Before, the sink awaited the full
        // `noteCalendarChanged`, and this returned only after the upload.)
        #expect(waited < .milliseconds(300), "armed in \(waited)")
        // The armed state is committed before the call returns, not raced
        // against a task the caller never awaited — §9.1's query gate has to be
        // honest the moment the window opens.
        #expect(try seq(storage, CredentialKey.calendarChangeSeq) == 1)
        #expect(try await engine.knownUnsynced)
    }

    @Test("three reports during one pass arm one follow-up, never three passes")
    func threeArmsCoalesceIntoOneFollowUp() async throws {
        let store = StubMirrorCalendarStore(events: [mirrorEvent("EK-1", offset: 0)])
        let backend = StubMirrorBackend()
        let (engine, storage) = try makeEngine(store: store, backend: backend, marker: t0)
        let box = EngineBox()
        backend.onBatch = { index in
            guard index == 1, let engine = box.engine else { return }
            // A multi-action reply: three reports land while the pass is
            // mid-upload. None of them is in the snapshot that pass took.
            for _ in 0..<3 { try? await engine.armCalendarChanged() }
        }
        box.engine = engine

        try await engine.armCalendarChanged()
        waitForGate("the armed passes drain") { backend.uploads.count >= 2 }

        // One running pass plus at most one follow-up covers all four changes
        // (design §9.1 bounds a trigger storm at two), and the device is clean
        // afterwards — the follow-up took its snapshot after the last arm, so
        // nothing is left armed with no pass behind it.
        #expect(backend.uploads.count == 2)
        #expect(try seq(storage, CredentialKey.calendarChangeSeq) == 4)
        #expect(try seq(storage, CredentialKey.calendarSyncedChangeSeq) == 4)
        #expect(try await engine.knownUnsynced == false)
    }

    @Test("an arm with nothing running runs exactly one pass")
    func anArmWithNothingRunningRunsOnePass() async throws {
        let store = StubMirrorCalendarStore(events: [mirrorEvent("EK-1", offset: 0)])
        let backend = StubMirrorBackend()
        let (engine, storage) = try makeEngine(store: store, backend: backend, marker: t0)

        // The marker is seconds old, so the staleness gate would have skipped
        // this entirely: the arm is `.forced` by construction.
        try await engine.armCalendarChanged()
        waitForGate("the armed pass drains") { !backend.uploads.isEmpty }

        #expect(backend.uploads.count == 1)
        #expect(try seq(storage, CredentialKey.calendarSyncedChangeSeq) == 1)
        #expect(try await engine.knownUnsynced == false)
    }

    @Test("a gated trigger during a pass arms nothing: one window, no follow-up")
    func gatedDuringAPassDoesNotArmARerun() async throws {
        let store = StubMirrorCalendarStore(events: [mirrorEvent("EK-1", offset: 0)])
        let backend = StubMirrorBackend()
        let (engine, _) = try makeEngine(store: store, backend: backend, marker: nil)
        let box = EngineBox()
        backend.onBatch = { index in
            if index == 1, let engine = box.engine {
                // Same question the running pass is already answering.
                _ = try? await engine.syncIfNeeded()
            }
        }
        box.engine = engine

        #expect(try await engine.syncIfNeeded() == true)

        #expect(backend.uploads.count == 1)
    }
}
