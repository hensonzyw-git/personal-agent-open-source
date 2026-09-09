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

    func executeAndReport(
        _ action: DeviceEventAction, settlesOperationID: String
    ) async -> OperationReceipt {
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
            return OperationReceipt(
                operationID: settlesOperationID,
                state: .sourceInProgress, cancelRequested: false,
                clientDetached: false, tool: nil, recordID: nil, failureReason: nil,
                duplicateCheckID: nil, clarification: nil, duplicateExisting: nil,
                answer: nil
            )
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

    /// A chat send whose reply carries a `device_action` and parks the
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
                receipt["device_action"] = Self.actionPayload()
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
                receipt["device_action"] = payload
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
                receipt["device_action"] = payload
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
                receipt["device_action"] = Self.actionPayload()
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
                    receipt["device_action"] = Self.actionPayload()
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
                    receipt["device_action"] = Self.actionPayload()
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
                    receipt["device_action"] = Self.actionPayload()
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
    @Test("two concurrent resumes anchor once and execute once")
    func concurrentResumesExecuteOnce() async throws {
        let service = Service()
        service.answer { call, seen in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                // Both resumes re-present the same idempotent key; the second
                // reply is the server's replay of the first. The real server
                // answers a replay with the operation's current projection —
                // parked, with the action on it.
                var receipt = chatReceipt("source_in_progress", tool: "calendar.create_event")
                receipt["device_action"] = Self.actionPayload()
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

        #expect(executor.actions.count == 1, "the same action must execute exactly once")
        #expect(service.count("POST", "/v1/device-actions/018f0000-0000-7000-8000-00000000cafe/result") == 1)
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
                receipt["device_action"] = Self.actionPayload()
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
                receipt["device_action"] = Self.actionPayload()
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
            slot?.deliveredActionID == nil,
            "A's marker must not have been written into B's slot"
        )
        #expect(
            executor.actions.isEmpty,
            "A's action must not execute against B's slot"
        )
    }

    // --- fourth review H1: a late settled report must not clear a replaced slot --

    /// H1, reproduced: A's action executed and its report POST is held open;
    /// the user discards A and sends B (the slot now holds B's key); A's
    /// report is then released and answers settled. The clear path after the
    /// report deleted B's pending — B's own reply could then never anchor and
    /// B's action never executed. Clearing must verify the slot's owner
    /// (idempotency key) and its operation, exactly like the anchor merge.
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
                receipt["device_action"] = Self.actionPayload()
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
}
