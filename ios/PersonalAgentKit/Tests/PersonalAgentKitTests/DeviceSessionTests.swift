import Foundation
import Testing

@testable import PersonalAgentKit

/// The session's failure shapes, driven through a stubbed `URLProtocol` so the
/// real `AgentClient`, the real JSON decoding and the real refresh policy all run.
///
/// The cases were chosen the way §5.1 requires: a token that expires, a device
/// revoked mid-session, a server that answers 200 with the wrong shape, a
/// challenge that names another device. A fake that only ever answers correctly
/// would prove nothing about any of them.
private final class StubProtocol: URLProtocol {
    struct Route {
        let status: Int
        let body: Data
    }

    nonisolated(unsafe) static var handler: (@Sendable (URLRequest) -> Route)?
    nonisolated(unsafe) static var requests: [String] = []
    /// Every header set the wire actually carried, per request, in arrival
    /// order. Recorded separately from `requests` so a test can ask what a
    /// request *declared* and not only where it went.
    nonisolated(unsafe) static var headers: [[String: String]] = []
    private static let lock = NSLock()

    static func record(_ path: String) {
        lock.withLock { requests.append(path) }
    }

    static func record(_ request: URLRequest) {
        lock.withLock { headers.append(request.allHTTPHeaderFields ?? [:]) }
    }

    static func count(_ path: String) -> Int {
        lock.withLock { requests.filter { $0 == path }.count }
    }

    /// The value a named header carried on **every** request this stub saw.
    /// Returns nil when any request omitted it, or when none was made — a
    /// declaration that holds on some requests and not others is exactly the
    /// drift this is here to catch.
    static func unanimousHeader(_ name: String) -> String? {
        let seen = lock.withLock { headers }
        guard !seen.isEmpty else { return nil }
        let values = seen.map { $0[name] }
        guard let first = values.first, first != nil else { return nil }
        return values.allSatisfy { $0 == first } ? first : nil
    }

    static func reset() {
        lock.withLock {
            requests = []
            headers = []
        }
        handler = nil
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let path = request.url?.path ?? ""
        StubProtocol.record(path)
        StubProtocol.record(request)
        let route = StubProtocol.handler?(request) ?? Route(status: 500, body: Data())
        let response = HTTPURLResponse(
            url: request.url!,
            statusCode: route.status,
            httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: route.body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

/// A clock and a counter the stub can share with the test body. Swift 6 will not
/// let a `@Sendable` closure capture a mutable local, and the alternative --
/// making them globals -- would leak state between tests.
private final class Box<T>: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: T

    init(_ value: T) { stored = value }

    var value: T {
        get { lock.withLock { stored } }
        set { lock.withLock { stored = newValue } }
    }
}

private enum InjectedStoreError: Error {
    case writeFailed
}

private final class FailingEnrollmentStore: CredentialStore, @unchecked Sendable {
    private let backing = InMemoryCredentialStore()

    func read(_ key: String) throws -> Data? {
        try backing.read(key)
    }

    func write(_ key: String, value: Data) throws {
        if key == CredentialKey.enrollment {
            throw InjectedStoreError.writeFailed
        }
        try backing.write(key, value: value)
    }

    func delete(_ key: String) throws {
        try backing.delete(key)
    }
}

private func json(_ object: [String: Any]) -> Data {
    try! JSONSerialization.data(withJSONObject: object)
}

private let deviceID = "018f0000-0000-7000-8000-000000000002"

private func stubbedClient() throws -> AgentClient {
    let configuration = URLSessionConfiguration.ephemeral
    configuration.protocolClasses = [StubProtocol.self]
    return try AgentClient(
        baseURL: URL(string: "http://127.0.0.1:8810")!,
        session: URLSession(configuration: configuration)
    )
}

private func challengeBody(id: String = "chal-1", device: String = deviceID, audience: String = "personal-agent-api") -> Data {
    json([
        "challenge_id": id,
        "device_id": device,
        "nonce": DeviceWireContract.base64URLEncode(Data(repeating: 7, count: 32)),
        "audience": audience,
        "expires_at": "2026-07-26T07:02:00+00:00",
    ])
}

private func tokenBody(value: String, device: String = deviceID) -> Data {
    json([
        "access_token": value,
        "token_type": "Bearer",
        "expires_in": 600,
        "device_id": device,
        "scopes": ["device.self.read"],
        "allowed_tools_version": "v1",
    ])
}

private let enrolledBody = json([
    "device_id": deviceID,
    "display_name": "iPhone",
    "device_key_thumbprint": "thumb",
    "scopes": ["device.self.read"],
    "allowed_tools_version": "v1",
    "created_at": "2026-07-26T07:00:00+00:00",
])

private let capabilitiesBody = json([
    "allowed_tools_version": "v1",
    "tools": [["alias": "meta.capabilities"]],
    // `DEV-030`: the canonical Timeline id is part of this contract, and the
    // client refuses a capabilities body without it.
    "conversation_id": "tl_0000000000000000000000000000beef",
])

private let devicesBody = json([
    "devices": [
        [
            "device_id": deviceID,
            "display_name": "iPhone",
            "status": "active",
            "is_self": true,
            "has_push_token": false,
            "allowed_tools_version": "v1",
            "created_at": "2026-07-26T07:00:00+00:00",
            "revoked_at": NSNull(),
        ]
    ]
])

@Suite("The device session", .serialized)
struct DeviceSessionTests {
    private func session(
        store: CredentialStore = InMemoryCredentialStore(),
        clock: @escaping @Sendable () -> Date = { Date(timeIntervalSince1970: 1_000) }
    ) throws -> DeviceSession {
        DeviceSession(client: try stubbedClient(), store: store,
                      identityFactory: .softwareForTests, now: clock)
    }

    @Test("enrollment persists the device and a later launch restores it")
    func enrollmentPersists() async throws {
        StubProtocol.reset()
        StubProtocol.handler = { _ in .init(status: 201, body: enrolledBody) }
        let store = InMemoryCredentialStore()
        let first = try session(store: store)
        _ = try await first.enroll(code: "code", displayName: "iPhone")
        #expect(await first.state == .enrolled(deviceID: deviceID))
        #expect(await first.deviceKeyKind != nil)
        #expect(try store.read(CredentialKey.enrollment) != nil)
        #expect(
            try DeviceSession.storedBaseURL(in: store)?.absoluteString
                == "http://127.0.0.1:8810"
        )

        let second = try session(store: store)
        #expect(try await second.restore() == .enrolled(deviceID: deviceID))
    }

    @Test("the first simulator format migrates into one enrollment envelope")
    func legacyEnrollmentMigrates() async throws {
        StubProtocol.reset()
        let identity = SoftwareDeviceIdentity()
        let store = InMemoryCredentialStore([
            CredentialKey.deviceKeyBlob: identity.persistentRepresentation,
            CredentialKey.deviceKeyKind: Data(DeviceKeyKind.software.rawValue.utf8),
            CredentialKey.deviceID: Data(deviceID.utf8),
            CredentialKey.baseURL: Data("http://127.0.0.1:8810".utf8),
        ])
        let session = try session(store: store)

        #expect(try await session.restore() == .enrolled(deviceID: deviceID))
        #expect(try store.read(CredentialKey.enrollment) != nil)
    }

    @Test("a refused code leaves nothing persisted")
    func refusedCodePersistsNothing() async throws {
        StubProtocol.reset()
        StubProtocol.handler = { _ in
            .init(status: 403, body: json(["error": ["code": "ENROLLMENT_REJECTED"]]))
        }
        let store = InMemoryCredentialStore()
        let session = try session(store: store)
        await #expect(throws: AgentClientError.enrollmentRejected) {
            _ = try await session.enroll(code: "wrong", displayName: "iPhone")
        }
        #expect(try store.read(CredentialKey.deviceID) == nil)
        #expect(try store.read(CredentialKey.deviceKeyBlob) == nil)
        #expect(await session.state == .notEnrolled)
    }

    @Test("a local persistence failure revokes the newly enrolled server device")
    func persistenceFailureRevokesServerDevice() async throws {
        StubProtocol.reset()
        StubProtocol.handler = { request in
            switch request.url?.path {
            case "/v1/enrollments/claim":
                return .init(status: 201, body: enrolledBody)
            case "/v1/auth/challenges":
                return .init(status: 200, body: challengeBody())
            case "/v1/auth/tokens":
                return .init(status: 200, body: tokenBody(value: "cleanup-token"))
            case "/v1/devices/\(deviceID)":
                return .init(
                    status: 200,
                    body: json([
                        "device_id": deviceID,
                        "display_name": "iPhone",
                        "status": "revoked",
                        "is_self": true,
                        "has_push_token": false,
                        "allowed_tools_version": "v1",
                        "created_at": "2026-07-26T07:00:00+00:00",
                        "revoked_at": "2026-07-26T08:00:00+00:00",
                    ])
                )
            default:
                return .init(status: 500, body: Data())
            }
        }
        let store = FailingEnrollmentStore()
        let session = try session(store: store)

        do {
            _ = try await session.enroll(code: "code", displayName: "iPhone")
            Issue.record("enrollment should fail when its local envelope cannot be stored")
        } catch let error as DeviceSessionError {
            #expect(
                error == .localPersistenceFailed(
                    deviceID: deviceID,
                    serverDeviceRevoked: true
                )
            )
        }
        #expect(StubProtocol.count("/v1/devices/\(deviceID)") == 1)
        #expect(try store.read(CredentialKey.enrollment) == nil)
        #expect(await session.state == .notEnrolled)
    }

    @Test("one token is minted for many calls, and reused until it expires")
    func tokenIsCached() async throws {
        StubProtocol.reset()
        StubProtocol.handler = { request in
            switch request.url?.path {
            case "/v1/enrollments/claim": return .init(status: 201, body: enrolledBody)
            case "/v1/auth/challenges": return .init(status: 200, body: challengeBody())
            case "/v1/auth/tokens": return .init(status: 200, body: tokenBody(value: "t1"))
            case "/v1/devices": return .init(status: 200, body: devicesBody)
            default: return .init(status: 200, body: capabilitiesBody)
            }
        }
        let seconds = Box(1_000.0)
        let clock: @Sendable () -> Date = { Date(timeIntervalSince1970: seconds.value) }
        let session = try session(clock: clock)
        _ = try await session.enroll(code: "code", displayName: "iPhone")

        _ = try await session.capabilities()
        _ = try await session.capabilities()
        _ = try await session.devices()
        #expect(StubProtocol.count("/v1/auth/challenges") == 1)

        // Past the refresh margin: the next call mints a second token.
        seconds.value += 600
        _ = try await session.capabilities()
        #expect(StubProtocol.count("/v1/auth/challenges") == 2)
    }

    @Test("concurrent callers share a single challenge")
    func concurrentCallersShareRefresh() async throws {
        StubProtocol.reset()
        StubProtocol.handler = { request in
            switch request.url?.path {
            case "/v1/enrollments/claim": return .init(status: 201, body: enrolledBody)
            case "/v1/auth/challenges": return .init(status: 200, body: challengeBody())
            case "/v1/auth/tokens": return .init(status: 200, body: tokenBody(value: "t1"))
            default: return .init(status: 200, body: capabilitiesBody)
            }
        }
        let session = try session()
        _ = try await session.enroll(code: "code", displayName: "iPhone")
        await withThrowingTaskGroup(of: Void.self) { group in
            for _ in 0..<8 {
                group.addTask { _ = try await session.capabilities() }
            }
        }
        #expect(StubProtocol.count("/v1/auth/challenges") == 1)
    }

    @Test("an unauthenticated call is retried exactly once")
    func oneRetryOnly() async throws {
        StubProtocol.reset()
        let capabilityCalls = Box(0)
        StubProtocol.handler = { request in
            switch request.url?.path {
            case "/v1/enrollments/claim": return .init(status: 201, body: enrolledBody)
            case "/v1/auth/challenges": return .init(status: 200, body: challengeBody())
            case "/v1/auth/tokens": return .init(status: 200, body: tokenBody(value: "t1"))
            default:
                capabilityCalls.value += 1
                return .init(status: 401, body: json(["error": ["code": "UNAUTHENTICATED"]]))
            }
        }
        let session = try session()
        _ = try await session.enroll(code: "code", displayName: "iPhone")
        await #expect(throws: AgentClientError.unauthenticated) {
            _ = try await session.capabilities()
        }
        #expect(capabilityCalls.value == 2)
        #expect(StubProtocol.count("/v1/auth/challenges") == 2)
    }

    @Test("a revoked device is reported and its key is not wiped")
    func revokedDeviceKeepsItsKey() async throws {
        StubProtocol.reset()
        let enrolled = Box(false)
        StubProtocol.handler = { request in
            switch request.url?.path {
            case "/v1/enrollments/claim":
                enrolled.value = true
                return .init(status: 201, body: enrolledBody)
            case "/v1/auth/challenges":
                return .init(
                    status: 401, body: json(["error": ["code": "DEVICE_AUTH_REJECTED"]])
                )
            default:
                return .init(status: 200, body: capabilitiesBody)
            }
        }
        let store = InMemoryCredentialStore()
        let session = try session(store: store)
        _ = try await session.enroll(code: "code", displayName: "iPhone")
        #expect(enrolled.value)

        await #expect(throws: AgentClientError.deviceRejected) {
            _ = try await session.capabilities()
        }
        #expect(await session.state == .rejected(deviceID: deviceID))
        // The enrollment envelope survives, so the app can explain the state
        // instead of silently resetting itself.
        #expect(try store.read(CredentialKey.enrollment) != nil)

        // A rejected session does not keep hammering the server.
        let before = StubProtocol.count("/v1/auth/challenges")
        await #expect(throws: AgentClientError.deviceRejected) {
            _ = try await session.capabilities()
        }
        #expect(StubProtocol.count("/v1/auth/challenges") == before)
    }

    @Test("a challenge for another device is never signed")
    func challengeForAnotherDevice() async throws {
        StubProtocol.reset()
        StubProtocol.handler = { request in
            switch request.url?.path {
            case "/v1/enrollments/claim": return .init(status: 201, body: enrolledBody)
            case "/v1/auth/challenges":
                return .init(status: 200, body: challengeBody(device: "someone-else"))
            default: return .init(status: 200, body: capabilitiesBody)
            }
        }
        let session = try session()
        _ = try await session.enroll(code: "code", displayName: "iPhone")
        await #expect(throws: DeviceSessionError.challengeNotForThisDevice) {
            _ = try await session.capabilities()
        }
        #expect(StubProtocol.count("/v1/auth/tokens") == 0)
    }

    @Test("a challenge with an unexpected audience is never signed")
    func unexpectedAudience() async throws {
        StubProtocol.reset()
        StubProtocol.handler = { request in
            switch request.url?.path {
            case "/v1/enrollments/claim": return .init(status: 201, body: enrolledBody)
            case "/v1/auth/challenges":
                return .init(status: 200, body: challengeBody(audience: "some-other-api"))
            default: return .init(status: 200, body: capabilitiesBody)
            }
        }
        let session = try session()
        _ = try await session.enroll(code: "code", displayName: "iPhone")
        await #expect(throws: DeviceSessionError.unexpectedAudience("some-other-api")) {
            _ = try await session.capabilities()
        }
        #expect(StubProtocol.count("/v1/auth/tokens") == 0)
    }

    @Test("a token issued for another device is refused")
    func tokenForAnotherDevice() async throws {
        StubProtocol.reset()
        StubProtocol.handler = { request in
            switch request.url?.path {
            case "/v1/enrollments/claim": return .init(status: 201, body: enrolledBody)
            case "/v1/auth/challenges": return .init(status: 200, body: challengeBody())
            case "/v1/auth/tokens":
                return .init(status: 200, body: tokenBody(value: "t1", device: "other"))
            default: return .init(status: 200, body: capabilitiesBody)
            }
        }
        let session = try session()
        _ = try await session.enroll(code: "code", displayName: "iPhone")
        await #expect(throws: AgentClientError.malformedResponse) {
            _ = try await session.capabilities()
        }
    }

    @Test("a 200 with the wrong shape is not a success")
    func malformedSuccessBody() async throws {
        StubProtocol.reset()
        StubProtocol.handler = { request in
            switch request.url?.path {
            case "/v1/enrollments/claim": return .init(status: 201, body: enrolledBody)
            case "/v1/auth/challenges": return .init(status: 200, body: challengeBody())
            case "/v1/auth/tokens": return .init(status: 200, body: tokenBody(value: "t1"))
            default: return .init(status: 200, body: json(["unexpected": "shape"]))
            }
        }
        let session = try session()
        _ = try await session.enroll(code: "code", displayName: "iPhone")
        await #expect(throws: AgentClientError.malformedResponse) {
            _ = try await session.capabilities()
        }
    }

    @Test("calling before enrollment is an error, not an anonymous request")
    func notEnrolled() async throws {
        StubProtocol.reset()
        StubProtocol.handler = { _ in .init(status: 200, body: capabilitiesBody) }
        let session = try session()
        await #expect(throws: DeviceSessionError.notEnrolled) {
            _ = try await session.capabilities()
        }
        #expect(StubProtocol.count("/v1/capabilities") == 0)
    }

    @Test("a non-HTTP base URL is refused at construction")
    func badBaseURL() {
        #expect(throws: AgentClientError.invalidBaseURL) {
            _ = try AgentClient(baseURL: URL(string: "ftp://example.com")!)
        }
        #expect(throws: AgentClientError.invalidBaseURL) {
            _ = try AgentClient(baseURL: URL(string: "file:///etc/passwd")!)
        }
    }

    @Test("self-revocation stops using the token and reports the rejection")
    func selfRevocation() async throws {
        StubProtocol.reset()
        StubProtocol.handler = { request in
            switch request.url?.path {
            case "/v1/enrollments/claim": return .init(status: 201, body: enrolledBody)
            case "/v1/auth/challenges": return .init(status: 200, body: challengeBody())
            case "/v1/auth/tokens": return .init(status: 200, body: tokenBody(value: "t1"))
            default:
                return .init(
                    status: 200,
                    body: json([
                        "device_id": deviceID,
                        "display_name": "iPhone",
                        "status": "revoked",
                        "is_self": true,
                        "has_push_token": false,
                        "allowed_tools_version": "v1",
                        "created_at": "2026-07-26T07:00:00+00:00",
                        "revoked_at": "2026-07-26T08:00:00+00:00",
                    ])
                )
            }
        }
        let session = try session()
        _ = try await session.enroll(code: "code", displayName: "iPhone")
        let summary = try await session.revokeSelf()
        #expect(summary.status == "revoked")
        #expect(await session.state == .rejected(deviceID: deviceID))
    }
}

// --- the declared wire version ------------------------------------------------

/// A stub of this suite's own, deliberately not `StubProtocol`.
///
/// `StubProtocol` answers through one process-wide static handler, and the two
/// suites in this file run concurrently (`.serialized` orders tests *within* a
/// suite, never between two). Sharing it made the enrollment suite answer 500
/// whenever this suite's handler won the race — the assertion here never reads
/// a reply, so the interference could only ever show up as someone else's
/// failure. This stub has no handler to race on: its answer is fixed at 500 by
/// construction, which is exactly what the test needs (the header is set before
/// the request leaves, so an unusable body is the point).
private final class HeaderStubProtocol: URLProtocol {
    nonisolated(unsafe) static var headers: [[String: String]] = []
    nonisolated(unsafe) static var paths: [String] = []
    private static let lock = NSLock()

    static func reset() {
        lock.withLock {
            headers = []
            paths = []
        }
    }

    static func count(_ path: String) -> Int {
        lock.withLock { paths.filter { $0 == path }.count }
    }

    /// The value a named header carried on **every** request this stub saw.
    /// Nil when any request omitted it, or when none was made — a declaration
    /// that holds on some requests and not others is exactly the drift this is
    /// here to catch.
    static func unanimousHeader(_ name: String) -> String? {
        let seen = lock.withLock { headers }
        guard !seen.isEmpty else { return nil }
        let values = seen.map { $0[name] }
        guard let first = values.first, first != nil else { return nil }
        return values.allSatisfy { $0 == first } ? first : nil
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let recorded = request.allHTTPHeaderFields ?? [:]
        HeaderStubProtocol.lock.withLock {
            HeaderStubProtocol.headers.append(recorded)
            HeaderStubProtocol.paths.append(request.url?.path ?? "")
        }
        let response = HTTPURLResponse(
            url: request.url!, statusCode: 500, httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data())
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    static func client() throws -> AgentClient {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [HeaderStubProtocol.self]
        return try AgentClient(
            baseURL: URL(string: "http://127.0.0.1:8811")!,
            session: URLSession(configuration: configuration)
        )
    }
}

/// The header the server's calendar issuance and delivery gates read (design
/// §2.5, R1-F1).
///
/// What is defended here is not "a header is set" but "**every** request
/// declares it". A client that declared its capability only on the calls it
/// remembered to mark would be read as v1 on the one it forgot — and v1 is not
/// a degraded experience at this boundary: it is a refusal to issue, or an
/// action withheld after the operation was already parked.
@Suite("The declared client wire version", .serialized)
struct ClientWireVersionTests {
    @Test("this build declares the version it actually implements")
    func versionIsTheImplementedOne() {
        // v4 adds model-led chat results to v3's calendar sync epoch.
        // This build decodes ResultEnvelope v2 and declares that capability.
        #expect(ClientWireVersion.version == 6)
        #expect(ClientWireVersion.value == "6")
        #expect(ClientWireVersion.header == "X-Client-Wire-Version")
    }

    @Test("every request this build sends declares it, without exception")
    func everyRequestDeclaresIt() async throws {
        HeaderStubProtocol.reset()
        let client = try HeaderStubProtocol.client()
        let key = "018f0000-0000-7000-8000-0000000000ff"

        _ = try? await client.requestChallenge(deviceID: deviceID)
        _ = try? await client.sendChatMessage(
            conversationID: "tl_1", text: "hi", idempotencyKey: key, token: "t"
        )
        _ = try? await client.sendChatMessage(
            conversationID: "tl_1", parts: [.text("photo"), .imageReference(mediaID: key)],
            idempotencyKey: key, token: "t"
        )
        _ = try? await client.putMediaContent(mediaID: key, body: Data([0xff, 0xd8, 0xff]), token: "t")
        _ = try? await client.readMedia(mediaID: key, token: "t")
        _ = try? await client.operation(operationID: "op_1", token: "t")
        _ = try? await client.reportDeviceActionResult(
            actionID: key, body: .failed(detail: nil), token: "t"
        )
        _ = try? await client.uploadCalendarSync(
            windowStart: Date(timeIntervalSince1970: 0),
            windowEnd: Date(timeIntervalSince1970: 86_400),
            events: [],
            calendars: [],
            windowComplete: true,
            snapshotAsOf: Date(timeIntervalSince1970: 0),
            syncEpoch: 1,
            token: "t"
        )

        // The requests really went out; a stub that recorded nothing would
        // make the assertion below pass for the wrong reason.
        #expect(HeaderStubProtocol.count("/v1/chat/messages") == 2)
        #expect(HeaderStubProtocol.count("/v1/calendar/sync") == 1)
        #expect(
            HeaderStubProtocol.unanimousHeader(ClientWireVersion.header)
                == ClientWireVersion.value
        )
    }
}
