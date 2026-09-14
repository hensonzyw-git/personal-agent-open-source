import Foundation
import Testing

@testable import PersonalAgentKit

/// `DEV-030`'s failure set, designed before the implementation as §5.1 requires.
///
/// The cases are chosen against the three constraints the task names, and each one
/// is a way the client could lie to Henson about his own ledger:
///
/// - a `succeeded` with no external evidence, a `failed_safe` carrying reassuring
///   prose, and a state this build does not know — none of which may render as a
///   recorded expense;
/// - a dropped reply, a crash before the request, and an app restart — none of
///   which may produce a second write or a second idempotency key;
/// - a cursor that the server refuses, a quiet incremental page, and an old page
///   whose `newer_cursor` points at the middle of history — none of which may lose
///   or duplicate the Timeline.
///
/// The counterparty is the **real** `AgentClient` and the **real** `DeviceSession`
/// over a stubbed `URLProtocol`, so the real JSON decoding, the real token policy
/// and the real request construction are all in the loop. Only HTTP is fake.

// --- the stub service --------------------------------------------------------
//
// This harness is shared with `DuplicateDecisionTests.swift` and
// `ReviewCenterTests.swift` (`DEV-031`), which is why the declarations below
// are module-internal rather than file-private. Everything else in this file
// stays private.

struct Call: Sendable {
    let method: String
    let path: String
    let query: [String: String]
    let headers: [String: String]
    /// Scalar JSON fields are split by type so the recorded call stays Sendable.
    let body: [String: String]
    let booleans: [String: Bool]
    /// The full request JSON as encoded, for the cross-language contract
    /// tests that must assert a *field's presence* the scalars do not carry
    /// (e.g. `snapshot_as_of`, whose absence the real route rejects).
    /// Re-parsed on access from the retained bytes so the recorded call
    /// stays Sendable.
    let rawBody: Data

    var idempotencyKey: String? { headers["Idempotency-Key"] }
    func string(_ field: String) -> String? { body[field] }
    func bool(_ field: String) -> Bool? { booleans[field] }

    /// The parsed JSON object, or an empty dict when the body was not JSON.
    /// Access is confined to tests; a corrupt body reads as empty rather
    /// than trapping, because the assertion that follows is the point.
    var rawJSONBody: [String: Any] {
        (try? JSONSerialization.jsonObject(with: rawBody)) as? [String: Any] ?? [:]
    }
}

struct Reply: Sendable {
    let status: Int
    let body: Data

    static func ok(_ object: [String: Any]) -> Reply {
        .init(status: 200, body: chatJSON(object))
    }

    static func accepted(_ object: [String: Any]) -> Reply {
        .init(status: 202, body: chatJSON(object))
    }

    static func error(_ status: Int, _ code: String) -> Reply {
        .init(
            status: status,
            body: chatJSON([
                "error": [
                    "code": code, "message": "refused", "retryable": false,
                ]
            ])
        )
    }
}

/// One test's fake service, reachable at its own loopback port.
///
/// The port is what keeps the suites independent: a single shared stub let two
/// suites running in parallel answer each other's requests, and the failures
/// looked like routing bugs in the client rather than in the test harness.
final class Service: @unchecked Sendable {
    let port: Int
    var baseURL: URL { URL(string: "http://127.0.0.1:\(port)")! }

    private let lock = NSLock()
    private var calls: [Call] = []
    /// Answers for everything past enrollment and token minting.
    private var route: @Sendable (Call, Int) -> Reply = { _, _ in
        .error(500, "INTERNAL_ERROR")
    }

    init() {
        port = ServiceRegistry.shared.register()
        ServiceRegistry.shared.attach(self, port: port)
    }

    func answer(_ route: @escaping @Sendable (Call, Int) -> Reply) {
        lock.withLock { self.route = route }
    }

    func handle(_ call: Call) -> Reply {
        lock.withLock { calls.append(call) }
        // The real server parses this header with `uuid.UUID(key)` and then
        // requires `str(parsed) == key`, so a canonical *lower-case* UUIDv4 is the
        // only accepted spelling. Refusing anything else here is not extra
        // strictness for its own sake: the first live run against the real service
        // refused every write this client sent, because Foundation's
        // `UUID.uuidString` is upper-case and this stub used to accept it. A fake
        // that is more permissive than the server it stands in for cannot catch
        // that class of defect at all.
        if let key = call.idempotencyKey, !IdempotencyKey.isCanonical(key) {
            return .error(400, "INVALID_ARGUMENT")
        }
        switch (call.method, call.path) {
        case ("POST", "/v1/enrollments/claim"):
            return .init(status: 201, body: chatEnrolledBody)
        case ("POST", "/v1/auth/challenges"):
            return .init(status: 200, body: chatChallengeBody)
        case ("POST", "/v1/auth/tokens"):
            return .init(status: 200, body: chatTokenBody)
        default:
            let seen = lock.withLock {
                calls.filter { $0.method == call.method && $0.path == call.path }.count
            }
            return lock.withLock { route }(call, seen - 1)
        }
    }

    var log: [Call] { lock.withLock { calls } }

    func count(_ method: String, _ path: String) -> Int {
        log.filter { $0.method == method && $0.path == path }.count
    }

    func calls(_ method: String, _ path: String) -> [Call] {
        log.filter { $0.method == method && $0.path == path }
    }

    var chatPosts: [Call] { calls("POST", "/v1/chat/messages") }
    var timelineGets: [Call] { log.filter { $0.path.hasSuffix("/events") } }
}

/// Which fake service owns which loopback port.
final class ServiceRegistry: @unchecked Sendable {
    static let shared = ServiceRegistry()

    private let lock = NSLock()
    private var nextPort = 9_000
    private var services: [Int: Service] = [:]

    func register() -> Int {
        lock.withLock {
            nextPort += 1
            return nextPort
        }
    }

    func attach(_ service: Service, port: Int) {
        lock.withLock { services[port] = service }
    }

    func service(port: Int) -> Service? {
        lock.withLock { services[port] }
    }
}

final class ChatStub: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let components = URLComponents(
            url: request.url!, resolvingAgainstBaseURL: true
        )
        var query: [String: String] = [:]
        for item in components?.queryItems ?? [] {
            query[item.name] = item.value ?? ""
        }
        let call = Call(
            method: request.httpMethod ?? "",
            path: request.url?.path ?? "",
            query: query,
            headers: request.allHTTPHeaderFields ?? [:],
            decodedBody: Self.decodeBody(request),
            rawBody: Self.readBody(request) ?? Data()
        )
        guard let port = request.url?.port,
              let service = ServiceRegistry.shared.service(port: port)
        else {
            client?.urlProtocol(
                self, didFailWithError: URLError(.cannotFindHost)
            )
            return
        }
        // Handle each request off the protocol queue. Synchronous handling
        // here serialises independent requests behind whatever handler runs
        // long — the held POST of the trail suite blocked every by-key poll
        // until it answered, so "polls beside the open POST" could never be
        // observed, and the suite only passed where machine speed let two
        // polls sneak in first. A real server answers independent connections
        // concurrently; the stub must too. Sequential flows are unaffected:
        // one request in flight at a time cannot tell the difference.
        //
        // URLProtocol subclasses are called on a single protocol queue and are
        // not Sendable; the box scopes that promise to exactly this hop. The
        // URLProtocol client callbacks themselves are documented thread-safe.
        let loader = SendableLoader(self)
        let requestURL = request.url!
        DispatchQueue.global().async {
            let reply = service.handle(call)
            let response = HTTPURLResponse(
                url: requestURL,
                statusCode: reply.status,
                httpVersion: "HTTP/1.1",
                headerFields: ["Content-Type": "application/json"]
            )!
            loader.client?.urlProtocol(
                loader.base, didReceive: response, cacheStoragePolicy: .notAllowed
            )
            loader.client?.urlProtocol(loader.base, didLoad: reply.body)
            loader.client?.urlProtocolDidFinishLoading(loader.base)
        }
    }

    /// Escapes a URLProtocol subclass through a `@Sendable` dispatch without
    /// pretending the subclass itself is `Sendable`.
    private final class SendableLoader: @unchecked Sendable {
        let base: ChatStub
        let client: (any URLProtocolClient)?
        init(_ base: ChatStub) {
            self.base = base
            self.client = base.client
        }
    }

    override func stopLoading() {}

    /// `URLProtocol` usually hands the body over as a stream rather than as
    /// `httpBody`, and reading only the latter would silently assert against an
    /// empty request. Both are checked.
    private static func readBody(_ request: URLRequest) -> Data? {
        var data = request.httpBody
        if data == nil, let stream = request.httpBodyStream {
            stream.open()
            var collected = Data()
            let size = 4096
            let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: size)
            defer { buffer.deallocate(); stream.close() }
            while stream.hasBytesAvailable {
                let read = stream.read(buffer, maxLength: size)
                if read <= 0 { break }
                collected.append(buffer, count: read)
            }
            data = collected
        }
        return data
    }

    private static func decodeBody(
        _ request: URLRequest
    ) -> (strings: [String: String], booleans: [String: Bool]) {
        guard let data = readBody(request), !data.isEmpty,
              let object = try? JSONSerialization.jsonObject(with: data)
                as? [String: Any]
        else { return ([:], [:]) }
        return (
            object.compactMapValues { $0 as? String },
            object.compactMapValues { $0 as? Bool }
        )
    }
}

private extension Call {
    init(
        method: String,
        path: String,
        query: [String: String],
        headers: [String: String],
        decodedBody: (strings: [String: String], booleans: [String: Bool]),
        rawBody: Data
    ) {
        self.init(
            method: method,
            path: path,
            query: query,
            headers: headers,
            body: decodedBody.strings,
            booleans: decodedBody.booleans,
            rawBody: rawBody
        )
    }
}

func chatJSON(_ object: [String: Any]) -> Data {
    try! JSONSerialization.data(withJSONObject: object)
}

let chatDeviceID = "018f0000-0000-7000-8000-00000000c0de"
let chatTimelineID = "tl_0000000000000000000000000000c0de"

let chatEnrolledBody = chatJSON([
    "device_id": chatDeviceID,
    "display_name": "iPhone",
    "device_key_thumbprint": "thumb",
    "scopes": ["finance.write", "device.self.read"],
    "allowed_tools_version": "v1",
])

let chatChallengeBody = chatJSON([
    "challenge_id": "chal-1",
    "device_id": chatDeviceID,
    "nonce": DeviceWireContract.base64URLEncode(Data(repeating: 9, count: 32)),
    "audience": "personal-agent-api",
    "expires_at": "2026-07-30T07:02:00+00:00",
])

let chatTokenBody = chatJSON([
    "access_token": "token-1",
    "token_type": "Bearer",
    "expires_in": 600,
    "device_id": chatDeviceID,
    "scopes": ["finance.write", "device.self.read"],
    "allowed_tools_version": "v1",
])

/// The server's operation projection, with every required field present.
func chatReceipt(
    _ state: String,
    operation: String = "op-1",
    cancelRequested: Bool = false,
    clientDetached: Bool = false,
    tool: Any = NSNull(),
    // Present and null by default, which is the server's own shape: it always
    // emits the key, and emits null exactly when no tool was recorded.
    domain: Any = NSNull(),
    recordID: Any = NSNull(),
    failureReason: Any = NSNull(),
    duplicateCheckID: Any = NSNull(),
    extra: [String: Any] = [:]
) -> [String: Any] {
    var body: [String: Any] = [
        "operation_id": operation,
        "state": state,
        "cancel_requested": cancelRequested,
        "client_detached": clientDetached,
        "tool": tool,
        "domain": domain,
        "record_id": recordID,
        "failure_reason": failureReason,
        "duplicate_check_id": duplicateCheckID,
    ]
    for (key, value) in extra { body[key] = value }
    return body
}

func chatPage(
    _ events: [[String: Any]],
    olderCursor: Any = NSNull(),
    newerCursor: Any = NSNull(),
    hasOlder: Bool = false,
    hasNewer: Bool = false,
    conversation: String = chatTimelineID
) -> [String: Any] {
    [
        "conversation_id": conversation,
        "events": events,
        "older_cursor": olderCursor,
        "newer_cursor": newerCursor,
        "has_older": hasOlder,
        "has_newer": hasNewer,
    ]
}

func chatEvent(
    _ id: String,
    type: String = "user_message",
    operation: Any = NSNull(),
    content: [String: Any] = ["text": "咖啡 18 个人支出"]
) -> [String: Any] {
    [
        "event_id": id,
        "event_type": type,
        "operation_id": operation,
        "created_at": "2026-07-30T07:00:00+00:00",
        "content": content,
    ]
}

// --- fixtures ----------------------------------------------------------------

func makeChatSession(
    service: Service, store: CredentialStore
) throws -> DeviceSession {
    let configuration = URLSessionConfiguration.ephemeral
    configuration.protocolClasses = [ChatStub.self]
    let client = try AgentClient(
        baseURL: service.baseURL,
        session: URLSession(configuration: configuration)
    )
    return DeviceSession(client: client, store: store, identityFactory: .softwareForTests, now: {
        Date(timeIntervalSince1970: 1_000)
    })
}

/// An enrolled session plus a bound Timeline, which is the state every chat test
/// starts from. Sleeping is a no-op so the poll schedule costs no wall time.
/// Pass a real `Task.sleep`-backed closure when a test depends on cancellation
/// landing *during* a wait, which a no-op sleep cannot observe.
func makeChat(
    service: Service,
    store: CredentialStore = InMemoryCredentialStore(),
    deviceActionExecutor: DeviceActionExecuting? = nil,
    bind: Bool = true,
    pollDelays: [Duration] = Array(repeating: .zero, count: 4),
    sleep: @escaping @Sendable (Duration) async throws -> Void = { _ in }
) async throws -> (ChatTimeline, DeviceSession, CredentialStore) {
    let session = try makeChatSession(service: service, store: store)
    _ = try await session.enroll(code: "code", displayName: "iPhone")
    let chat = ChatTimeline(
        backend: session, store: store, deviceActionExecutor: deviceActionExecutor,
        pollDelays: pollDelays, sleep: sleep
    )
    if bind { await chat.bind(conversationID: chatTimelineID) }
    return (chat, session, store)
}

// --- `G1`: the business fields, and what must never reach the card ---------------

@Suite("The G1 receipt record")
struct ReceiptRecordTests {
    private func receipt(record: Any?) throws -> OperationReceipt {
        var body = chatReceipt(
            "succeeded", tool: "finance.log_expense", recordID: "rec-1"
        )
        if let record { body["record"] = record }
        return try JSONDecoder().decode(
            OperationReceipt.self, from: chatJSON(body)
        )
    }

    private func aRecord(_ overrides: [String: Any] = [:]) -> [String: Any] {
        var record: [String: Any] = [
            "name": "午饭",
            "amount_cny": "38.50",
            "occurred_on": "2026-08-15",
            "is_family_expense": false,
            "category": "餐饮",
        ]
        for (key, value) in overrides { record[key] = value }
        return record
    }

    @Test("a receipt with no record is still a proven write")
    func noRecordIsStillRecorded() throws {
        // The `idempotent_replay` shape, and every receipt written before `G1`.
        // The card falls back to its status row; the write is still proven.
        let parsed = try receipt(record: nil)
        #expect(parsed.record == nil)
        #expect(parsed.outcome.provesWrite)
    }

    @Test("a malformed record costs the card its fields, never the receipt")
    func malformedRecordFailsClosedOnFieldsOnly() throws {
        // The asymmetry that matters: a bad *presentation* payload must not turn
        // a committed ledger write into a failure the user is invited to retry.
        for broken in [
            aRecord(["amount_cny": 38.5]),          // money as a float
            aRecord(["is_family_expense": "true"]), // the flag as a string
            aRecord(["name": ""]),                  // an empty required field
            aRecord(["category": ""]),              // empty is not the same as null
        ] {
            let parsed = try receipt(record: broken)
            #expect(parsed.record == nil)
            #expect(parsed.outcome.provesWrite, "the write is proven by record_id")
        }
    }

    @Test("a missing family flag is refused, never defaulted to personal")
    func missingFamilyFlagIsRefused() throws {
        var record = aRecord()
        record.removeValue(forKey: "is_family_expense")
        // Defaulting to `false` would silently redraw a family expense as a
        // personal one -- the single field where a wrong default states a wrong
        // accounting fact rather than an incomplete one.
        #expect(try receipt(record: record).record == nil)
    }

    @Test("money is never parsed, so it is never re-rendered")
    func moneyStaysTheLedgersOwnText() throws {
        let record = try #require(
            try receipt(record: aRecord(["amount_cny": "0.10"])).record
        )
        // Not 0.1, and not 0.10000000000000001.
        #expect(record.amount == "0.10")
    }

    @Test("a record on a receipt that proves nothing never reaches the card")
    func recordWithoutEvidenceIsNotShown() throws {
        // A `succeeded` for a governed write with no `record_id` is already
        // `.indeterminate`. Business fields alongside it must not create a
        // second route by which the card claims a write.
        var body = chatReceipt("succeeded", tool: "finance.log_expense")
        body["record"] = aRecord()
        let parsed = try JSONDecoder().decode(
            OperationReceipt.self, from: chatJSON(body)
        )
        #expect(!parsed.outcome.provesWrite)
        if case .recorded = parsed.outcome {
            Issue.record("fields promoted an unproven write to a receipt")
        }
    }

    @Test("a category outside the ledger's options is never sent")
    func unknownCategoryIsRefusedBeforeTheNetwork() {
        #expect(ExpenseCategory.isKnown("餐饮"))
        // The connector creates no select option, so this would be a refused
        // write; refusing locally keeps a governed write from being spent on it.
        #expect(!ExpenseCategory.isKnown("咖啡"))
        #expect(!ExpenseCategory.isKnown(""))
    }
}

// --- the chatReceipt projection: no success from prose ---------------------------

@Suite("The DEV-030 chatReceipt projection")
struct OperationReceiptTests {
    private func decode(_ object: [String: Any]) throws -> OperationReceipt {
        try JSONDecoder().decode(OperationReceipt.self, from: chatJSON(object))
    }

    @Test("a governed write proves itself with an external record id")
    func recordEvidence() throws {
        let parsed = try decode(
            chatReceipt(
                "succeeded", tool: "finance.log_expense", recordID: "rec-42"
            )
        )
        #expect(parsed.outcome == .recorded(recordID: "rec-42", tool: "finance.log_expense", record: nil))
        #expect(parsed.outcome.provesWrite)
    }

    @Test("succeeded without evidence is unknown, never a recorded expense")
    func succeededWithoutEvidence() throws {
        let parsed = try decode(chatReceipt("succeeded", tool: "finance.log_expense"))
        #expect(parsed.outcome == .indeterminate(state: "succeeded"))
        #expect(!parsed.outcome.provesWrite)
    }

    @Test("a read-only query success is a structured query card, and still not a write")
    func answerIsNotAWrite() throws {
        let parsed = try decode(
            chatReceipt(
                "succeeded",
                tool: "finance.query_expenses",
                extra: [
                    "answer": "共 3 条记录，个人支出合计 ¥1200.00",
                    "query_result": [
                        "status": "ok",
                        "view": "total",
                        "metric": "personal_spend_total_cny",
                        "record_count": 3,
                        "filters_applied": ["categories": ["网球"]],
                        "source_system": "feishu_bitable",
                        "evidence": ["kind": "aggregate_query"],
                        "personal_spend_total_cny": "1200.00",
                    ],
                ]
            )
        )
        guard case .answeredWithQuery(let result, let tool) = parsed.outcome else {
            Issue.record("expected a query card, got \(parsed.outcome)")
            return
        }
        #expect(result.view == .total)
        #expect(result.amount == "1200.00")
        #expect(result.recordCount == 3)
        #expect(tool == "finance.query_expenses")
        #expect(!parsed.outcome.provesWrite)
    }

    @Test("a query tool that succeeded without a projectable result is unknown")
    func queryWithoutResultIsUnknown() throws {
        // Defense-in-depth: the server always projects `query_result` for a
        // succeeded query. If it is missing or malformed, the receipt is not a
        // clean answer and not a query card -- it is unknown.
        let parsed = try decode(
            chatReceipt(
                "succeeded",
                tool: "finance.query_expenses",
                extra: ["answer": "共 3 条记录"]
            )
        )
        #expect(parsed.outcome == .indeterminate(state: "succeeded"))
    }

    @Test("a by_category query result projects its buckets")
    func byCategoryProjectsBuckets() throws {
        let parsed = try decode(
            chatReceipt(
                "succeeded",
                tool: "finance.query_expenses",
                extra: [
                    "query_result": [
                        "view": "by_category",
                        "record_count": 3,
                        "personal_spend_total_cny": "1500.00",
                        "by_category": [
                            [
                                "category": "旅行",
                                "personal_spend_total_cny": "1000.00",
                                "record_count": 2,
                                "share_of_total": "66.67",
                            ],
                            [
                                "category": nil,
                                "personal_spend_total_cny": "500.00",
                                "record_count": 1,
                                "share_of_total": "33.33",
                            ],
                        ],
                    ],
                ]
            )
        )
        guard case .answeredWithQuery(let result, _) = parsed.outcome else {
            Issue.record("expected a query card, got \(parsed.outcome)")
            return
        }
        #expect(result.view == .byCategory)
        #expect(result.byCategory.count == 2)
        #expect(result.byCategory[0].category == "旅行")
        #expect(result.byCategory[0].amount == "1000.00")
        #expect(result.byCategory[1].category == nil)
        #expect(result.byCategory[1].share == "33.33")
    }

    @Test("a records query result preserves the next-page cursor")
    func recordsPreservesCursor() throws {
        let parsed = try decode(
            chatReceipt(
                "succeeded",
                tool: "finance.query_expenses",
                extra: [
                    "query_result": [
                        "view": "records",
                        "record_count": 5,
                        "records": [
                            [
                                "record_id": "rec1",
                                "name": "网球场地费",
                                "occurred_on": "2026-05-01",
                                "category": "运动",
                                "is_family_expense": false,
                                "personal_spend_cny": "200.00",
                            ]
                        ],
                        "next_cursor": "cursor-v1",
                    ],
                ]
            )
        )
        guard case .answeredWithQuery(let result, _) = parsed.outcome else {
            Issue.record("expected a query card, got \(parsed.outcome)")
            return
        }
        #expect(result.view == .records)
        #expect(result.records.count == 1)
        #expect(result.records[0].name == "网球场地费")
        #expect(result.records[0].isFamilyExpense == false)
        #expect(result.nextCursor == "cursor-v1")
    }

    @Test("an unknown query view is not a card and not a clean answer")
    func unknownQueryViewIsUnknown() throws {
        // The strict decoder refuses a view this build cannot render. The
        // receipt survives (the query is a read, after all) but projects as
        // indeterminate, never as prose and never as a success.
        let parsed = try decode(
            chatReceipt(
                "succeeded",
                tool: "finance.query_expenses",
                extra: [
                    "query_result": [
                        "view": "pie_chart",
                        "record_count": 3,
                    ],
                ]
            )
        )
        #expect(parsed.outcome == .indeterminate(state: "succeeded"))
    }

    @Test("an unknown state stops the poll loop and claims nothing")
    func unknownState() throws {
        let parsed = try decode(chatReceipt("teleported", recordID: "rec-1"))
        #expect(parsed.state == .unrecognised("teleported"))
        #expect(parsed.outcome == .indeterminate(state: "teleported"))
        #expect(parsed.outcome.isSettled)
        #expect(!parsed.outcome.releasesPendingSlot)
        #expect(!parsed.outcome.provesWrite)
    }

    @Test("a cancel request past submit is not a rollback")
    func cancelIsNotRollback() throws {
        let parsed = try decode(
            chatReceipt("source_in_progress", cancelRequested: true, clientDetached: true)
        )
        #expect(parsed.outcome == .running)
        #expect(parsed.cancellation == .requestedOutcomeStillAuthoritative)
    }

    @Test("only cancelled_pre_submit may say nothing was written")
    func cancelledPreSubmit() throws {
        let parsed = try decode(chatReceipt("cancelled_pre_submit", cancelRequested: true))
        #expect(parsed.outcome == .cancelledBeforeSubmit)
        #expect(parsed.cancellation == .cancelledBeforeSubmit)
    }

    @Test("a duplicate park without its check id is not a decision prompt")
    func duplicateWithoutCheckID() throws {
        let parsed = try decode(
            chatReceipt(
                "waiting_for_duplicate_decision",
                extra: ["duplicate_existing": "午饭 ¥20 餐饮"]
            )
        )
        #expect(parsed.outcome == .indeterminate(state: "waiting_for_duplicate_decision"))
    }

    @Test("a duplicate park exposes the check id the user has to decide on")
    func duplicatePark() throws {
        let parsed = try decode(
            chatReceipt(
                "waiting_for_duplicate_decision",
                duplicateCheckID: "dup-9",
                extra: ["duplicate_existing": "午饭 ¥20 餐饮"]
            )
        )
        #expect(
            parsed.outcome
                == .needsDuplicateDecision(checkID: "dup-9", existing: "午饭 ¥20 餐饮")
        )
        #expect(parsed.outcome.isSettled)
    }

    @Test("needs_manual_review keeps the record id and is not a success")
    func manualReview() throws {
        let parsed = try decode(
            chatReceipt(
                "needs_manual_review",
                recordID: "rec-7",
                failureReason: "SOURCE_COMMIT_UNKNOWN"
            )
        )
        #expect(
            parsed.outcome
                == .needsManualReview(
                    reason: "SOURCE_COMMIT_UNKNOWN", recordID: "rec-7", domain: nil
                )
        )
        #expect(!parsed.outcome.releasesPendingSlot)
        #expect(!parsed.outcome.provesWrite)
    }

    @Test("a body missing cancel_requested is refused, not defaulted")
    func missingFlagIsRefused() {
        var body = chatReceipt("succeeded")
        body["cancel_requested"] = nil
        #expect(throws: (any Error).self) {
            try JSONDecoder().decode(OperationReceipt.self, from: chatJSON(body))
        }
    }
}

// --- the Timeline projection -------------------------------------------------

@Suite("The DEV-030 Timeline projection")
struct TimelineEventTests {
    private func decode(_ object: [String: Any]) throws -> TimelineEvent {
        try JSONDecoder().decode(TimelineEvent.self, from: chatJSON(object))
    }

    @Test("reassuring prose next to failed_safe still renders as a failure")
    func proseNeverOverridesState() throws {
        // The exact shape §5.1 warns about: the model's own words say the expense
        // was recorded, and the structured state says nothing was written.
        let parsed = try decode(
            chatEvent(
                "ev-1",
                type: "operation_result",
                content: [
                    "state": "failed_safe",
                    "answer": "已记录：咖啡 18 元，记录成功",
                    "failure_reason": "TOOL_NOT_ALLOWLISTED",
                ]
            )
        )
        #expect(
            parsed.kind
                == .operationResult(
                    outcome: .failedSafe(reason: "TOOL_NOT_ALLOWLISTED"),
                    state: .failedSafe,
                    toolEvidence: .unknown
                )
        )
    }

    @Test("a persisted success with a record id is evidence")
    func persistedRecordEvidence() throws {
        let parsed = try decode(
            chatEvent(
                "ev-2",
                type: "operation_result",
                content: ["state": "succeeded", "record_id": "rec-42"]
            )
        )
        #expect(
            parsed.kind
                == .operationResult(
                    outcome: .recorded(recordID: "rec-42", tool: nil, record: nil),
                    state: .succeeded,
                    toolEvidence: .unknown
                )
        )
    }

    @Test("a persisted success with no record id claims nothing")
    func persistedSuccessWithoutEvidence() throws {
        let parsed = try decode(
            chatEvent("ev-3", type: "operation_result", content: ["state": "succeeded"])
        )
        #expect(
            parsed.kind
                == .operationResult(
                    outcome: .indeterminate(state: "succeeded"),
                    state: .succeeded,
                    toolEvidence: .unknown
                )
        )
    }

    @Test("an explicit no-tool answer stays an answer, never 'unknown tool'")
    func explicitNoToolAnswer() throws {
        // New events carry `tool`, including an explicit null for a direct
        // answer. That is the only shape 无工具调用 may be claimed from.
        let parsed = try decode(
            chatEvent(
                "ev-tool-null",
                type: "operation_result",
                content: [
                    "state": "succeeded",
                    "tool": nil,
                    "answer": "好的",
                ]
            )
        )
        #expect(
            parsed.kind
                == .operationResult(
                    outcome: .answered("好的"),
                    state: .succeeded,
                    toolEvidence: .known(nil)
                )
        )
    }

    @Test("an old event without a tool fact stays unknown, not 'no tool'")
    func missingToolFactIsUnknown() throws {
        // History recorded before `tool` was persisted carries a plain answer
        // that could be raw query JSON. Without tool evidence it must not be
        // read as a clean answer -- and never as 无工具调用.
        let parsed = try decode(
            chatEvent(
                "ev-old",
                type: "operation_result",
                content: [
                    "state": "succeeded",
                    "answer": "本月个人支出 2093.00",
                ]
            )
        )
        #expect(
            parsed.kind
                == .operationResult(
                    outcome: .indeterminate(state: "succeeded"),
                    state: .succeeded,
                    toolEvidence: .unknown
                )
        )
    }

    @Test("an old event with a raw JSON answer stays unknown")
    func oldRawJsonAnswerIsUnknown() throws {
        // The pre-fix shape: a query result dumped as a JSON string into
        // `answer`. Decoded as a `JSONValue.string`, not a nested object, and
        // without tool evidence it projects as unknown, never as a clean
        // answer.
        let parsed = try decode(
            chatEvent(
                "ev-raw-json",
                type: "operation_result",
                content: [
                    "state": "succeeded",
                    "answer": "{\"status\":\"ok\",\"view\":\"total\"}",
                ]
            )
        )
        #expect(
            parsed.kind
                == .operationResult(
                    outcome: .indeterminate(state: "succeeded"),
                    state: .succeeded,
                    toolEvidence: .unknown
                )
        )
    }

    @Test("a persisted query event projects the same card as the live receipt")
    func persistedQueryEvent() throws {
        let parsed = try decode(
            chatEvent(
                "ev-query",
                type: "operation_result",
                content: [
                    "state": "succeeded",
                    "tool": "finance.query_expenses",
                    "query_result": [
                        "view": "total",
                        "record_count": 3,
                        "personal_spend_total_cny": "1200.00",
                    ],
                ]
            )
        )
        guard case .operationResult(.answeredWithQuery(let result, let tool), .succeeded, .known(let recordedTool)) = parsed.kind else {
            Issue.record("expected a query card event, got \(parsed.kind)")
            return
        }
        #expect(result.view == .total)
        #expect(result.amount == "1200.00")
        #expect(tool == "finance.query_expenses")
        #expect(recordedTool == "finance.query_expenses")
    }

    @Test("a Session divider is presentation, not dialogue")
    func divider() throws {
        let parsed = try decode(
            chatEvent("ev-4", type: "session_divider", content: ["reason": "idle_and_unrelated"])
        )
        #expect(parsed.kind == .sessionDivider(reason: "idle_and_unrelated", corrected: false))
        let corrected = try decode(
            chatEvent(
                "ev-5",
                type: "session_boundary_corrected",
                content: ["reason": "user_correction"]
            )
        )
        #expect(parsed.eventID == "ev-4")
        #expect(corrected.kind == .sessionDivider(reason: "user_correction", corrected: true))
    }

    @Test("a duplicate decision marker permanently closes its check")
    func duplicateDecisionMarker() throws {
        let parsed = try decode(
            chatEvent(
                "ev-decision",
                type: "duplicate_decision",
                content: [
                    "duplicate_check_id": "chk-1",
                    "decision": "write_anyway",
                ]
            )
        )
        #expect(
            parsed.kind
                == .duplicateDecision(
                    checkID: "chk-1",
                    decision: "write_anyway"
                )
        )
    }

    @Test("a malformed duplicate decision marker stays visible as unknown")
    func malformedDuplicateDecisionMarker() throws {
        let parsed = try decode(
            chatEvent(
                "ev-decision-bad",
                type: "duplicate_decision",
                content: ["duplicate_check_id": "chk-1"]
            )
        )
        #expect(parsed.kind == .unrecognised(eventType: "duplicate_decision"))
    }

    @Test("a category correction marker carries the verified current row")
    func categoryCorrectionMarker() throws {
        let parsed = try decode(
            chatEvent(
                "ev-category",
                type: "expense_category_corrected",
                content: [
                    "record_id": "rec-42",
                    "record": [
                        "name": "午饭",
                        "amount_cny": "38.50",
                        "occurred_on": "2026-08-15",
                        "is_family_expense": false,
                        "category": "购物",
                        "category_updated_at": "2026-08-15T02:31:00Z",
                    ],
                ]
            )
        )
        guard case .expenseCategoryCorrected(let recordID, let record) = parsed.kind else {
            Issue.record("expected a category correction, got \(parsed.kind)")
            return
        }
        #expect(recordID == "rec-42")
        #expect(record.category == "购物")
        #expect(record.categoryUpdatedAt == "2026-08-15T02:31:00Z")
    }

    @Test("a category marker without a verified edit timestamp is unreadable")
    func categoryCorrectionNeedsTimestamp() throws {
        let parsed = try decode(
            chatEvent(
                "ev-category-bad",
                type: "expense_category_corrected",
                content: [
                    "record_id": "rec-42",
                    "record": [
                        "name": "午饭",
                        "amount_cny": "38.50",
                        "occurred_on": "2026-08-15",
                        "is_family_expense": false,
                        "category": "购物",
                    ],
                ]
            )
        )
        #expect(
            parsed.kind
                == .unrecognised(eventType: "expense_category_corrected")
        )
    }

    @Test("an unknown chatEvent type stays visible instead of vanishing")
    func unknownEventType() throws {
        let parsed = try decode(chatEvent("ev-6", type: "teleport", content: [:]))
        #expect(parsed.kind == .unrecognised(eventType: "teleport"))
    }

    @Test("a daily review event carries its frozen snapshot")
    func dailyReviewEvent() throws {
        let parsed = try decode(
            chatEvent(
                "ev-review",
                type: "daily_review",
                content: [
                    "review_id": "rev-1",
                    "review_date": "2026-07-25",
                    "item_count": 1,
                    "items": [
                        [
                            "record_id": "recA",
                            "tool": "finance.log_expense",
                            "committed_at": "2026-07-25T06:00:00+00:00",
                            "table_kind": "expense",
                            "values": ["name": "咖啡", "amount": "18.00"],
                        ]
                    ],
                ]
            )
        )
        guard case .dailyReview(let snapshot) = parsed.kind else {
            Issue.record("expected a daily review card, got \(parsed.kind)")
            return
        }
        #expect(snapshot.reviewID == "rev-1")
        #expect(snapshot.reviewDate == "2026-07-25")
        #expect(snapshot.itemCount == 1)
        #expect(snapshot.items.count == 1)
        #expect(snapshot.items[0].recordID == "recA")
        #expect(snapshot.items[0].values?["amount"] == .string("18.00"))
    }

    @Test("a daily review event without a review id is unreadable, not blank")
    func dailyReviewNeedsIdentity() throws {
        let parsed = try decode(
            chatEvent(
                "ev-review-bad",
                type: "daily_review",
                content: ["review_date": "2026-07-25", "item_count": 0, "items": []]
            )
        )
        #expect(parsed.kind == .unrecognised(eventType: "daily_review"))
    }

    @Test("a risk report event carries its frozen snapshot")
    func riskReportEvent() throws {
        let parsed = try decode(
            chatEvent(
                "ev-risk",
                type: "risk_report",
                content: [
                    "as_of": "2026-08-22",
                    "state": "NORMAL",
                    "mbs": 35.0,
                    "css": 40.0,
                    "afrs": 60.0,
                    "action": "持仓观察",
                ]
            )
        )
        guard case .riskReport(let snapshot) = parsed.kind else {
            Issue.record("expected a risk report card, got \(parsed.kind)")
            return
        }
        #expect(snapshot.asOf == "2026-08-22")
        #expect(snapshot.state == "NORMAL")
        #expect(snapshot.mbs == 35.0)
        #expect(snapshot.css == 40.0)
        #expect(snapshot.afrs == 60.0)
        #expect(snapshot.action == "持仓观察")
    }

    @Test("a risk report event tolerates absent scores and action")
    func riskReportAbsentScores() throws {
        let parsed = try decode(
            chatEvent(
                "ev-risk-null",
                type: "risk_report",
                content: ["as_of": "2026-08-22", "state": "DELEVERAGING"]
            )
        )
        guard case .riskReport(let snapshot) = parsed.kind else {
            Issue.record("expected a risk report card, got \(parsed.kind)")
            return
        }
        #expect(snapshot.mbs == nil)
        #expect(snapshot.css == nil)
        #expect(snapshot.afrs == nil)
        #expect(snapshot.action == nil)
    }

    @Test("a risk report event without as_of is unreadable, not blank")
    func riskReportNeedsAsOf() throws {
        let parsed = try decode(
            chatEvent(
                "ev-risk-bad",
                type: "risk_report",
                content: ["state": "NORMAL", "mbs": 35.0]
            )
        )
        #expect(parsed.kind == .unrecognised(eventType: "risk_report"))
    }

    @Test("a risk report event carries its indicator breakdown")
    func riskReportComponents() throws {
        let parsed = try decode(
            chatEvent(
                "ev-risk-comp",
                type: "risk_report",
                content: [
                    "as_of": "2026-08-22",
                    "state": "NORMAL",
                    "rates_credit": 65.0,
                    "components": [
                        "mbs": [
                            ["label": "VIX", "value": "16.0", "band": "green"],
                            ["label": "广度（>200日均线）", "value": "68.4%", "band": "green"],
                        ],
                        "css": [
                            ["label": "AI 篮子", "value": "警戒", "band": "orange"],
                        ],
                        "rates_credit": [
                            ["label": "10Y 美债收益率", "value": "4.80%", "band": "orange"],
                        ],
                    ],
                ]
            )
        )
        guard case .riskReport(let snapshot) = parsed.kind else {
            Issue.record("expected a risk report card, got \(parsed.kind)")
            return
        }
        let components = try #require(snapshot.components)
        #expect(components.mbs.count == 2)
        #expect(components.mbs[0].label == "VIX")
        #expect(components.mbs[0].value == "16.0")
        #expect(components.mbs[0].band == "green")
        #expect(components.mbs[1].value == "68.4%")
        #expect(components.css.count == 1)
        #expect(components.css[0].label == "AI 篮子")
        #expect(components.css[0].value == "警戒")
        #expect(components.css[0].band == "orange")
        #expect(snapshot.ratesCredit == 65.0)
        #expect(components.ratesCredit?.count == 1)
        #expect(components.ratesCredit?[0].label == "10Y 美债收益率")
    }

    @Test("a malformed components degrades to a score-only card, not unrecognised")
    func riskReportMalformedComponents() throws {
        let parsed = try decode(
            chatEvent(
                "ev-risk-bad-comp",
                type: "risk_report",
                content: [
                    "as_of": "2026-08-22",
                    "state": "NORMAL",
                    "mbs": 35.0,
                    "components": [
                        "mbs": [["label": "VIX", "value": "16.0", "band": 123]],
                    ],
                ]
            )
        )
        guard case .riskReport(let snapshot) = parsed.kind else {
            Issue.record("expected a risk report card, got \(parsed.kind)")
            return
        }
        #expect(snapshot.asOf == "2026-08-22")
        #expect(snapshot.mbs == 35.0)
        #expect(snapshot.components == nil)  // malformed -> score-only, not a crash
    }

    @Test("a risk report event carries its data-quality flag")
    func riskReportQualityStatus() throws {
        let parsed = try decode(
            chatEvent(
                "ev-risk-quality",
                type: "risk_report",
                content: [
                    "as_of": "2026-08-22",
                    "state": "NORMAL",
                    "quality_status": "data_quality_warning",
                ]
            )
        )
        guard case .riskReport(let snapshot) = parsed.kind else {
            Issue.record("expected a risk report card, got \(parsed.kind)")
            return
        }
        #expect(snapshot.qualityStatus == "data_quality_warning")
    }

    @Test("a risk report event carries freshness and anomaly flags")
    func riskReportFreshnessAndAnomaly() throws {
        let parsed = try decode(
            chatEvent(
                "ev-risk-stale",
                type: "risk_report",
                content: [
                    "as_of": "2026-08-22",
                    "state": "NORMAL",
                    "stale_days": 10,
                    "anomalous": true,
                ]
            )
        )
        guard case .riskReport(let snapshot) = parsed.kind else {
            Issue.record("expected a risk report card, got \(parsed.kind)")
            return
        }
        #expect(snapshot.staleDays == 10)
        #expect(snapshot.anomalous == true)
    }

    @Test("a non-string where text belongs is unreadable, not blank")
    func nonStringText() throws {
        let parsed = try decode(chatEvent("ev-7", content: ["text": 18]))
        #expect(parsed.kind == .unrecognised(eventType: "user_message"))
        // The chatPage still decodes: one odd value must not blank the whole history.
        #expect(parsed.content["text"] == .number(18))
    }
}

// --- sending, resuming and cancelling ---------------------------------------

@Suite("The DEV-030 chat send path", .serialized)
struct ChatSendTests {
    private func newService() -> Service { Service() }

    @Test("an accepted message is polled to its structured chatReceipt")
    func pollToReceipt() async throws {
        let service = newService()
        service.answer { call, seen in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                return .accepted(chatReceipt("accepted"))
            case ("GET", "/v1/operations/op-1"):
                return seen == 0
                    ? .accepted(chatReceipt("source_in_progress"))
                    : .ok(
                        chatReceipt(
                            "succeeded", tool: "finance.log_expense", recordID: "rec-42"
                        )
                    )
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let (chat, _, store) = try await makeChat(service: service)

        let final = try await chat.send(text: "咖啡 18 个人支出")

        #expect(final.outcome == .recorded(recordID: "rec-42", tool: "finance.log_expense", record: nil))
        #expect(service.chatPosts.count == 1)
        #expect(service.chatPosts.first?.idempotencyKey?.isEmpty == false)
        #expect(service.chatPosts.first?.string("conversation_id") == chatTimelineID)
        // A settled operation releases the pending slot.
        #expect(try store.read(CredentialKey.pendingChatSend) == nil)
    }

    @Test("a confirmed new-topic send carries a boolean reset instruction")
    func newTopicSendCarriesResetInstruction() async throws {
        let service = newService()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                return .ok(chatReceipt("succeeded"))
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let (chat, _, _) = try await makeChat(service: service)

        _ = try await chat.send(text: "帮我规划周末", startNewSession: true)

        #expect(service.chatPosts.first?.bool("start_new_session") == true)
    }

    @Test("a dropped reply resumes the same operation with the same key")
    func droppedReplyResumesSameOperation() async throws {
        let service = newService()
        service.answer { call, seen in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                // The first POST reaches the server, which anchors the operation,
                // and the reply is lost on the way back.
                if seen == 0 { return .init(status: 599, body: Data()) }
                return .accepted(chatReceipt("accepted"))
            case ("GET", "/v1/operations/op-1"):
                return .ok(chatReceipt("succeeded", tool: "finance.log_expense", recordID: "rec-42"))
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let (chat, _, store) = try await makeChat(service: service)

        await #expect(throws: (any Error).self) {
            _ = try await chat.send(text: "咖啡 18 个人支出")
        }
        // The unresolved message is still on disk, and a new one is refused rather
        // than quietly starting a second write. `#require` rather than a force
        // unwrap: if this property ever breaks, the run has to report *which*
        // expectation failed instead of aborting the whole process.
        let pending = try #require(try await chat.pendingSend())
        #expect(pending.operationID == nil)
        await #expect(throws: ChatTimeline.ChatError.unresolvedSend(pending)) {
            _ = try await chat.send(text: "另一笔")
        }

        let resumed = try await chat.resume()
        #expect(resumed?.outcome == .recorded(recordID: "rec-42", tool: "finance.log_expense", record: nil))

        let keys = Set(service.chatPosts.compactMap(\.idempotencyKey))
        #expect(service.chatPosts.count == 2)
        #expect(keys.count == 1, "a retry must re-present the same idempotency key")
        #expect(try store.read(CredentialKey.pendingChatSend) == nil)
    }

    @Test("an app restart polls the known operation and never re-posts")
    func restartPollsInsteadOfPosting() async throws {
        let service = newService()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("GET", "/v1/operations/op-1"):
                return .ok(chatReceipt("succeeded", tool: "finance.log_expense", recordID: "rec-42"))
            default:
                return .error(500, "INTERNAL_ERROR")
            }
        }
        let store = InMemoryCredentialStore()
        // The state a previous launch left behind: the operation id is known, so the
        // answer is a poll. Posting again would be a second chance at a second row.
        let pending = ChatTimeline.PendingSend(
            idempotencyKey: "3f2504e0-4f89-41d3-9a0c-0305e82c3302",
            conversationID: chatTimelineID,
            text: "咖啡 18 个人支出",
            clarificationOf: nil,
            operationID: "op-1"
        )
        try store.write(
            CredentialKey.pendingChatSend, value: try JSONEncoder().encode(pending)
        )
        let (chat, _, _) = try await makeChat(service: service, store: store)

        let resumed = try await chat.resume()

        #expect(resumed?.outcome.provesWrite == true)
        #expect(service.chatPosts.isEmpty)
        #expect(service.count("GET", "/v1/operations/op-1") == 1)
    }

    @Test("nothing pending means nothing to resume")
    func resumeWithoutPending() async throws {
        let service = newService()
        let (chat, _, _) = try await makeChat(service: service)
        #expect(try await chat.resume() == nil)
        #expect(service.chatPosts.isEmpty)
    }

    @Test("an unusable local pending record is reported, not discarded")
    func malformedPendingRecord() async throws {
        let service = newService()
        let store = InMemoryCredentialStore()
        try store.write(CredentialKey.pendingChatSend, value: Data("{".utf8))
        let (chat, _, _) = try await makeChat(service: service, store: store)
        await #expect(throws: ChatTimeline.ChatError.pendingSendMalformed) {
            _ = try await chat.resume()
        }
    }

    @Test("an idempotency conflict clears the slot instead of blocking forever")
    func idempotencyConflict() async throws {
        let service = newService()
        service.answer { call, _ in
            call.path == "/v1/chat/messages"
                ? .error(409, "IDEMPOTENCY_CONFLICT")
                : .error(404, "NOT_FOUND")
        }
        let (chat, _, store) = try await makeChat(service: service)

        await #expect(throws: AgentClientError.idempotencyConflict) {
            _ = try await chat.send(text: "咖啡 18 个人支出")
        }
        // Authentication and body validation both precede the atomic operation
        // creation, so a 409 proves nothing was anchored.
        #expect(try store.read(CredentialKey.pendingChatSend) == nil)
        #expect(service.chatPosts.count == 1)
    }

    @Test("a server error keeps the slot, because it proves nothing")
    func serverErrorKeepsPending() async throws {
        let service = newService()
        service.answer { call, _ in
            call.path == "/v1/chat/messages"
                ? .error(500, "INTERNAL_ERROR")
                : .error(404, "NOT_FOUND")
        }
        let (chat, _, store) = try await makeChat(service: service)

        await #expect(throws: AgentClientError.serverError(status: 500)) {
            _ = try await chat.send(text: "咖啡 18 个人支出")
        }
        #expect(try store.read(CredentialKey.pendingChatSend) != nil)
        #expect(service.chatPosts.count == 1)
    }

    @Test("the poll budget is bounded and the operation stays resumable")
    func boundedPolling() async throws {
        let service = newService()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                return .accepted(chatReceipt("accepted"))
            case ("GET", "/v1/operations/op-1"):
                return .accepted(chatReceipt("verifying"))
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let (chat, _, store) = try await makeChat(
            service: service,
            pollDelays: Array(repeating: .zero, count: 3)
        )

        let unfinished = try await chat.send(text: "咖啡 18 个人支出")

        #expect(unfinished.outcome == .running)
        #expect(service.count("GET", "/v1/operations/op-1") == 3)
        // Still resumable: the client giving up watching is not the write giving up.
        #expect(try store.read(CredentialKey.pendingChatSend) != nil)
    }

    @Test("a state this build does not know is not polled and claims nothing")
    func unknownStateStopsPolling() async throws {
        let service = newService()
        service.answer { call, _ in
            call.path == "/v1/chat/messages"
                ? .accepted(chatReceipt("teleported", recordID: "rec-42"))
                : .error(404, "NOT_FOUND")
        }
        let (chat, _, store) = try await makeChat(service: service)

        let unknown = try await chat.send(text: "咖啡 18 个人支出")

        #expect(unknown.outcome == .indeterminate(state: "teleported"))
        #expect(!unknown.outcome.provesWrite)
        // Neither polled in a loop nor believed. The operation id stays durable:
        // an unknown state may still represent an in-flight write, so releasing
        // the slot would let a retry mint a second idempotency key.
        #expect(service.count("GET", "/v1/operations/op-1") == 0)
        #expect(try store.read(CredentialKey.pendingChatSend) != nil)
    }

    @Test("manual review keeps the idempotency slot until the user resolves it")
    func manualReviewKeepsPending() async throws {
        let service = newService()
        service.answer { call, _ in
            call.path == "/v1/chat/messages"
                ? .ok(
                    chatReceipt(
                        "needs_manual_review",
                        tool: "finance.log_expense",
                        domain: "finance",
                        recordID: "rec-42",
                        failureReason: "RECEIPT_MISMATCH"
                    )
                )
                : .error(404, "NOT_FOUND")
        }
        let (chat, _, store) = try await makeChat(service: service)

        let review = try await chat.send(text: "咖啡 18 个人支出")

        #expect(
            review.outcome
                == .needsManualReview(
                    reason: "RECEIPT_MISMATCH", recordID: "rec-42", domain: "finance"
                )
        )
        #expect(try store.read(CredentialKey.pendingChatSend) != nil)
        let pending = try #require(try await chat.pendingSend())
        await #expect(throws: ChatTimeline.ChatError.unresolvedSend(pending)) {
            _ = try await chat.send(text: "再记一次咖啡 18")
        }
    }

    @Test("a clarification is answered by a new operation with a new key")
    func clarificationIsANewOperation() async throws {
        let service = newService()
        service.answer { call, seen in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                if seen == 0 {
                    return .accepted(
                        chatReceipt(
                            "waiting_for_clarification",
                            extra: ["clarification": "这笔是个人还是家庭支出？"]
                        )
                    )
                }
                return .ok(
                    chatReceipt(
                        "succeeded",
                        operation: "op-2",
                        tool: "finance.log_expense",
                        recordID: "rec-43"
                    )
                )
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let (chat, _, store) = try await makeChat(service: service)

        let parked = try await chat.send(text: "咖啡 18")
        #expect(parked.outcome == .needsClarification(question: "这笔是个人还是家庭支出？"))
        // A parked operation is settled: polling it could never end, because its
        // only exit is a new message.
        #expect(service.count("GET", "/v1/operations/op-1") == 0)
        #expect(try store.read(CredentialKey.pendingChatSend) == nil)

        let answered = try await chat.send(text: "个人", clarificationOf: "op-1")
        #expect(answered.outcome == .recorded(recordID: "rec-43", tool: "finance.log_expense", record: nil))
        #expect(service.chatPosts.count == 2)
        #expect(service.chatPosts[1].string("clarification_of") == "op-1")
        #expect(
            Set(service.chatPosts.compactMap(\.idempotencyKey)).count == 2,
            "a clarification answer is a new request and needs its own key"
        )
    }

    @Test("cancelling before submit clears the slot; cancelling later does not")
    func cancelStates() async throws {
        let service = newService()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                return .accepted(chatReceipt("accepted"))
            case ("GET", "/v1/operations/op-1"):
                return .accepted(chatReceipt("source_in_progress"))
            case ("DELETE", "/v1/operations/op-1"):
                return .accepted(chatReceipt("source_in_progress", cancelRequested: true))
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let (chat, _, store) = try await makeChat(
            service: service,
            pollDelays: Array(repeating: .zero, count: 1)
        )
        _ = try await chat.send(text: "咖啡 18 个人支出")

        let cancelled = try await chat.cancel(operationID: "op-1")
        #expect(cancelled.outcome == .running)
        #expect(cancelled.cancellation == .requestedOutcomeStillAuthoritative)
        // The write may already be in flight, so the operation is still the one to
        // watch and its record must stay.
        #expect(try store.read(CredentialKey.pendingChatSend) != nil)

        service.answer { call, _ in
            call.method == "DELETE"
                ? .ok(chatReceipt("cancelled_pre_submit", cancelRequested: true))
                : .error(404, "NOT_FOUND")
        }
        let settled = try await chat.cancel(operationID: "op-1")
        #expect(settled.outcome == .cancelledBeforeSubmit)
        #expect(try store.read(CredentialKey.pendingChatSend) == nil)
    }

    @Test("the client cannot chat before the server names the Timeline")
    func noClientSideSession() async throws {
        let service = newService()
        let (chat, _, _) = try await makeChat(service: service, bind: false)
        await #expect(throws: ChatTimeline.ChatError.timelineUnknown) {
            _ = try await chat.send(text: "咖啡 18 个人支出")
        }
        #expect(service.chatPosts.isEmpty, "no request may be made without a server id")
        #expect(await chat.boundConversationID == nil)
    }

    @Test("capabilities must name the canonical Timeline")
    func capabilitiesCarryTheTimeline() async throws {
        let service = newService()
        service.answer { call, _ in
            call.path == "/v1/capabilities"
                ? .ok([
                    "allowed_tools_version": "v1",
                    "tools": [["alias": "finance.log_expense"]],
                    "conversation_id": chatTimelineID,
                ])
                : .error(404, "NOT_FOUND")
        }
        let (_, session, _) = try await makeChat(service: service)
        #expect(try await session.capabilities().conversationID == chatTimelineID)

        // A body without it is refused: the client has no other way to learn the
        // Timeline and must never invent one.
        service.answer { call, _ in
            call.path == "/v1/capabilities"
                ? .ok(["allowed_tools_version": "v1", "tools": []])
                : .error(404, "NOT_FOUND")
        }
        await #expect(throws: AgentClientError.malformedResponse) {
            _ = try await session.capabilities()
        }
        _ = service
    }
}

// --- history ------------------------------------------------------------------

@Suite("The DEV-030 Timeline history", .serialized)
struct ChatHistoryTests
{
    private func newService() -> Service { Service() }

    @Test("the first chatPage is the newest, and scrolling up prepends in order")
    func pagingUp() async throws {
        let service = newService()
        service.answer { call, seen in
            guard call.path.hasSuffix("/events") else { return .error(404, "NOT_FOUND") }
            if call.query["cursor"] == nil {
                return .ok(
                    chatPage(
                        [chatEvent("ev-3"), chatEvent("ev-4")],
                        olderCursor: "cur-older-3",
                        newerCursor: "cur-newer-4",
                        hasOlder: true
                    )
                )
            }
            #expect(call.query["cursor"] == "cur-older-3")
            #expect(call.query["direction"] == "older")
            return .ok(
                chatPage(
                    [chatEvent("ev-1"), chatEvent("ev-2")],
                    // An old chatPage also carries a `newer_cursor`, anchored at *its*
                    // newest row -- ev-2, far behind the live edge.
                    newerCursor: "cur-newer-2",
                    hasOlder: false
                )
            )
        }
        let (chat, _, _) = try await makeChat(service: service)

        try await chat.loadLatest()
        #expect(await chat.events.map(\.eventID) == ["ev-3", "ev-4"])
        #expect(await chat.hasOlder)

        #expect(try await chat.loadOlder())
        #expect(await chat.events.map(\.eventID) == ["ev-1", "ev-2", "ev-3", "ev-4"])
        #expect(await chat.hasOlder == false)
        #expect(
            await chat.newestCursor == "cur-newer-4",
            "an older chatPage must not drag the sync cursor back into history"
        )
        #expect(service.timelineGets.count == 2)
    }

    @Test("incremental sync appends and survives a quiet chatPage")
    func incrementalSync() async throws {
        let service = newService()
        service.answer { call, seen in
            guard call.path.hasSuffix("/events") else { return .error(404, "NOT_FOUND") }
            if call.query["cursor"] == nil {
                return .ok(
                    chatPage([chatEvent("ev-1")], newerCursor: "cur-newer-1")
                )
            }
            #expect(call.query["direction"] == "newer")
            if seen == 1 {
                return .ok(
                    chatPage(
                        [chatEvent("ev-1"), chatEvent("ev-2")],
                        newerCursor: "cur-newer-2",
                        hasNewer: false
                    )
                )
            }
            // A quiet poll: no events, and therefore no cursor at all.
            return .ok(chatPage([]))
        }
        let (chat, _, _) = try await makeChat(service: service)

        try await chat.loadLatest()
        try await chat.syncNewer()
        // ev-1 arrived twice and appears once.
        #expect(await chat.events.map(\.eventID) == ["ev-1", "ev-2"])
        #expect(await chat.newestCursor == "cur-newer-2")

        try await chat.syncNewer()
        #expect(
            await chat.newestCursor == "cur-newer-2",
            "a quiet chatPage must not end incremental sync"
        )
        #expect(service.timelineGets.count == 3)
        #expect(service.timelineGets.last?.query["cursor"] == "cur-newer-2")
    }

    @Test("an empty Timeline can discover its first chatEvent without a cursor")
    func emptyTimelineCanRefresh() async throws {
        let service = newService()
        service.answer { call, seen in
            guard call.path.hasSuffix("/events") else {
                return .error(404, "NOT_FOUND")
            }
            return seen == 0
                ? .ok(chatPage([]))
                : .ok(chatPage([chatEvent("ev-1")], newerCursor: "cur-newer-1"))
        }
        let (chat, _, _) = try await makeChat(service: service)

        try await chat.loadLatest()
        try await chat.syncNewer()

        #expect(service.timelineGets.count == 2)
        #expect(await chat.events.map(\.eventID) == ["ev-1"])
        #expect(await chat.newestCursor == "cur-newer-1")
    }

    @Test("direction=newer without a cursor is refused before the request")
    func newerNeedsCursor() async throws {
        let service = newService()
        let (_, session, _) = try await makeChat(service: service)
        await #expect(throws: AgentClientError.cursorRequired) {
            _ = try await session.timelinePage(
                conversationID: chatTimelineID, cursor: nil, direction: .newer, limit: nil
            )
        }
        #expect(service.timelineGets.isEmpty)
    }

    @Test("a cursor the server refuses reloads once instead of looping")
    func invalidCursorReloads() async throws {
        let service = newService()
        service.answer { call, seen in
            guard call.path.hasSuffix("/events") else { return .error(404, "NOT_FOUND") }
            if call.query["cursor"] == nil {
                return .ok(
                    chatPage([chatEvent("ev-9")], olderCursor: "cur-stale", hasOlder: true)
                )
            }
            return .error(400, "INVALID_CURSOR")
        }
        let (chat, _, _) = try await makeChat(service: service)

        try await chat.loadLatest()
        #expect(try await chat.loadOlder() == false)
        // One refused chatPage, then exactly one reload -- not a retry loop.
        #expect(service.timelineGets.count == 3)
        #expect(await chat.events.map(\.eventID) == ["ev-9"])
    }

    @Test("a chatPage missing its events array is malformed, never an empty history")
    func malformedPage() async throws {
        let service = newService()
        service.answer { call, _ in
            call.path.hasSuffix("/events")
                ? .ok(["conversation_id": chatTimelineID, "has_older": false, "has_newer": false])
                : .error(404, "NOT_FOUND")
        }
        let (chat, _, _) = try await makeChat(service: service)
        await #expect(throws: AgentClientError.malformedResponse) {
            try await chat.loadLatest()
        }
        #expect(await chat.events.isEmpty)
        _ = service
    }

    @Test("the server's canonical Timeline id wins over the requested alias")
    func canonicalIDAdopted() async throws {
        let service = newService()
        service.answer { call, _ in
            call.path.hasSuffix("/events")
                ? .ok(chatPage([chatEvent("ev-1")], conversation: "tl_canonical"))
                : .error(404, "NOT_FOUND")
        }
        let (chat, _, _) = try await makeChat(service: service)
        await chat.bind(conversationID: "legacy-alias")
        try await chat.loadLatest()
        #expect(await chat.boundConversationID == "tl_canonical")
        #expect(service.timelineGets.first?.path.contains("legacy-alias") == true)
    }

    @Test("binding a different Timeline drops the loaded history")
    func rebindClearsHistory() async throws {
        let service = newService()
        service.answer { call, _ in
            call.path.hasSuffix("/events")
                ? .ok(chatPage([chatEvent("ev-1")], newerCursor: "cur-1"))
                : .error(404, "NOT_FOUND")
        }
        let (chat, _, _) = try await makeChat(service: service)
        try await chat.loadLatest()
        #expect(await chat.events.count == 1)

        await chat.bind(conversationID: "tl_other")
        #expect(await chat.events.isEmpty)
        #expect(await chat.newestCursor == nil)
        _ = service
    }
}

// --- the cross-language chatReceipt contract -------------------------------------

/// The Swift half of `DEV-030`'s chatReceipt contract.
///
/// It reads the **same file** the Python suite reads --
/// `src/personal_agent/api/vectors/chat_receipt_vectors.json`, whose receipts are
/// produced by the server's own `_operation_projection`. There is no copy in this
/// package: two copies of a contract drift, and the drift would show up as the app
/// telling Henson a expense was recorded when the server never said so.
///
/// If this suite is ever run outside the repo the vector-dependent tests fail
/// rather than pass silently.
private struct ReceiptVectors {
    struct Case {
        let name: String
        let chatReceipt: Data
        let expectedOutcome: String
        let expectedProvesWrite: Bool
        let expectedSettled: Bool
        let expectedReleasesPending: Bool
        let expectedCancellation: String
        /// The domain the *server* wrote into this receipt, read straight off the
        /// vector's own body. It is a fact the server derived from the tool's IR
        /// contract, and the client must decode that exact value: the 人工核对
        /// card's wording is chosen by it.
        let domain: String?
        /// What the phone reported, when the case declares it (v8). Present only
        /// on the calendar-write cases, and `nil` on the one that predates the
        /// server emitting the field -- which is itself the case that matters:
        /// history must render without it, as `unstated`.
        let deviceEvidence: String?
        /// The action id an override of this receipt must name, or `null` when
        /// 「仍要创建」 may not be answered at all (v9). The server states it once,
        /// from `may_override`; the client asserts its own decision against it.
        let expectedOverrideActionID: String?
    }

    let contract: String
    let operationStates: [String]
    let recordEvidenceTools: [String]
    let queryEvidenceTools: [String]
    let calendarQueryEvidenceTools: [String]
    let deviceExecutedTools: [String]
    /// The one device tool whose duplicates the user may override (v9). The
    /// server's `CALENDAR_DEVICE_TOOL` and the client's `calendarDeviceTool` are
    /// two names for it, and this holds them equal.
    let overrideTool: String
    let expenseCategories: [String]
    let cases: [Case]

    static func load() -> ReceiptVectors? {
        // …/ios/PersonalAgentKit/Tests/PersonalAgentKitTests/ChatTimelineTests.swift
        let repoRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()  // PersonalAgentKitTests
            .deletingLastPathComponent()  // Tests
            .deletingLastPathComponent()  // PersonalAgentKit
            .deletingLastPathComponent()  // ios
            .deletingLastPathComponent()  // repo root
        let path = repoRoot.appendingPathComponent(
            "src/personal_agent/api/vectors/chat_receipt_vectors.json"
        )
        guard
            let data = try? Data(contentsOf: path),
            let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let rawCases = root["cases"] as? [[String: Any]]
        else { return nil }
        let cases: [Case] = rawCases.compactMap { entry in
            guard
                let name = entry["name"] as? String,
                let chatReceipt = entry["receipt"],
                let body = try? JSONSerialization.data(withJSONObject: chatReceipt),
                let outcome = entry["expected_outcome"] as? String,
                let provesWrite = entry["expected_proves_write"] as? Bool,
                let settled = entry["expected_settled"] as? Bool,
                let releasesPending =
                    entry["expected_releases_pending"] as? Bool,
                let cancellation = entry["expected_cancellation"] as? String
            else { return nil }
            return Case(
                name: name,
                chatReceipt: body,
                expectedOutcome: outcome,
                expectedProvesWrite: provesWrite,
                expectedSettled: settled,
                expectedReleasesPending: releasesPending,
                expectedCancellation: cancellation,
                domain: (chatReceipt as? [String: Any])?["domain"] as? String,
                deviceEvidence: entry["expected_device_evidence"] as? String,
                expectedOverrideActionID: entry["expected_override_action_id"]
                    as? String
            )
        }
        return ReceiptVectors(
            contract: root["contract"] as? String ?? "",
            operationStates: root["operation_states"] as? [String] ?? [],
            recordEvidenceTools: root["record_evidence_tools"] as? [String] ?? [],
            queryEvidenceTools: root["query_evidence_tools"] as? [String] ?? [],
            calendarQueryEvidenceTools: root["calendar_query_evidence_tools"]
                as? [String] ?? [],
            deviceExecutedTools: root["device_executed_tools"] as? [String] ?? [],
            overrideTool: root["override_tool"] as? String ?? "",
            expenseCategories: root["expense_categories"] as? [String] ?? [],
            cases: cases
        )
    }
}

/// The label this client would put on an outcome, in the vector's vocabulary.
private func label(_ outcome: OperationOutcome) -> String {
    switch outcome {
    case .running: return "running"
    case .needsClarification: return "needs_clarification"
    case .needsDuplicateDecision: return "needs_duplicate_decision"
    case .recorded: return "recorded"
    case .calendarEventWritten: return "calendar_event_written"
    case .answered: return "answered"
    case .answeredV2: return "answered_v2"
    case .answeredWithQuery, .answeredWithCalendarQuery:
        // One wire name for "a succeeded governed read with a structured card":
        // which card is the client's own business, and the server states only
        // that the read produced a projectable result.
        return "answered_with_query"
    case .failedSafe: return "failed_safe"
    case .needsManualReview: return "needs_manual_review"
    case .cancelledBeforeSubmit: return "cancelled_before_submit"
    case .indeterminate: return "indeterminate"
    }
}

private func label(_ note: CancellationNote) -> String {
    switch note {
    case .none: return "none"
    case .requestedOutcomeStillAuthoritative:
        return "requested_outcome_still_authoritative"
    case .cancelledBeforeSubmit: return "cancelled_before_submit"
    }
}

@Suite("The frozen cross-language chat chatReceipt contract")
struct ReceiptContractTests {
    fileprivate let vectors = ReceiptVectors.load()

    @Test("the vector file is the one this build was written against")
    func contractVersion() throws {
        let vectors = try #require(vectors)
        #expect(vectors.contract == "chat_receipt_projection_v9")
        #expect(!vectors.cases.isEmpty)
    }

    @Test("this build knows every operation state the server can emit")
    func everyStateIsKnown() throws {
        let vectors = try #require(vectors)
        for wire in vectors.operationStates {
            let state = OperationState(wire: wire)
            #expect(
                state != .unrecognised(wire),
                "the server can emit \(wire) and this build would call it unknown"
            )
            // Round-tripping catches a typo that would otherwise make a known state
            // decode into a *different* known state.
            #expect(state.wire == wire)
        }
    }

    @Test("the record-evidence tool set matches the server's")
    func evidenceToolsMatch() throws {
        let vectors = try #require(vectors)
        #expect(Set(vectors.recordEvidenceTools) == OperationReceipt.recordEvidenceTools)
    }

    @Test("the 分类 picker offers exactly the ledger's own options")
    func expenseCategoriesMatch() throws {
        // The connector never creates a select option, so an option this client
        // invented would be a refused write rather than a new category. The
        // vector is what holds the picker and the ledger equal.
        let vectors = try #require(vectors)
        #expect(vectors.expenseCategories == ExpenseCategory.all)
    }

    @Test("every business field the server sends decodes onto the card")
    func recordFieldsDecode() throws {
        let vectors = try #require(vectors)
        let entry = try #require(
            vectors.cases.first { $0.name == "expense_recorded_with_fields" }
        )
        let receipt = try JSONDecoder().decode(
            OperationReceipt.self, from: entry.chatReceipt
        )
        let record = try #require(receipt.record)
        #expect(record.name == "午饭")
        #expect(record.amount == "38.50")
        #expect(record.occurredOn == "2026-08-15")
        #expect(record.isFamilyExpense == false)
        #expect(record.category == "餐饮")
        #expect(record.personalSpend == "38.50")
        // And it reaches the card through the outcome, not only the receipt.
        #expect(
            receipt.outcome
                == .recorded(
                    recordID: "recXXXXXXXXXXXX",
                    tool: "finance.log_expense",
                    record: record
                )
        )
    }

    @Test("a family expense keeps 原始金额 and 个人支出 apart")
    func familyRecordKeepsBothAmounts() throws {
        let vectors = try #require(vectors)
        let entry = try #require(
            vectors.cases.first { $0.name == "family_expense_recorded_with_fields" }
        )
        let record = try #require(
            try JSONDecoder()
                .decode(OperationReceipt.self, from: entry.chatReceipt).record
        )
        #expect(record.isFamilyExpense)
        #expect(record.amount == "2000.00")
        // The Base formula's answer, never re-derived on this side.
        #expect(record.personalSpend == "1000.00")
    }

    @Test("an edited category is marked and drops the stale formula value")
    func editedRecordIsMarked() throws {
        let vectors = try #require(vectors)
        let entry = try #require(
            vectors.cases.first { $0.name == "expense_category_edited" }
        )
        let record = try #require(
            try JSONDecoder()
                .decode(OperationReceipt.self, from: entry.chatReceipt).record
        )
        #expect(record.categoryUpdatedAt != nil)
        // 个人支出 may depend on 分类; a carried-over value would put a number on
        // the card the ledger may no longer agree with.
        #expect(record.personalSpend == nil)
    }

    @Test("a refund carries no category and keeps its negative amount")
    func refundRecordHasNoCategory() throws {
        let vectors = try #require(vectors)
        let entry = try #require(
            vectors.cases.first { $0.name == "refund_recorded_without_category" }
        )
        let record = try #require(
            try JSONDecoder()
                .decode(OperationReceipt.self, from: entry.chatReceipt).record
        )
        #expect(record.category == nil)
        #expect(record.amount == "-880.00")
    }

    @Test("the query-evidence tool set matches the server's")
    func queryEvidenceToolsMatch() throws {
        // The server's set is IR-derived; the client's is hard-coded. The vector
        // is what holds them equal, so a rename or a second governed query fails
        // here before it reaches a user.
        let vectors = try #require(vectors)
        #expect(Set(vectors.queryEvidenceTools) == OperationReceipt.queryEvidenceTools)
    }

    @Test("the calendar query-evidence tool set matches the server's")
    func calendarQueryEvidenceToolsMatch() throws {
        let vectors = try #require(vectors)
        #expect(
            Set(vectors.calendarQueryEvidenceTools)
                == OperationReceipt.calendarQueryEvidenceTools
        )
        // Two sets, and never one tool in both: a result decoded as the wrong
        // domain's card is the failure this separation exists to prevent.
        #expect(
            OperationReceipt.queryEvidenceTools
                .isDisjoint(with: OperationReceipt.calendarQueryEvidenceTools)
        )
    }

    @Test("the device-executed tool set matches the server's")
    func deviceExecutedToolsMatch() throws {
        // The server's set is IR-derived; the client's is hard-coded, and it is
        // what decides whether a success draws the calendar card or the ledger
        // receipt. The vector holds them equal, so a second device tool cannot
        // ship a receipt this build renders as a ledger row.
        let vectors = try #require(vectors)
        #expect(Set(vectors.deviceExecutedTools) == OperationReceipt.deviceExecutedTools)
        // A device tool is still an R2 write whose receipt must carry evidence;
        // the sets overlap by design and neither is a subset of the other's
        // complement. What they must not do is disagree about `record_id`.
        #expect(
            OperationReceipt.deviceExecutedTools
                .isSubset(of: OperationReceipt.recordEvidenceTools)
        )
    }

    @Test("a calendar receipt reports what the phone decided, or says it does not know")
    func calendarEvidenceIsDecodedAsDeclared() throws {
        // The three-way distinction the 2026-09-10 review found missing: a
        // `created` and a `duplicate` are both successes, and a receipt that
        // predates the field is neither. Reading the third as one of the first
        // two is how a card ends up offering a button it must not.
        let vectors = try #require(vectors)
        var seen = Set<String>()
        for vectorCase in vectors.cases {
            guard vectorCase.expectedOutcome == "calendar_event_written" else {
                continue
            }
            let receipt = try JSONDecoder().decode(
                OperationReceipt.self, from: vectorCase.chatReceipt
            )
            let declared = try #require(vectorCase.deviceEvidence)
            seen.insert(declared)
            guard
                case .calendarEventWritten(let eventID, _, let evidence, _) =
                    receipt.outcome
            else {
                Issue.record("\(vectorCase.name): not the calendar card")
                continue
            }
            // The event id is the evidence the write happened, and it is the
            // same field the ledger receipt would have shown.
            #expect(eventID == receipt.recordID)
            #expect(evidence.terminalLabel == CalendarDeviceResult(wire: declared).terminalLabel)
        }
        // All three must be covered by the file, or the distinction is asserted
        // only where it is easy.
        #expect(seen == ["created", "duplicate", "unstated"])
    }

    @Test("a calendar receipt never names the ledger")
    func calendarReceiptCopyNeverSaysLedger() {
        // The defect, stated as the assertion that would have caught it. The
        // 2026-09-10 review found a created calendar event rendering
        // 「账本已存在此记录」 beside a 打开飞书账本 link.
        for evidence in [CalendarDeviceResult.created, .duplicate, .unstated] {
            #expect(!evidence.terminalLabel.contains("账本"))
            #expect(!evidence.terminalLabel.isEmpty)
        }
        #expect(
            Set([CalendarDeviceResult.created, .duplicate, .unstated].map(\.terminalLabel))
                .count == 3,
            "two outcomes share a label, so the card would not distinguish them"
        )
    }

    @Test("the calendar domain the card forks on is the server's own value")
    func calendarDomainMatchesTheServer() throws {
        // `ManualReviewCopy.forDomain` compares against a literal in this
        // package, and the server derives its domain string from the tool's IR
        // contract. The vector is the only place the two meet: without this the
        // client could spell it "cal" and every test would still pass while no
        // calendar card was ever drawn -- the fork would fail open, silently, to
        // the ledger copy.
        let vectors = try #require(vectors)
        let review = try #require(
            vectors.cases.first { $0.name == "calendar_manual_review_keeps_record" }
        )
        #expect(review.domain == OperationReceipt.calendarDomain)
        // Every calendar tool must share one domain string, or the fork would
        // cover the write and miss the read. The query case is the other
        // calendar tool the vector carries.
        let query = try #require(
            vectors.cases.first { $0.name == "calendar_query_list_card" }
        )
        #expect(query.domain == review.domain)
        // The ledger's own case, for the other direction: it must not be the
        // value this build forks on.
        let ledger = try #require(
            vectors.cases.first { $0.name == "manual_review_keeps_record" }
        )
        #expect(ledger.domain != OperationReceipt.calendarDomain)
        #expect(ManualReviewCopy.forDomain(ledger.domain) == .ledger)
    }

    @Test("the list card renders the rows the server's own summary renders")
    func calendarCardMatchesTheServersSummary() throws {
        // The two sides render from the same fields by hand-kept rules (design
        // §13 step 6), so the vector's `answer` -- which the *server* produced
        // from those fields -- is the only thing here that can catch a drift.
        // The summary shows three lines and a total; the card shows all of the
        // page. Those three must be character-identical.
        let vectors = try #require(vectors)
        let entry = try #require(
            vectors.cases.first { $0.name == "calendar_query_list_card" }
        )
        let receipt = try JSONDecoder().decode(
            OperationReceipt.self, from: entry.chatReceipt
        )
        guard case .answeredWithCalendarQuery(let result, let tool) = receipt.outcome
        else {
            Issue.record("expected the calendar list card, got \(receipt.outcome)")
            return
        }
        #expect(tool == "calendar.query_events")
        #expect(result.recordCount == 4)
        #expect(result.nextCursor == nil)
        #expect(!result.mirrorStale)
        #expect(result.sourceSystem == "apple_calendar_mirror")
        let answer = try #require(receipt.answer)
        #expect(answer.hasSuffix("数据截至 \(result.dataAsOf)"))
        for row in result.events.prefix(3) {
            #expect(
                answer.contains(row.line),
                "the server summarised “\(row.line)” differently: \(answer)"
            )
        }
        // The fourth row is past the summary's three lines and is on the card
        // anyway -- that is what the card is for. Its calendar has no name the
        // device knows, and the row shows none rather than the raw identifier.
        #expect(result.events.count == 4)
        #expect(result.events[3].calendarTitle == nil)
        #expect(result.events[3].displayTitle == "体检")
        // `created_by_agent` reaches the row, which is what draws 已创建.
        #expect(result.events[0].createdByAgent)
        #expect(!result.events[1].createdByAgent)
        #expect(result.events[1].calendarTitle == "出游计划")
    }

    @Test("a calendar query page with a cursor says there is more")
    func calendarPageWithACursor() throws {
        // `next_cursor` is what drives 「看更多」, and the case that carries one
        // is the only place the server's own page contract is exercised.
        let page = """
        {"status":"ok","events":[],"record_count":9,"next_cursor":"cur-2",\
        "data_as_of":"2026-10-06T07:30:00+08:00","mirror_stale":false,\
        "source_system":"apple_calendar_mirror"}
        """
        let result = try JSONDecoder().decode(
            CalendarQueryResult.self, from: Data(page.utf8)
        )
        #expect(result.nextCursor == "cur-2")
        // A page carries at most what the total says exists, which is what the
        // 「另有 N 条未列出」 line counts against.
        #expect(result.recordCount == 9)
    }

    @Test("every server chatReceipt projects to the outcome both sides agreed on")
    func casesAgree() throws {
        let vectors = try #require(vectors)
        for entry in vectors.cases {
            let parsed = try JSONDecoder().decode(
                OperationReceipt.self, from: entry.chatReceipt
            )
            #expect(
                label(parsed.outcome) == entry.expectedOutcome,
                "\(entry.name): projected \(label(parsed.outcome))"
            )
            #expect(
                parsed.outcome.provesWrite == entry.expectedProvesWrite,
                "\(entry.name): provesWrite disagreed with the server contract"
            )
            #expect(
                parsed.outcome.isSettled == entry.expectedSettled,
                "\(entry.name): settled disagreed with the server contract"
            )
            #expect(
                parsed.outcome.releasesPendingSlot
                    == entry.expectedReleasesPending,
                "\(entry.name): pending-slot policy disagreed with the contract"
            )
            #expect(
                label(parsed.cancellation) == entry.expectedCancellation,
                "\(entry.name): cancellation disagreed with the server contract"
            )
            // The domain the server derived from the tool's IR contract is a
            // fact about the operation, and this client's 人工核对 card is chosen
            // by it. A receipt that decoded a different value would send the
            // person to the wrong place to check a write.
            #expect(
                parsed.domain == entry.domain,
                "\(entry.name): the decoded domain disagreed with the server's"
            )
            // Whether 「仍要创建」 belongs on this receipt, and to which action.
            // The server answers the same question in `may_override`, and the
            // Python pin holds `may_override` against this same field -- so a
            // rule changed on one side fails on both, instead of shipping a
            // button that writes a second copy of an event the user has.
            let decision = parsed.outcome.overrideDecision
            #expect(
                decision.actionID == entry.expectedOverrideActionID,
                "\(entry.name): override decision disagreed with the contract"
            )
        }
    }

    @Test("the override tool is the one the server names")
    func theOverrideToolAgrees() throws {
        let vectors = try #require(vectors)
        // Named on both sides rather than derived from `deviceExecutedTools`:
        // an override is not a property of being device-executed, it is the
        // meaning a calendar duplicate has, and a second device tool must
        // decide its own override semantics rather than inherit these.
        #expect(vectors.overrideTool == OperationReceipt.calendarDeviceTool)
        #expect(vectors.overrideTool == "calendar.create_event")
    }
}

// --- the operation progress trail ---------------------------------------------
//
// While the chat POST can still be holding the connection (the server waits up
// to 30 seconds), the client polls `GET /v1/operations/by-key/{key}` with the
// idempotency key it already holds. An unanchored key is a 400
// `OPERATION_NOT_ANCHORED`, which means "keep waiting", never a failure. The
// trail is a structured stage, never model prose.

@Suite("The operation progress trail")
struct OperationProgressTests {
    /// A receipt for a stage the trail should surface. `tool` is the fact the
    /// dispatching transition records server-side.
    private func runningReceipt(
        _ state: String, tool: Any = NSNull(), operation: String = "op-1"
    ) -> [String: Any] {
        var body = chatReceipt(state, operation: operation)
        body["tool"] = tool
        return body
    }

    private func byKeyBody(
        _ state: String, tool: Any = NSNull(), operation: String = "op-1"
    ) -> [String: Any] {
        var body = chatReceipt(state, operation: operation)
        body["tool"] = tool
        return body
    }

    @Test("a settled send never polls by key")
    func settledSendSkipsByKeyPolling() async throws {
        let service = Service()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                return .ok(chatReceipt("succeeded", tool: "finance.log_expense", recordID: "rec-1"))
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        // A real cancellable sleep, not the no-op default: the property under
        // test is that cancellation lands *during* the first wait, which a
        // no-op sleep cannot model. The first window (1s) is far longer than
        // the cold-start POST path takes, so the cancel always arrives before
        // poll one, and it costs nothing — the sleep never completes.
        let (chat, _, _) = try await makeChat(
            service: service,
            pollDelays: [.seconds(1), .zero, .zero, .zero],
            sleep: { try await Task.sleep(for: $0) }
        )

        let final = try await chat.send(text: "咖啡 18")

        #expect(final.outcome.isSettled)
        // No by-key request ever left: the POST settled inside the first wait,
        // so the trail was cancelled before its first poll.
        let byKeyCalls = service.log.filter {
            $0.method == "GET" && $0.path.contains("/v1/operations/by-key/")
        }
        #expect(byKeyCalls.isEmpty)
    }

    @Test("a running send surfaces the stages the settle polls prove")
    func runningSendSurfacesSettleStages() async throws {
        let service = Service()
        service.answer { call, seen in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                return seen == 0
                    ? .accepted(chatReceipt("accepted"))
                    : .error(500, "INTERNAL_ERROR")
            case ("GET", "/v1/operations/op-1"):
                return .accepted(chatReceipt("source_in_progress"))
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let (chat, _, store) = try await makeChat(service: service)
        let trail = OperationTrail()
        await chat.setProgressSink { stage in trail.record(stage) }

        // The POST replies with an accepted receipt; settle() then polls op-1,
        // which stays running until the schedule runs out.
        let final = try await chat.send(text: "咖啡 18")
        #expect(final.outcome == .running)

        let seen = trail.stages
        #expect(seen.contains(.accepted))
        #expect(seen.contains(.sourceInProgress))
        #expect(try store.read(CredentialKey.pendingChatSend) != nil)
    }

    @Test("an unanchored key during the POST window keeps the trail at accepted")
    func unanchoredKeyKeepsWaiting() async throws {
        let service = Service()
        let keyBox = KeyBox()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                if let key = call.idempotencyKey { keyBox.set(key) }
                // Hold the POST open: never settle within the poll schedule.
                return .accepted(chatReceipt("accepted"))
            case ("GET", "/v1/operations/op-1"):
                return .accepted(chatReceipt("interpreting"))
            default:
                // by-key before anchor: the server's OPERATION_NOT_ANCHORED.
                return .error(400, "OPERATION_NOT_ANCHORED")
            }
        }
        let (chat, _, _) = try await makeChat(service: service)
        let trail = OperationTrail()
        await chat.setProgressSink { stage in trail.record(stage) }

        let final = try await chat.send(text: "咖啡 18")
        #expect(final.outcome == .running)

        // The trail never claimed a stage beyond what the server proved: the
        // unanchored by-key answers were swallowed, and only the settle polls'
        // states were reported.
        let seen = trail.stages
        #expect(seen.contains(.accepted))
        #expect(seen.contains(.interpreting))
        #expect(!seen.contains { stage in
            if case .dispatching = stage { return true }
            return false
        })
    }

    @Test("a transport failure on the by-key poll does not kill the settle loop")
    func byKeyTransportFailureIsSurvivable() async throws {
        let service = Service()
        service.answer { call, seen in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                return .accepted(chatReceipt("accepted"))
            case ("GET", "/v1/operations/op-1"):
                return seen == 3
                    ? .ok(chatReceipt("succeeded", tool: "finance.log_expense", recordID: "rec-1"))
                    : .accepted(chatReceipt("dispatching", tool: "finance.log_expense"))
            default:
                return .init(status: 599, body: Data())
            }
        }
        let (chat, _, store) = try await makeChat(service: service)

        let final = try await chat.send(text: "咖啡 18")

        #expect(final.outcome == .recorded(recordID: "rec-1", tool: "finance.log_expense", record: nil))
        #expect(try store.read(CredentialKey.pendingChatSend) == nil)
    }
}

/// Collects the stages a test's sink received.
final class OperationTrail: @unchecked Sendable {
    private let lock = NSLock()
    private var _stages: [OperationStage] = []

    func record(_ stage: OperationStage) {
        lock.withLock { _stages.append(stage) }
    }

    var stages: [OperationStage] {
        lock.withLock { _stages }
    }
}

/// Holds the idempotency key across the actor boundary.
final class KeyBox: @unchecked Sendable {
    private let lock = NSLock()
    private var _key: String?
    func set(_ key: String) { lock.withLock { _key = key } }
    var key: String? { lock.withLock { _key } }
}

/// Blocks the calling thread until `predicate` holds or five seconds pass,
/// then returns either way.
///
/// The POST handlers of this suite hold the connection until the evidence
/// their assertions need has landed, instead of sleeping a fixed interval: a
/// fixed sleep races machine speed, and the CI runner lost that race — one
/// by-key poll inside a 30 ms window where a laptop found two. A starved gate
/// answers anyway so the assertions below fail loudly instead of hanging.
func waitForGate(
    _ name: String,
    until predicate: @escaping @Sendable () -> Bool
) {
    let deadline = Date().addingTimeInterval(5)
    while !predicate() && Date() < deadline {
        Thread.sleep(forTimeInterval: 0.001)
    }
    if !predicate() {
        print("trail gate starved after 5s: \(name)")
    }
}

@Suite("The concurrent trail beside the POST")
struct TrailConcurrencyTests {
    @Test("by-key polls run while the POST is open and stop after it answers")
    func trailPollsBesideThePost() async throws {
        let service = Service()
        let keyBox = KeyBox()
        // A held POST: it answers only once the trail has demonstrably polled
        // by-key at least twice beside the open connection — and never reaches
        // a settle poll. A fixed sleep raced the CI runner's speed (one slow
        // round trip ate the whole window); the gate makes "polled beside the
        // POST" a precondition of the answer instead.
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                if let key = call.idempotencyKey { keyBox.set(key) }
                waitForGate("two by-key polls beside the open POST") {
                    guard let key = keyBox.key else { return false }
                    return service.calls("GET", "/v1/operations/by-key/\(key)").count >= 2
                }
                return .ok(
                    chatReceipt("succeeded", tool: "finance.log_expense", recordID: "rec-1")
                )
            default:
                return .error(400, "OPERATION_NOT_ANCHORED")
            }
        }
        let (chat, _, store) = try await makeChat(
            service: service,
            // Real pacing, not a zero-delay burst: with `.zero` delays and the
            // no-op test sleep the trail spends all eight attempts in the few
            // microseconds before the POST handler starts holding the
            // connection, and a gate waiting for polls *inside* the hold
            // starves. 25 ms spacing spreads the polls over the hold; the
            // second one reliably lands while the gate is closed.
            pollDelays: Array(repeating: .milliseconds(25), count: 8),
            sleep: { try await Task.sleep(for: $0) }
        )
        let trail = OperationTrail()
        await chat.setProgressSink { stage in trail.record(stage) }

        let final = try await chat.send(text: "咖啡 18")
        #expect(final.outcome.isSettled)

        let key = try #require(keyBox.key)
        let byKeyPolls = service.calls("GET", "/v1/operations/by-key/\(key)")
        #expect(byKeyPolls.count >= 2, "the trail polled beside the open POST")
        #expect(try store.read(CredentialKey.pendingChatSend) == nil)
    }

    @Test("a dispatching observation beside the POST carries the server's tool name")
    func trailCarriesToolBesideThePost() async throws {
        let service = Service()
        let keyBox = KeyBox()
        let trail = OperationTrail()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                if let key = call.idempotencyKey { keyBox.set(key) }
                // Hold until the sink has actually recorded the dispatching
                // stage — the very evidence the assertion below needs — so
                // the observation is not a race against runner speed.
                waitForGate("the sink observed the dispatching stage") {
                    trail.stages.contains { stage in
                        if case .dispatching = stage { return true }
                        return false
                    }
                }
                return .ok(
                    chatReceipt("succeeded", tool: "finance.log_expense", recordID: "rec-1")
                )
            default:
                // The server anchored and dispatched while the POST held.
                return .ok(
                    chatReceipt("dispatching", tool: "finance.log_expense")
                )
            }
        }
        let (chat, _, _) = try await makeChat(
            service: service,
            // Same real pacing as above: the dispatching poll must arrive
            // while the POST holds, not in the pre-hold burst.
            pollDelays: Array(repeating: .milliseconds(25), count: 8),
            sleep: { try await Task.sleep(for: $0) }
        )
        await chat.setProgressSink { stage in trail.record(stage) }

        _ = try await chat.send(text: "咖啡 18")

        let seen = trail.stages
        #expect(seen.contains { stage in
            if case .dispatching(let tool) = stage, tool == "finance.log_expense" {
                return true
            }
            return false
        })
    }

    @Test("the settle loop keeps its throw-through semantics")
    func settleStillThrowsThroughTransportErrors() async throws {
        let service = Service()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                return .accepted(chatReceipt("accepted"))
            case ("GET", "/v1/operations/op-1"):
                // Every settle poll fails at the transport level.
                return .init(status: 599, body: Data())
            default:
                return .error(400, "OPERATION_NOT_ANCHORED")
            }
        }
        let (chat, _, store) = try await makeChat(
            service: service,
            pollDelays: [.zero, .zero, .zero, .zero]
        )

        // The by-key trail swallows its failures; the settle loop does not.
        // A send that ends with every poll failed leaves the slot standing.
        await #expect(throws: (any Error).self) {
            _ = try await chat.send(text: "咖啡 18")
        }
        #expect(try store.read(CredentialKey.pendingChatSend) != nil)
    }
}
