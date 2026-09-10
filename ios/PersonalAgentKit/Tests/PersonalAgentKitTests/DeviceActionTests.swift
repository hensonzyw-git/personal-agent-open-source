import Foundation
import Testing
@testable import PersonalAgentKit

/// The device-action wire contract, fail-closed.
///
/// The failure shapes here are the §5.1 ones the device boundary opens: the
/// phone is the executor, so a payload it cannot understand is a *known*
/// failure to report (failed), never silence (which the server's timeout sweep
/// would read as needs_manual_review) and never a guess (an event with a
/// defaulted time is not the event the user asked for).
@Suite("Device action wire")
struct DeviceActionWireTests {

    private let validPayload: [String: Any] = [
        "action_id": "action-1",
        "tool": "calendar.create_event",
        "event": [
            "title": "网球",
            "start": "2026-09-12T15:00:00+08:00",
            "end": "2026-09-12T16:30:00+08:00",
            "all_day": false,
            "location": NSNull(),
            "notes": NSNull(),
        ],
    ]

    private func payload(_ mutate: (inout [String: Any]) -> Void) -> [String: Any] {
        var copy = validPayload
        mutate(&copy)
        return copy
    }

    @Test("a valid payload decodes with the authorised fields")
    func validDecode() throws {
        let action = try DeviceEventAction.decode(from: validPayload)
        #expect(action.actionID == "action-1")
        #expect(action.tool == "calendar.create_event")
        #expect(action.event.title == "网球")
        #expect(action.event.allDay == false)
        #expect(action.event.location == nil)
        let draft = try #require(action.event.draft())
        #expect(draft.title == "网球")
    }

    @Test("a missing required field is refused, not repaired")
    func missingFields() {
        #expect(throws: DeviceActionError.malformed("device_action is missing action_id")) {
            _ = try DeviceEventAction.decode(from: payload { $0.removeValue(forKey: "action_id") })
        }
        #expect(throws: DeviceActionError.self) {
            _ = try DeviceEventAction.decode(from: payload {
                $0["event"] = eventWithout("title")
            })
        }
        #expect(throws: DeviceActionError.self) {
            _ = try DeviceEventAction.decode(from: payload {
                $0["event"] = eventWithout("start")
            })
        }
        #expect(throws: DeviceActionError.self) {
            _ = try DeviceEventAction.decode(from: payload {
                $0["event"] = eventWithout("end")
            })
        }
        #expect(throws: DeviceActionError.self) {
            _ = try DeviceEventAction.decode(from: payload {
                $0["event"] = eventWithout("all_day")
            })
        }
    }

    private func eventWithout(_ key: String) -> [String: Any] {
        var event = validPayload["event"] as! [String: Any]
        event.removeValue(forKey: key)
        return event
    }

    @Test("an unknown tool is refused, not guessed at")
    func unknownTool() {
        #expect(throws: DeviceActionError.unsupportedTool("future.tool")) {
            _ = try DeviceEventAction.decode(from: payload { $0["tool"] = "future.tool" })
        }
    }

    @Test("unparseable times are malformed, never saved at a guessed instant")
    func unparseableTimes() {
        #expect(throws: DeviceActionError.self) {
            _ = try DeviceEventAction.decode(from: payload {
                $0["event"] = eventWithout("start").merging(
                    ["start": "下周三下午"], uniquingKeysWith: { _, new in new }
                )
            })
        }
    }

    @Test("the report body is closed and event_id travels only with evidence")
    func reportBodies() {
        // created/duplicate carry the event id the server records as evidence.
        let created = DeviceActionResultBody.created(eventID: "EK-1")
        #expect(created.result == "created")
        #expect(created.eventID == "EK-1")
        #expect(created.detail == nil)
        // denied/failed are the zero-write testimony: no event id exists.
        let denied = DeviceActionResultBody.denied(detail: "access denied")
        #expect(denied.result == "denied")
        #expect(denied.eventID == nil)
        #expect(denied.detail == "access denied")
        let failed = DeviceActionResultBody.failed(detail: "boom")
        #expect(failed.result == "failed")
        #expect(failed.eventID == nil)
    }

    @Test("the receipt's device_action envelope keeps the wire-spelled action id")
    func envelopeDecodesTheWireSpelledID() throws {
        // Regression: the envelope's keys must be declared, not synthesised —
        // a synthesised `actionID` key decodes the wire's `action_id` to nil
        // and turns every well-formed action into an unnameable refusal.
        let body = chatJSON([
            "action_id": "018f0000-0000-7000-8000-00000000cafe",
            "tool": "calendar.create_event",
            "event": [
                "title": "网球",
                "start": "2026-09-12T15:00:00Z",
                "end": "2026-09-12T16:30:00Z",
                "all_day": false,
            ],
        ])
        let envelope = try JSONDecoder().decode(DeviceActionEnvelope.self, from: body)
        guard case .execute(let action) = envelope.resolve() else {
            Issue.record("a well-formed action must resolve to execute")
            return
        }
        #expect(action.actionID == "018f0000-0000-7000-8000-00000000cafe")
    }
}

/// The sync uploader's window and chunking semantics: the device is the fact
/// source, so the last batch of a window must carry `window_complete`, and an
/// empty window must still produce one complete batch (nothing in the window
/// is a fact, not an absence of evidence).
@Suite("Calendar sync uploader")
struct CalendarSyncUploaderTests {

    private func event(_ offset: TimeInterval) -> CalendarMirrorEvent {
        CalendarMirrorEvent(
            eventIdentifier: "EK-\(Int(offset))", calendarIdentifier: "CAL-1",
            title: "e", start: Date(timeIntervalSince1970: offset),
            end: Date(timeIntervalSince1970: offset + 3600), allDay: false,
            location: nil, notes: nil,
            lastModified: Date(timeIntervalSince1970: 1_783_000_000)
        )
    }

    @Test("a window fits one batch and the batch is complete")
    func singleBatch() {
        let uploader = CalendarSyncUploader()
        let now = Date(timeIntervalSince1970: 1_783_000_000)
        let chunks = uploader.chunk([event(1), event(2)], now: now)
        #expect(chunks.count == 1)
        #expect(chunks[0].lastBatch)
        #expect(chunks[0].events.count == 2)
        // 回看 90 天 / 前瞻 180 天.
        #expect(chunks[0].windowStart.timeIntervalSince(now) == -90 * 86_400)
        #expect(chunks[0].windowEnd.timeIntervalSince(now) == 180 * 86_400)
    }

    @Test("more than one batch chunks, and only the last is complete")
    func multiBatch() {
        let uploader = CalendarSyncUploader(batchSize: 2)
        let now = Date(timeIntervalSince1970: 1_783_000_000)
        let events = (0..<5).map { event(Double($0)) }
        let chunks = uploader.chunk(events, now: now)
        #expect(chunks.count == 3)
        #expect(chunks.map { $0.lastBatch } == [false, false, true])
        #expect(chunks.flatMap { $0.events }.count == 5)
    }

    @Test("an empty window still produces one complete batch")
    func emptyWindow() {
        let uploader = CalendarSyncUploader()
        let now = Date(timeIntervalSince1970: 1_783_000_000)
        let chunks = uploader.chunk([], now: now)
        #expect(chunks.count == 1)
        #expect(chunks[0].events.isEmpty)
        #expect(chunks[0].lastBatch)
    }

    @Test("RFC 3339 round-trips in UTC")
    func rfc3339() throws {
        let date = try #require(RFC3339.parse("2026-09-12T07:00:00Z"))
        #expect(RFC3339.string(from: date) == "2026-09-12T07:00:00Z")
        // An offset form parses to the same instant.
        let offset = try #require(RFC3339.parse("2026-09-12T15:00:00+08:00"))
        #expect(offset == date)
    }
}

// --- the device-action flow (`calendar.create_event`) -------------------------
//
// The reply to a chat send is the ONLY time the action exists on the wire, so
// the tests drive the real send path over the same URLProtocol stub the DEV-030
// suites use, with a stubbed executor seam. The failure shapes are §5.1's: an
// action nobody runs, an action the device refuses, and a refusal that cannot
// name its action — each either reported or honestly silent, never repaired.

/// A stub executor that records what it was handed and what it answered.
final class StubDeviceActionExecutor: DeviceActionExecuting, @unchecked Sendable {
    private let lock = NSLock()
    private var _actions: [DeviceEventAction] = []
    var actions: [DeviceEventAction] { lock.withLock { _actions } }
    /// The save outcome to hand back, or nil to use the default `created`.
    var outcome: CalendarSaveOutcome?
    /// The backend the report goes through — set after `makeChat` builds the
    /// session, which the executor cannot capture at construction time.
    var backend: (any ChatBackend)?
    /// What the stub answers on the report endpoint, or nil to proxy through
    /// to `backend` (the real endpoint interaction).
    var reportReply: OperationReceipt?
    /// Throws instead of answering when set — the lost-report-reply shape.
    var reportFails = false

    func executeAndReport(_ action: DeviceEventAction) async -> OperationReceipt? {
        lock.withLock { _actions.append(action) }
        let body: DeviceActionResultBody
        switch outcome ?? .created(eventID: "EK-NEW-1") {
        case .created(let id): body = .created(eventID: id)
        case .duplicate(let id): body = .duplicate(eventID: id)
        case .denied: body = .denied(detail: "refused by the stub")
        case .failed(let detail): body = .failed(detail: detail)
        }
        // The report goes through the real backend (the stub service), so the
        // endpoint interaction is the real one.
        do {
            if reportFails {
                throw AgentClientError.transport(
                    URLError(.networkConnectionLost).localizedDescription
                )
            }
            if let reportReply { return reportReply }
            return try await #require(backend)
                .reportDeviceActionResult(actionID: action.actionID, body: body)
        } catch {
            // The lost report reply: no settled projection exists on this
            // device, and the stub will not invent one — the caller degrades to
            // the parked receipt it already holds (design §4.2).
            return nil
        }
    }

    func failedReport(detail: String) -> DeviceActionResultBody {
        .failed(detail: detail)
    }
}

@Suite("The device-action flow", .serialized)
struct DeviceActionFlowTests {

    /// The action id is a canonical UUIDv4 because the server locates the
    /// operation by it (the idempotency key), and the stub service enforces
    /// that shape on every request it relays. A function, not a stored
    /// constant: a fresh dictionary per call, so nothing non-Sendable is
    /// ever shared.
    private static func actionPayload() -> [String: Any] {
        [
            "action_id": "018f0000-0000-7000-8000-00000000cafe",
            "tool": "calendar.create_event",
            "event": [
                "title": "网球",
                "start": "2026-09-12T15:00:00Z",
                "end": "2026-09-12T16:30:00Z",
                "all_day": false,
            ],
        ]
    }

    /// A chat send whose reply carries a `device_actions` list and parks the
    /// operation at `source_in_progress`. The report endpoint answers the way
    /// the real one does: a `created`/`duplicate` report settles succeeded
    /// with the reported event id as evidence; a `denied`/`failed` report
    /// settles `failed_safe`. A fake that answered success to every report
    /// would let a device claim a write it never made — exactly what this
    /// endpoint exists to prevent.
    private func answerWithAction(_ service: Service) {
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                var receipt = chatReceipt("source_in_progress", tool: "calendar.create_event")
                receipt["device_actions"] = [Self.actionPayload()]
                return .ok(receipt)
            case ("POST", let path) where path.hasPrefix("/v1/device-actions/"):
                if call.string("result") == "created" || call.string("result") == "duplicate" {
                    return .ok(chatReceipt(
                        "succeeded", tool: "calendar.create_event",
                        recordID: call.string("event_id") ?? "EK-NEW-1"
                    ))
                }
                return .ok(chatReceipt(
                    "failed_safe", tool: "calendar.create_event",
                    failureReason: "DEVICE_EXECUTION_FAILED"
                ))
            default:
                return .error(404, "NOT_FOUND")
            }
        }
    }

    @Test("a handed action is executed and its report settles the operation")
    func actionExecutedAndReported() async throws {
        let service = Service()
        answerWithAction(service)
        let executor = StubDeviceActionExecutor()
        let (chat, session, store) = try await makeChat(
            service: service, deviceActionExecutor: executor
        )
        executor.backend = session

        let final = try await chat.send(text: "周六下午三点网球")

        #expect(executor.actions.count == 1)
        #expect(executor.actions.first?.actionID == Self.actionPayload()["action_id"] as? String)
        #expect(executor.actions.first?.event.title == "网球")
        #expect(final.state == .succeeded)
        #expect(final.recordID == "EK-NEW-1")
        #expect(service.count("POST", "/v1/device-actions/018f0000-0000-7000-8000-00000000cafe/result") == 1)
        // A settled operation releases the pending slot.
        #expect(try store.read(CredentialKey.pendingChatSend) == nil)
    }

    @Test("an action with no executor composed is reported failed, not dropped")
    func actionWithoutExecutorIsReportedFailed() async throws {
        let service = Service()
        answerWithAction(service)
        let (chat, _, _) = try await makeChat(service: service)

        let final = try await chat.send(text: "周六下午三点网球")

        // The server was told the action did not run — the sweep is not the
        // only witness, and the person is not left with a phantom.
        #expect(service.count("POST", "/v1/device-actions/018f0000-0000-7000-8000-00000000cafe/result") == 1)
        let report = service.log.first {
            $0.path.hasPrefix("/v1/device-actions/")
        }
        #expect(report?.string("result") == "failed")
        #expect(final.state == .failedSafe)
    }

    @Test("a refusal with an action id is reported failed immediately")
    func refusedActionWithIDIsReported() async throws {
        let service = Service()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                var payload = Self.actionPayload()
                payload["tool"] = "future.tool"
                var receipt = chatReceipt("source_in_progress", tool: "calendar.create_event")
                receipt["device_actions"] = [payload]
                return .ok(receipt)
            case ("POST", let path) where path.hasPrefix("/v1/device-actions/"):
                return .ok(chatReceipt("failed_safe", failureReason: "DEVICE_EXECUTION_FAILED"))
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let executor = StubDeviceActionExecutor()
        let (chat, session, _) = try await makeChat(
            service: service, deviceActionExecutor: executor
        )
        executor.backend = session

        let final = try await chat.send(text: "安排一件事")

        // The executor was never reached: the action was refused at decode.
        #expect(executor.actions.isEmpty)
        #expect(service.count("POST", "/v1/device-actions/018f0000-0000-7000-8000-00000000cafe/result") == 1)
        #expect(final.state == .failedSafe)
    }

    @Test("a refusal without an action id reports nothing and polls the state")
    func refusalWithoutIDStaysSilent() async throws {
        let service = Service()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                var payload = Self.actionPayload()
                payload.removeValue(forKey: "action_id")
                var receipt = chatReceipt("source_in_progress", tool: "calendar.create_event")
                receipt["device_actions"] = [payload]
                return .ok(receipt)
            case ("GET", "/v1/operations/op-1"):
                // One poll answers terminal; nothing may report to the
                // device-actions endpoint, which does not know this action.
                return .ok(chatReceipt("succeeded", tool: "calendar.create_event", recordID: "EK-1"))
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let (chat, _, _) = try await makeChat(service: service)

        let final = try await chat.send(text: "安排一件事")

        // The poll actually happened (the state was read back) and no report
        // was sent for an action this device cannot name.
        #expect(service.count("GET", "/v1/operations/op-1") >= 1)
        #expect(service.log.filter { $0.path.hasPrefix("/v1/device-actions/") }.isEmpty)
        #expect(final.state == .succeeded)
    }

    @Test("a reply without a device action never touches the report endpoint")
    func plainReplySkipsTheFlow() async throws {
        let service = Service()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                return .ok(chatReceipt("succeeded", tool: "finance.log_expense", recordID: "rec-1"))
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let executor = StubDeviceActionExecutor()
        let (chat, _, _) = try await makeChat(
            service: service, deviceActionExecutor: executor
        )

        let final = try await chat.send(text: "咖啡 18 个人支出")

        #expect(executor.actions.isEmpty)
        #expect(service.log.filter { $0.path.hasPrefix("/v1/device-actions/") }.isEmpty)
        #expect(final.outcome == .recorded(recordID: "rec-1", tool: "finance.log_expense", record: nil))
    }

    @Test("a denied save is the device's zero-write testimony")
    func deniedSaveReportsDenied() async throws {
        let service = Service()
        answerWithAction(service)
        let executor = StubDeviceActionExecutor()
        executor.outcome = .denied
        let (chat, session, _) = try await makeChat(
            service: service, deviceActionExecutor: executor
        )
        executor.backend = session

        let final = try await chat.send(text: "周六下午三点网球")

        let report = try #require(service.log.first { $0.path.hasPrefix("/v1/device-actions/") })
        #expect(report.string("result") == "denied")
        #expect(final.state == .failedSafe)
    }

    @Test("a lost report reply polls the operation id, never the action id")
    func lostReportReplyPollsTheOperationID() async throws {
        // Review R9, reproduced: the executor degraded to a parked receipt
        // carrying the *action id* (the report endpoint's address, and the
        // operation's idempotency key), so the settle loop polled
        // `GET /v1/operations/{action id}` — an id no operation has.
        let service = Service()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                var receipt = chatReceipt("source_in_progress", tool: "calendar.create_event")
                receipt["device_actions"] = [Self.actionPayload()]
                return .ok(receipt)
            case ("GET", "/v1/operations/op-1"):
                // The settle loop must land here: the real operation id from
                // the chat reply.
                return .ok(chatReceipt("succeeded", tool: "calendar.create_event", recordID: "EK-1"))
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let executor = StubDeviceActionExecutor()
        executor.reportFails = true
        let (chat, _, _) = try await makeChat(
            service: service, deviceActionExecutor: executor
        )

        let final = try await chat.send(text: "周六下午三点网球")

        // The poll reached the real operation, so the turn still settles
        // honestly after the lost report reply; the action id — the
        // idempotency key — was never used as an operation id.
        #expect(service.count("GET", "/v1/operations/op-1") >= 1)
        #expect(service.count("GET", "/v1/operations/018f0000-0000-7000-8000-00000000cafe") == 0)
        #expect(final.state == .succeeded)
    }
}
// --- review R6: the poll path must deliver a parked device action exactly once

@Suite("Device action delivery on the poll path", .serialized)
struct DeviceActionPollDeliveryTests {

    /// The chat POST detaches (202, synthetic body, no action on it). The
    /// operation is parked at `source_in_progress`, and the poll — the same
    /// projection the server answers every `GET /v1/operations/{id}` with —
    /// carries the action. Executing it is what settles the operation.
    @Test("a delivered action rides the poll and is executed once")
    func deliveredActionOnPollIsExecuted() async throws {
        let service = Service()
        service.answer { call, seen in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                // The detached synthetic body: no action travels here.
                return .accepted(chatReceipt("accepted"))
            case ("GET", "/v1/operations/op-1"):
                if seen == 0 {
                    // First poll: parked, and the action is on it.
                    var receipt = chatReceipt(
                        "source_in_progress", tool: "calendar.create_event"
                    )
                    receipt["device_actions"] = [Self.actionPayload()]
                    return .ok(receipt)
                }
                // Later polls: the report already settled it.
                return .ok(chatReceipt(
                    "succeeded", tool: "calendar.create_event", recordID: "EK-NEW-1"
                ))
            case ("POST", let path) where path.hasPrefix("/v1/device-actions/"):
                return .ok(chatReceipt(
                    "succeeded", tool: "calendar.create_event", recordID: "EK-NEW-1"
                ))
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let executor = StubDeviceActionExecutor()
        let (chat, session, store) = try await makeChat(
            service: service, deviceActionExecutor: executor
        )
        executor.backend = session

        let final = try await chat.send(text: "周六下午三点网球")

        #expect(executor.actions.count == 1)
        #expect(executor.actions.first?.actionID == Self.actionPayload()["action_id"] as? String)
        #expect(service.count("POST", "/v1/device-actions/018f0000-0000-7000-8000-00000000cafe/result") == 1)
        #expect(final.state == .succeeded)
        #expect(final.recordID == "EK-NEW-1")
        #expect(try store.read(CredentialKey.pendingChatSend) == nil)
    }

    /// The delivery marker is durable: an app restart after a lost report
    /// reply resumes the same operation, and the poll hands the action over
    /// again — but an action this device already executed must never run a
    /// second time. Re-running is how a duplicate event gets born; the sweep,
    /// not a re-execution, is what owns the silence.
    @Test("a resumed operation never re-executes an already-delivered action")
    func resumedOperationDoesNotReexecute() async throws {
        let service = Service()
        service.answer { call, seen in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                return .accepted(chatReceipt("accepted"))
            case ("GET", "/v1/operations/op-1"):
                if seen < 4 {
                    // Parked the whole time: the projection hands the action
                    // over on every read, so polls 1–3 re-deliver it to the
                    // settle loop that already executed it — the redelivery
                    // the marker must absorb without a second run.
                    var receipt = chatReceipt(
                        "source_in_progress", tool: "calendar.create_event"
                    )
                    receipt["device_actions"] = [Self.actionPayload()]
                    return .ok(receipt)
                }
                // The resume's by-id read: the report's CAS finally landed.
                return .ok(chatReceipt(
                    "succeeded", tool: "calendar.create_event", recordID: "EK-NEW-1"
                ))
            case ("POST", let path) where path.hasPrefix("/v1/device-actions/"):
                // The first execution's report never settled the operation
                // (its reply was lost in flight), so the operation stays
                // parked and the projection keeps re-offering the action.
                return .ok(chatReceipt("source_in_progress", tool: "calendar.create_event"))
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let executor = StubDeviceActionExecutor()
        let (chat, session, store) = try await makeChat(
            service: service, deviceActionExecutor: executor
        )
        executor.backend = session

        // First send: executes the delivered action once; the report never
        // settles it, the four polls all re-deliver, and the pending record
        // survives with the marker on disk. The execute count proves the
        // re-deliveries inside the settle loop were absorbed silently —
        // a second execution would make this 5, not 1.
        _ = try await chat.send(text: "周六下午三点网球")
        #expect(executor.actions.count == 1)
        #expect(service.count("GET", "/v1/operations/op-1") >= 4)

        // Resume (what `resume()` does after a restart): the by-id read
        // finds the settled operation. The action is not re-offered on it —
        // but if the read had caught the still-parked shape, the marker
        // would have to refuse it just the same.
        let final = try await chat.resume()

        #expect(executor.actions.count == 1)
        #expect(final?.state == .succeeded)
        #expect(try store.read(CredentialKey.pendingChatSend) == nil)
    }

    /// A resume whose stored marker predates the poll's action has no marker:
    /// the first poll delivery *is* the first execution. This is the fresh
    /// detach + restart shape — the send exhausts its polls before the
    /// worker parks the operation, so the by-id read on resume is the
    /// delivery — and it must execute exactly once, not zero.
    @Test("a resumed operation with no delivered marker executes the polled action")
    func resumedFreshOperationExecutesPolledAction() async throws {
        let service = Service()
        service.answer { call, seen in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                return .accepted(chatReceipt("accepted"))
            case ("GET", "/v1/operations/op-1"):
                if seen < 4 {
                    // The first send's polls: the worker is still running,
                    // nothing parked yet, no action anywhere. The send gives
                    // up and leaves the pending record with no marker.
                    return .ok(chatReceipt(
                        "source_in_progress", tool: "calendar.create_event"
                    ))
                }
                if seen == 4 {
                    // The resume's by-id read: parked now, action on it.
                    var receipt = chatReceipt(
                        "source_in_progress", tool: "calendar.create_event"
                    )
                    receipt["device_actions"] = [Self.actionPayload()]
                    return .ok(receipt)
                }
                return .ok(chatReceipt(
                    "succeeded", tool: "calendar.create_event", recordID: "EK-NEW-1"
                ))
            case ("POST", let path) where path.hasPrefix("/v1/device-actions/"):
                return .ok(chatReceipt(
                    "succeeded", tool: "calendar.create_event", recordID: "EK-NEW-1"
                ))
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let executor = StubDeviceActionExecutor()
        let (chat, session, _) = try await makeChat(
            service: service, deviceActionExecutor: executor
        )
        executor.backend = session

        // The send detaches and exhausts its polls without ever seeing an
        // action; the resume is the delivery.
        let detached = try await chat.send(text: "周六下午三点网球")
        #expect(executor.actions.count == 0)
        #expect(detached.state == .sourceInProgress)

        let final = try await chat.resume()

        #expect(executor.actions.count == 1)
        #expect(final?.state == .succeeded)
    }

    // --- second review F2: concurrent resumes must not re-execute ---------

    /// F2, reproduced: two `resume()` calls read the pending slot before
    /// either network reply lands (both see `operationID == nil`), the first
    /// reply anchors the operation and executes the action, then the second
    /// reply *writes its stale in-memory copy of the slot back* — erasing the
    /// `deliveredActionID` marker — and executes the same action a second
    /// time. The marker must survive a concurrent anchor write: the slot is
    /// merged from disk after the network wait, never overwritten from
    /// memory.
    ///
    /// In list form the claim is per item, so the race is per item too: the
    /// two resumes may legitimately split the list between them (each item is
    /// its own operation, and whichever resume claims it runs it), but no item
    /// may run twice.
    @Test("two concurrent resumes anchor once and execute each item once")
    func concurrentResumesExecuteOnce() async throws {
        let service = Service()
        service.answer { call, seen in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                // Both resumes re-present the same idempotent key; the second
                // reply is the server's replay of the first. The real server
                // answers a replay with the operation's current projection —
                // parked, with the whole plan on it.
                var receipt = chatReceipt("source_in_progress", tool: "calendar.create_event")
                receipt["device_actions"] = Self.actionPair()
                return .ok(receipt)
            case ("GET", "/v1/operations/op-1"):
                return .ok(chatReceipt(
                    "succeeded", tool: "calendar.create_event", recordID: "EK-NEW-1"
                ))
            case ("POST", let path) where path.hasPrefix("/v1/device-actions/"):
                return .ok(chatReceipt(
                    "succeeded", tool: "calendar.create_event", recordID: "EK-NEW-1"
                ))
            default:
                // A request this scenario did not plan is a defect in the
                // test, not a silent 404: surface it as a loud 400.
                return .error(400, "INVALID_ARGUMENT")
            }
        }
        let executor = StubDeviceActionExecutor()
        let store = InMemoryCredentialStore()
        let session = try makeChatSession(service: service, store: store)
        _ = try await session.enroll(code: "code", displayName: "iPhone")
        let chat = ChatTimeline(
            backend: session, store: store, deviceActionExecutor: executor,
            pollDelays: Array(repeating: .zero, count: 4), sleep: { _ in }
        )
        executor.backend = session

        // Seed the pending slot the way a crash mid-send leaves it: the key
        // is durable, the operation never anchored.
        let pending = ChatTimeline.PendingSend(
            idempotencyKey: "018f0000-0000-4000-8000-00000000f00d",
            conversationID: chatTimelineID,
            text: "周六下午三点网球",
            clarificationOf: nil,
            startNewSession: nil,
            operationID: nil
        )
        try store.write(
            CredentialKey.pendingChatSend,
            value: try JSONEncoder().encode(pending)
        )

        // Two concurrent resumes over the same slot.
        async let a: OperationReceipt? = chat.resume()
        async let b: OperationReceipt? = chat.resume()
        _ = try await [a, b] as [OperationReceipt?]

        // Each item exactly once — the two resumes may split the list, but
        // neither may re-run an item the other already claimed.
        #expect(executor.actions.count == 2, "each item must execute exactly once")
        #expect(
            Set(executor.actions.map(\.actionID))
                == [Self.primaryID, Self.siblingID]
        )
        #expect(service.count("POST", "/v1/device-actions/\(Self.primaryID)/result") == 1)
        #expect(service.count("POST", "/v1/device-actions/\(Self.siblingID)/result") == 1)
    }

    // --- third review G1: a late reply must not steal another message's slot --

    /// G1, reproduced: resume A's re-present POST is in flight when the user
    /// discards A and sends B — the slot now holds B's key. A's reply lands
    /// and the anchor merge (which checked only `operationID == nil`) writes
    /// op-A into B's slot and executes action-A, while B's own action never
    /// runs. The merge must carry and verify the *requesting* message's
    /// idempotency key: a slot that now belongs to a different message is not
    /// this reply's to anchor.
    @Test("a late reply cannot anchor into another message's slot")
    func lateReplyCannotStealAReplacedSlot() async throws {
        let service = Service()
        service.answer { call, seen in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                // Whichever message's POST this is, the server parks an
                // operation and hands the action over.
                let key = call.idempotencyKey ?? "?"
                var receipt = chatReceipt(
                    "source_in_progress", tool: "calendar.create_event"
                )
                receipt["device_actions"] = [Self.actionPayload()]
                // Distinguish the two operations so the test can assert who
                // executed what.
                receipt["operation_id"] = "op-a"
                _ = key
                return .ok(receipt)
            case ("GET", "/v1/operations/op-a"):
                return .ok(chatReceipt(
                    "succeeded", tool: "calendar.create_event", recordID: "EK-A"
                ))
            case ("POST", let path) where path.hasPrefix("/v1/device-actions/"):
                return .ok(chatReceipt(
                    "succeeded", tool: "calendar.create_event", recordID: "EK-A"
                ))
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let executor = StubDeviceActionExecutor()
        let store = InMemoryCredentialStore()
        let session = try makeChatSession(service: service, store: store)
        _ = try await session.enroll(code: "code", displayName: "iPhone")
        let chat = ChatTimeline(
            backend: session, store: store, deviceActionExecutor: executor,
            pollDelays: Array(repeating: .zero, count: 4), sleep: { _ in }
        )
        executor.backend = session

        // The POST holds its response until the slot has demonstrably been
        // taken over by B (the trail suite's gate pattern): this makes the
        // steal land strictly between A's request and A's reply, which a
        // fixed sleep could only race.
        final class FlagBox: @unchecked Sendable {
            private let lock = NSLock()
            private var value = false
            var isSet: Bool { lock.withLock { value } }
            func set() { lock.withLock { value = true } }
        }
        let stolen = FlagBox()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                waitForGate("A's slot is replaced by B") { stolen.isSet }
                var receipt = chatReceipt(
                    "source_in_progress", tool: "calendar.create_event"
                )
                receipt["device_actions"] = Self.actionPair()
                return .ok(receipt)
            case ("GET", "/v1/operations/op-1"):
                return .ok(chatReceipt(
                    "succeeded", tool: "calendar.create_event", recordID: "EK-A"
                ))
            case ("POST", let path) where path.hasPrefix("/v1/device-actions/"):
                return .ok(chatReceipt(
                    "succeeded", tool: "calendar.create_event", recordID: "EK-A"
                ))
            default:
                return .error(404, "NOT_FOUND")
            }
        }

        // Seed A's slot the way a crash mid-send leaves it.
        let a = ChatTimeline.PendingSend(
            idempotencyKey: "018f0000-0000-4000-8000-00000000aaa1",
            conversationID: chatTimelineID,
            text: "周六下午三点网球",
            clarificationOf: nil,
            startNewSession: nil,
            operationID: nil
        )
        try store.write(
            CredentialKey.pendingChatSend, value: try JSONEncoder().encode(a)
        )

        // A's resume is in flight, its POST held open; meanwhile the user
        // discards A and sends B — the slot is replaced with B's record.
        async let resumed: OperationReceipt? = chat.resume()
        waitForGate("the resume's POST is in flight") {
            service.count("POST", "/v1/chat/messages") >= 1
        }
        let b = ChatTimeline.PendingSend(
            idempotencyKey: "018f0000-0000-4000-8000-00000000bbb2",
            conversationID: chatTimelineID,
            text: "周一上午十点体检",
            clarificationOf: nil,
            startNewSession: nil,
            operationID: nil
        )
        try store.write(
            CredentialKey.pendingChatSend, value: try JSONEncoder().encode(b)
        )
        stolen.set()

        _ = try await resumed

        // A's late reply must NOT have anchored op-a into B's slot, and must
        // not have executed A's action: the slot now belongs to B.
        let slot = try store.read(CredentialKey.pendingChatSend)
            .flatMap { try? JSONDecoder().decode(
                ChatTimeline.PendingSend.self, from: $0
            ) }
        #expect(slot?.idempotencyKey == "018f0000-0000-4000-8000-00000000bbb2")
        #expect(
            slot?.deliveredActionIDs.isEmpty == true,
            "A's marker must not have been written into B's slot"
        )
        #expect(
            executor.actions.isEmpty,
            "no item of A's plan may execute against B's slot"
        )
    }

    // --- fourth review H1: a late settled report must not clear a replaced slot --

    /// H1, reproduced: A's action executed and its report POST is held open;
    /// the user discards A and sends B (the slot now holds B's key); A's
    /// report is then released and answers settled. The clear path after the
    /// report deleted B's pending — B's own reply could then never anchor and
    /// B's action never executed. Clearing must verify the slot's owner
    /// (idempotency key) and its operation, exactly like the anchor merge.
    ///
    /// In list form the ownership rule applies per item, and the pin is twofold:
    /// item 0, already claimed before the takeover, still runs and reports; the
    /// sibling, not yet claimed, is refused because the slot is no longer this
    /// message's to run. That refusal is the honest fail-closed outcome — the
    /// server's sweep parks it for review rather than this device writing an
    /// event for a message the user discarded.
    @Test("a late settled report does not clear another message's slot")
    func lateSettledReportDoesNotClearAReplacedSlot() async throws {
        // The gate holds A's report POST until the test has demonstrably
        // replaced the slot with B.
        final class FlagBox: @unchecked Sendable {
            private let lock = NSLock()
            private var value = false
            var isSet: Bool { lock.withLock { value } }
            func set() { lock.withLock { value = true } }
        }
        let replaced = FlagBox()
        let service = Service()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                var receipt = chatReceipt(
                    "source_in_progress", tool: "calendar.create_event"
                )
                receipt["device_actions"] = Self.actionPair()
                return .ok(receipt)
            case ("POST", let path) where path.hasPrefix("/v1/device-actions/"):
                // A's report hangs until B owns the slot.
                waitForGate("A's slot is replaced by B") { replaced.isSet }
                return .ok(chatReceipt(
                    "succeeded", tool: "calendar.create_event", recordID: "EK-A"
                ))
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let executor = StubDeviceActionExecutor()
        let store = InMemoryCredentialStore()
        let session = try makeChatSession(service: service, store: store)
        _ = try await session.enroll(code: "code", displayName: "iPhone")
        let chat = ChatTimeline(
            backend: session, store: store, deviceActionExecutor: executor,
            pollDelays: Array(repeating: .zero, count: 4), sleep: { _ in }
        )
        executor.backend = session

        // Seed A's slot; its resume anchors and executes, then blocks inside
        // the held report.
        let a = ChatTimeline.PendingSend(
            idempotencyKey: "018f0000-0000-4000-8000-00000000aaa1",
            conversationID: chatTimelineID,
            text: "周六下午三点网球",
            clarificationOf: nil,
            startNewSession: nil,
            operationID: nil
        )
        try store.write(
            CredentialKey.pendingChatSend, value: try JSONEncoder().encode(a)
        )
        async let resumed: OperationReceipt? = chat.resume()
        // A's action has executed and its report POST is in flight.
        waitForGate("A's action executed") { !executor.actions.isEmpty }
        waitForGate("A's report POST is in flight") {
            service.log.contains { call in
                call.method == "POST" && call.path.hasPrefix("/v1/device-actions/")
            }
        }

        // The user discards A and sends B: the slot is replaced with B's key.
        let b = ChatTimeline.PendingSend(
            idempotencyKey: "018f0000-0000-4000-8000-00000000bbb2",
            conversationID: chatTimelineID,
            text: "周一上午十点体检",
            clarificationOf: nil,
            startNewSession: nil,
            operationID: nil
        )
        try store.write(
            CredentialKey.pendingChatSend, value: try JSONEncoder().encode(b)
        )
        // Release A's report: it answers settled.
        replaced.set()

        _ = try await resumed

        // B's slot must survive A's settled report.
        let slot = try store.read(CredentialKey.pendingChatSend)
            .flatMap { try? JSONDecoder().decode(
                ChatTimeline.PendingSend.self, from: $0
            ) }
        #expect(slot?.idempotencyKey == "018f0000-0000-4000-8000-00000000bbb2")
        #expect(slot?.operationID == nil, "A's report must not anchor into B's slot")
        // The ownership rule applies per item: item 0 was claimed while the
        // slot was still A's and still runs; the sibling was not, and the slot
        // having moved on is exactly what stops this device writing an event
        // for a message the user discarded. It is left parked for the sweep.
        #expect(executor.actions.map(\.actionID) == [Self.primaryID])
    }

    // --- fifth review I1: a late refusal must not clear a replaced slot ------

    /// I1: the `provesNotAnchored` catch in both `send()` and `resume()`
    /// cleared the slot unconditionally. A's POST hangs; the user discards A
    /// and sends B; A's refusal (badRequest) then lands and deletes B's
    /// pending — stranding B's reply with no slot to anchor into. Both paths
    /// verified separately.
    private func replacedSlotSurvivesALateRefusal(
        route: String,
        drive: @escaping @Sendable (ChatTimeline) async throws -> Void
    ) async throws {
        final class FlagBox: @unchecked Sendable {
            private let lock = NSLock()
            private var value = false
            var isSet: Bool { lock.withLock { value } }
            func set() { lock.withLock { value = true } }
        }
        let replaced = FlagBox()
        let service = Service()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                // A's POST hangs until B owns the slot, then refuses.
                waitForGate("\(route): A's slot is replaced by B") { replaced.isSet }
                return .error(400, "INVALID_ARGUMENT")
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let executor = StubDeviceActionExecutor()
        let store = InMemoryCredentialStore()
        let session = try makeChatSession(service: service, store: store)
        _ = try await session.enroll(code: "code", displayName: "iPhone")
        let chat = ChatTimeline(
            backend: session, store: store, deviceActionExecutor: executor,
            pollDelays: Array(repeating: .zero, count: 4), sleep: { _ in }
        )
        executor.backend = session
        // `send()` refuses to run without a bound conversation.
        await chat.bind(conversationID: chatTimelineID)

        // Resume needs a pre-existing slot (it is the crash-recovery path);
        // send mints its own, so the helper only seeds on the resume route —
        // seeding for send would just make `send` refuse with
        // `unresolvedSend` before any POST left.
        if route == "resume" {
            let a = ChatTimeline.PendingSend(
                idempotencyKey: "018f0000-0000-4000-8000-00000000aaa1",
                conversationID: chatTimelineID,
                text: "周六下午三点网球",
                clarificationOf: nil,
                startNewSession: nil,
                operationID: nil
            )
            try store.write(
                CredentialKey.pendingChatSend, value: try JSONEncoder().encode(a)
            )
        }
        async let driven: Void = try drive(chat)
        waitForGate("\(route): A's POST is in flight") {
            service.count("POST", "/v1/chat/messages") >= 1
        }

        // The user discards A (a send can only start from an empty slot, so
        // the discard comes first on this path) and sends B: the slot now
        // holds B's key.
        if route == "send" {
            try await chat.discardPending()
        }
        let b = ChatTimeline.PendingSend(
            idempotencyKey: "018f0000-0000-4000-8000-00000000bbb2",
            conversationID: chatTimelineID,
            text: "周一上午十点体检",
            clarificationOf: nil,
            startNewSession: nil,
            operationID: nil
        )
        try store.write(
            CredentialKey.pendingChatSend, value: try JSONEncoder().encode(b)
        )
        // Release A's refusal.
        replaced.set()
        _ = try? await driven

        // B's slot must survive A's late refusal.
        let slot = try store.read(CredentialKey.pendingChatSend)
            .flatMap { try? JSONDecoder().decode(
                ChatTimeline.PendingSend.self, from: $0
            ) }
        #expect(
            slot?.idempotencyKey == "018f0000-0000-4000-8000-00000000bbb2",
            "\(route): A's refusal deleted B's pending"
        )
    }

    @Test("a late POST refusal does not clear a replaced slot (resume path)")
    func lateRefusalDoesNotClearAReplacedSlotOnResume() async throws {
        try await replacedSlotSurvivesALateRefusal(route: "resume") { chat in
            _ = try await chat.resume()
        }
    }

    @Test("a late POST refusal does not clear a replaced slot (send path)")
    func lateRefusalDoesNotClearAReplacedSlotOnSend() async throws {
        try await replacedSlotSurvivesALateRefusal(route: "send") { chat in
            _ = try await chat.send(text: "周六下午三点网球")
        }
    }

    private static func actionPayload() -> [String: Any] {
        [
            "action_id": "018f0000-0000-7000-8000-00000000cafe",
            "tool": "calendar.create_event",
            "event": [
                "title": "网球",
                "start": "2026-09-12T15:00:00Z",
                "end": "2026-09-12T16:30:00Z",
                "all_day": false,
            ],
        ]
    }

    /// The two ids the list-form counterexamples below use: item 0 (the
    /// message's own operation) and one sibling.
    static let primaryID = "018f0000-0000-7000-8000-00000000cafe"
    static let siblingID = "018f0000-0000-7000-8000-00000000face"

    /// The list the slot-ownership counterexamples carry (design §12: G1/H1/
    /// F2 全部并发反例在列表形态重写). One item would only re-run the
    /// single-action shape under a new name; the point is that each item is an
    /// independent operation with its own marker, and the races have to be
    /// re-won per item.
    static func actionPair() -> [[String: Any]] {
        var sibling = actionPayload()
        sibling["action_id"] = siblingID
        var event = sibling["event"] as! [String: Any]
        event["title"] = "体检"
        sibling["event"] = event
        return [actionPayload(), sibling]
    }
}

// --- the plural list (design §4.1/§4.2, §13 step 6 items 2–3) -----------------
//
// The failure shapes are designed before the implementation, as §5.1 requires.
// The list introduces exactly three new ways for the client to be wrong, and
// each one gets a test:
//
// - **running only the first item.** The server parked N operations and will
//   never hand a sibling over anywhere else; a client that stops at item 0
//   leaves those parked until the sweep calls them needs_manual_review.
// - **running an item twice.** The list arrives again on every reply that reads
//   the parked projection, so the marker has to be per item, not per reply.
// - **letting one bad row swallow the good ones.** A malformed element is a
//   refusal that names no action; it must not shrink the plan, and it must not
//   stop the siblings that *did* decode from running.
//
// The legacy singular branch is exercised too: it is the rollback path (design
// §14.2), and a branch nothing tests is a branch that is wrong the first time
// it is needed.

@Suite("The plural device-action list", .serialized)
struct DeviceActionListTests {

    private static let firstID = "018f0000-0000-7000-8000-000000000001"
    private static let secondID = "018f0000-0000-7000-8000-000000000002"

    private static func action(_ id: String, title: String) -> [String: Any] {
        [
            "action_id": id,
            "tool": "calendar.create_event",
            "event": [
                "title": title,
                "start": "2026-09-12T15:00:00Z",
                "end": "2026-09-12T16:30:00Z",
                "all_day": false,
            ],
        ]
    }

    private static func pair() -> [[String: Any]] {
        [
            action(firstID, title: "网球"),
            action(secondID, title: "体检"),
        ]
    }

    /// The report endpoint's answer, driven by the action the path names. Each
    /// action settles its *own* operation, so the fixture answers per action
    /// rather than with one blanket reply — a fake that settled every report
    /// the same way could not show a sibling failing on its own.
    private static func actionID(from path: String) -> String {
        path.replacingOccurrences(of: "/v1/device-actions/", with: "")
            .replacingOccurrences(of: "/result", with: "")
    }

    @Test("a reply carrying several actions runs and reports every one")
    func everyDeliveredActionRuns() async throws {
        let service = Service()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                var receipt = chatReceipt(
                    "source_in_progress", tool: "calendar.create_event"
                )
                receipt["device_actions"] = Self.pair()
                return .ok(receipt)
            case ("POST", let path) where path.hasPrefix("/v1/device-actions/"):
                return .ok(chatReceipt(
                    "succeeded", tool: "calendar.create_event",
                    recordID: "EK-\(Self.actionID(from: path))"
                ))
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let executor = StubDeviceActionExecutor()
        let (chat, session, _) = try await makeChat(
            service: service, deviceActionExecutor: executor
        )
        executor.backend = session

        let final = try await chat.send(text: "周六网球，周一上午体检")

        // Both ran, in plan order — not just item 0.
        #expect(executor.actions.map(\.actionID) == [Self.firstID, Self.secondID])
        // And each was reported under its own action id, exactly once.
        #expect(service.count("POST", "/v1/device-actions/\(Self.firstID)/result") == 1)
        #expect(service.count("POST", "/v1/device-actions/\(Self.secondID)/result") == 1)
        // The turn's card is the message's own operation — item 0's.
        #expect(final.state == .succeeded)
        #expect(final.operationID == "op-1")
    }

    @Test("a second delivery of the same list re-runs nothing")
    func aredeliveredListIsANoOp() async throws {
        let service = Service()
        service.answer { call, seen in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                var receipt = chatReceipt(
                    "source_in_progress", tool: "calendar.create_event"
                )
                receipt["device_actions"] = Self.pair()
                return .ok(receipt)
            case ("POST", let path) where path.hasPrefix("/v1/device-actions/"):
                // Item 0's report is accepted but its operation has not
                // settled yet: the client must poll, and the poll re-delivers
                // the whole list.
                if path.contains(Self.firstID), seen == 0 {
                    return .ok(chatReceipt(
                        "source_in_progress", tool: "calendar.create_event"
                    ))
                }
                return .ok(chatReceipt(
                    "succeeded", tool: "calendar.create_event", recordID: "EK-1"
                ))
            case ("GET", "/v1/operations/op-1"):
                if seen == 0 {
                    var parked = chatReceipt(
                        "source_in_progress", tool: "calendar.create_event"
                    )
                    parked["device_actions"] = Self.pair()
                    return .ok(parked)
                }
                return .ok(chatReceipt(
                    "succeeded", tool: "calendar.create_event", recordID: "EK-1"
                ))
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let executor = StubDeviceActionExecutor()
        let (chat, session, _) = try await makeChat(
            service: service, deviceActionExecutor: executor
        )
        executor.backend = session

        let final = try await chat.send(text: "周六网球，周一上午体检")

        // Each action ran once across the reply *and* the re-delivering poll.
        #expect(executor.actions.map(\.actionID) == [Self.firstID, Self.secondID])
        #expect(service.count("POST", "/v1/device-actions/\(Self.firstID)/result") == 1)
        #expect(service.count("POST", "/v1/device-actions/\(Self.secondID)/result") == 1)
        #expect(final.state == .succeeded)
    }

    @Test("an unreadable element does not drop its siblings")
    func oneBadRowDoesNotSwallowTheOthers() async throws {
        let service = Service()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                var receipt = chatReceipt(
                    "source_in_progress", tool: "calendar.create_event"
                )
                // A string where an action should be: the element cannot name
                // an action, so nothing about it is reportable.
                receipt["device_actions"] = [
                    Self.action(Self.firstID, title: "网球"),
                    "this is not an action",
                ]
                return .ok(receipt)
            case ("POST", let path) where path.hasPrefix("/v1/device-actions/"):
                return .ok(chatReceipt(
                    "succeeded", tool: "calendar.create_event", recordID: "EK-1"
                ))
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let executor = StubDeviceActionExecutor()
        let (chat, session, _) = try await makeChat(
            service: service, deviceActionExecutor: executor
        )
        executor.backend = session

        _ = try await chat.send(text: "周六网球")

        // The sibling that decoded still ran...
        #expect(executor.actions.map(\.actionID) == [Self.firstID])
        // ...and nothing was reported for the row that could not be named:
        // exactly one report left this device, for the one readable action.
        let reports = service.log.filter { $0.path.hasPrefix("/v1/device-actions/") }
        #expect(reports.map { $0.path } == ["/v1/device-actions/\(Self.firstID)/result"])
    }

    @Test("a refused sibling is reported without stopping the ones after it")
    func aRefusedSiblingDoesNotStopTheRest() async throws {
        let service = Service()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                var unknown = Self.action(Self.firstID, title: "网球")
                unknown["tool"] = "future.tool"
                var receipt = chatReceipt(
                    "source_in_progress", tool: "calendar.create_event"
                )
                receipt["device_actions"] = [
                    unknown, Self.action(Self.secondID, title: "体检"),
                ]
                return .ok(receipt)
            case ("POST", let path) where path.hasPrefix("/v1/device-actions/"):
                if Self.actionID(from: path) == Self.firstID {
                    return .ok(chatReceipt(
                        "failed_safe", failureReason: "DEVICE_EXECUTION_FAILED"
                    ))
                }
                return .ok(chatReceipt(
                    "succeeded", tool: "calendar.create_event", recordID: "EK-2"
                ))
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let executor = StubDeviceActionExecutor()
        let (chat, session, _) = try await makeChat(
            service: service, deviceActionExecutor: executor
        )
        executor.backend = session

        _ = try await chat.send(text: "周六网球，周一上午体检")

        // The tool this build does not run was never handed to EventKit...
        #expect(executor.actions.map(\.actionID) == [Self.secondID])
        // ...it was reported as a known failure instead of being dropped...
        let first = service.calls(
            "POST", "/v1/device-actions/\(Self.firstID)/result"
        )
        #expect(first.count == 1)
        #expect(first.first?.string("result") == "failed")
        // ...and the sibling that followed it still ran and reported.
        #expect(service.count("POST", "/v1/device-actions/\(Self.secondID)/result") == 1)
    }

    @Test("the message's action still runs when its own report is lost")
    func siblingsRunWhenThePrimaryReportIsLost() async throws {
        let service = Service()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                var receipt = chatReceipt(
                    "source_in_progress", tool: "calendar.create_event"
                )
                receipt["device_actions"] = Self.pair()
                return .ok(receipt)
            case ("GET", "/v1/operations/op-1"):
                return .ok(chatReceipt(
                    "succeeded", tool: "calendar.create_event", recordID: "EK-1"
                ))
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let executor = StubDeviceActionExecutor()
        executor.reportFails = true
        let (chat, session, _) = try await makeChat(
            service: service, deviceActionExecutor: executor
        )
        executor.backend = session

        let final = try await chat.send(text: "周六网球，周一上午体检")

        // A lost report reply does not skip the items behind it: both writes
        // still happened, and the server's sweep is the witness for the
        // testimony that never arrived.
        #expect(executor.actions.map(\.actionID) == [Self.firstID, Self.secondID])
        #expect(service.log.filter { $0.path.hasPrefix("/v1/device-actions/") }.isEmpty)
        // The turn degrades to the polled state, never to a made-up receipt.
        #expect(final.state == .succeeded)
    }

    @Test("an empty list is no action at all")
    func anEmptyListNeverReachesTheExecutor() async throws {
        let service = Service()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                var receipt = chatReceipt("succeeded", tool: "calendar.create_event")
                receipt["device_actions"] = []
                return .ok(receipt)
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let executor = StubDeviceActionExecutor()
        let (chat, session, _) = try await makeChat(
            service: service, deviceActionExecutor: executor
        )
        executor.backend = session

        let final = try await chat.send(text: "周六网球")

        #expect(executor.actions.isEmpty)
        #expect(service.log.filter { $0.path.hasPrefix("/v1/device-actions/") }.isEmpty)
        #expect(final.state == .succeeded)
    }

    @Test("a list is never merged with a legacy singular field")
    func aListIsNeverMergedWithTheLegacyField() async throws {
        let service = Service()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                var receipt = chatReceipt(
                    "source_in_progress", tool: "calendar.create_event"
                )
                receipt["device_actions"] = [Self.action(Self.firstID, title: "网球")]
                // A response carrying both must not smuggle a second action
                // past the list.
                receipt["device_action"] = Self.action(Self.secondID, title: "体检")
                return .ok(receipt)
            case ("POST", let path) where path.hasPrefix("/v1/device-actions/"):
                return .ok(chatReceipt(
                    "succeeded", tool: "calendar.create_event", recordID: "EK-1"
                ))
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let executor = StubDeviceActionExecutor()
        let (chat, session, _) = try await makeChat(
            service: service, deviceActionExecutor: executor
        )
        executor.backend = session

        _ = try await chat.send(text: "周六网球")

        #expect(executor.actions.map(\.actionID) == [Self.firstID])
        #expect(service.calls("POST", "/v1/device-actions/\(Self.secondID)/result").isEmpty)
    }

    /// The rollback path (design §14.2): a build at v2 rolled back to the
    /// previous API, which still answers with the singular field.
    @Test("the legacy singular field still runs")
    func theLegacySingularFieldStillRuns() async throws {
        let service = Service()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                var receipt = chatReceipt(
                    "source_in_progress", tool: "calendar.create_event"
                )
                receipt["device_action"] = Self.action(Self.firstID, title: "网球")
                return .ok(receipt)
            case ("POST", let path) where path.hasPrefix("/v1/device-actions/"):
                return .ok(chatReceipt(
                    "succeeded", tool: "calendar.create_event", recordID: "EK-1"
                ))
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let executor = StubDeviceActionExecutor()
        let (chat, session, _) = try await makeChat(
            service: service, deviceActionExecutor: executor
        )
        executor.backend = session

        _ = try await chat.send(text: "周六网球")

        #expect(executor.actions.map(\.actionID) == [Self.firstID])
        #expect(service.count("POST", "/v1/device-actions/\(Self.firstID)/result") == 1)
    }
}

// --- the pending slot's delivery marker across the list change ---------------

@Suite("The pending slot's delivery marker")
struct PendingSendMarkerTests {

    private static let firstID = "018f0000-0000-7000-8000-000000000001"
    private static let secondID = "018f0000-0000-7000-8000-000000000002"

    private func slot(_ json: [String: Any]) throws -> ChatTimeline.PendingSend {
        try JSONDecoder().decode(
            ChatTimeline.PendingSend.self, from: chatJSON(json)
        )
    }

    private func base() -> [String: Any] {
        [
            "idempotencyKey": "018f0000-0000-4000-8000-0000000000aa",
            "conversationID": chatTimelineID,
            "text": "周六网球，周一上午体检",
            "clarificationOf": NSNull(),
            "operationID": "op-1",
        ]
    }

    /// A slot written by the build before the list must not read as empty on
    /// the build after it. Losing that marker is how a restart re-executes a
    /// write the server may have settled long ago.
    @Test("a single-action marker is read into the list")
    func legacyMarkerIsRead() throws {
        var json = base()
        json["deliveredActionID"] = Self.firstID
        let decoded = try slot(json)
        #expect(decoded.deliveredActionIDs == [Self.firstID])
    }

    /// The reverse direction, which is why the legacy field is still written:
    /// a build rolled back to one action per slot reads an empty marker as
    /// "nothing ran yet" and executes a second time.
    @Test("the encoder still writes the first claim as the legacy field")
    func theLegacyFieldIsStillWritten() throws {
        let encoded = try JSONEncoder().encode(
            ChatTimeline.PendingSend(
                idempotencyKey: "018f0000-0000-4000-8000-0000000000aa",
                conversationID: chatTimelineID,
                text: "周六网球，周一上午体检",
                clarificationOf: nil,
                operationID: "op-1",
                deliveredActionIDs: [Self.firstID, Self.secondID]
            )
        )
        let json = try #require(
            try JSONSerialization.jsonObject(with: encoded) as? [String: Any]
        )
        #expect(json["deliveredActionID"] as? String == Self.firstID)
        #expect(json["deliveredActionIDs"] as? [String] == [Self.firstID, Self.secondID])
    }

    /// A response that carries both must not double-count: the same action
    /// must not appear twice in the marker, or a later membership test still
    /// works but the list stops describing the plan.
    @Test("an action named by both fields is claimed once")
    func bothFieldsDoNotDoubleCount() throws {
        var json = base()
        json["deliveredActionID"] = Self.firstID
        json["deliveredActionIDs"] = [Self.firstID, Self.secondID]
        let decoded = try slot(json)
        #expect(decoded.deliveredActionIDs == [Self.firstID, Self.secondID])
    }

    @Test("a slot that never delivered anything reads as no claims")
    func aFreshSlotHasNoClaims() throws {
        let decoded = try slot(base())
        #expect(decoded.deliveredActionIDs.isEmpty)
    }
}
