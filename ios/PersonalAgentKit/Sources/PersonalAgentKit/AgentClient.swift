import Foundation

/// The HTTP client for the Agent API, design 5.1 and 5.3.
///
/// Two rules shape every method:
///
/// - **an unexpected response is never a success.** Every body is decoded into a
///   declared shape; a 200 whose JSON does not match is `malformedResponse`, not
///   an empty result. The same reasoning as the server's "unreadable is never
///   empty" rule -- a silently-empty capability list would look exactly like a
///   correctly-configured service that grants nothing.
/// - **the client says what it knows.** `unauthenticated` (this token is no good),
///   `deviceRejected` (this device cannot get a token at all) and
///   `enrollmentRejected` are separate cases, because the app's reaction to each
///   is different and guessing produces either a spurious re-enrollment prompt or
///   an infinite refresh loop.
public struct AgentClient: Sendable {
    public let baseURL: URL
    private let session: URLSession

    /// - Parameter baseURL: the service origin, e.g. `https://agent.example.com`.
    ///   A non-HTTP(S) scheme is refused here rather than at the first request.
    public init(baseURL: URL, session: URLSession = .shared) throws {
        guard let scheme = baseURL.scheme?.lowercased(),
              scheme == "https" || scheme == "http"
        else {
            throw AgentClientError.invalidBaseURL
        }
        self.baseURL = baseURL
        self.session = session
    }

    // --- unauthenticated: enrollment and tokens ------------------------------

    public func claimEnrollment(
        code: String,
        publicKeyB64U: String,
        displayName: String
    ) async throws -> EnrolledDevice {
        try await send(
            method: "POST",
            path: "/v1/enrollments/claim",
            body: [
                "code": code,
                "public_key": publicKeyB64U,
                "display_name": displayName,
            ],
            as: EnrolledDevice.self
        )
    }

    public func requestChallenge(deviceID: String) async throws -> DeviceChallenge {
        try await send(
            method: "POST",
            path: "/v1/auth/challenges",
            body: ["device_id": deviceID],
            as: DeviceChallenge.self
        )
    }

    public func requestToken(
        challenge: DeviceChallenge,
        signatureB64U: String
    ) async throws -> IssuedToken {
        try await send(
            method: "POST",
            path: "/v1/auth/tokens",
            body: [
                "challenge_id": challenge.challengeID,
                "device_id": challenge.deviceID,
                // The server stores only a hash of the nonce, so it has to be
                // presented back to be verified. Design 4.2.1's example body
                // omits it; the server contract requires it.
                "nonce": challenge.nonce,
                "signature": signatureB64U,
            ],
            as: IssuedToken.self
        )
    }

    // --- authenticated -------------------------------------------------------

    public func capabilities(token: String) async throws -> Capabilities {
        try await send(
            method: "GET", path: "/v1/capabilities", token: token, as: Capabilities.self
        )
    }

    public func devices(token: String) async throws -> DeviceList {
        try await send(
            method: "GET", path: "/v1/devices", token: token, as: DeviceList.self
        )
    }

    public func revokeDevice(deviceID: String, token: String) async throws -> DeviceSummary {
        try await send(
            method: "DELETE",
            path: "/v1/devices/\(deviceID)",
            token: token,
            as: DeviceSummary.self
        )
    }

    /// Design 5.1's push-token route. There is no caller yet: a Personal Team
    /// build has no APNs entitlement, so the app never obtains a token to send.
    /// It is here so the two sides stay one contract rather than two.
    public func setPushToken(
        deviceID: String, pushToken: String?, token: String
    ) async throws -> PushTokenState {
        try await send(
            method: "PUT",
            path: "/v1/devices/\(deviceID)/push-token",
            body: ["push_token": pushToken],
            token: token,
            as: PushTokenState.self
        )
    }

    // --- chat, operations and the Timeline (`DEV-030`) ------------------------

    /// Post one chat message.
    ///
    /// `idempotencyKey` is the caller's, not this method's, and that is the whole
    /// point: a retry after a lost reply must present the **same** key so the
    /// server replays the same operation instead of starting a second write.
    /// `conversationID` must be the id the server itself handed out — the client
    /// neither creates nor manages Sessions.
    ///
    /// A `202` is a normal answer here, not an error: the model turn continues
    /// server-side and the reply carries the durable `operation_id` to poll.
    public func sendChatMessage(
        conversationID: String,
        text: String,
        clarificationOf: String? = nil,
        idempotencyKey: String,
        token: String
    ) async throws -> OperationReceipt {
        try await send(
            method: "POST",
            path: "/v1/chat/messages",
            body: [
                "conversation_id": conversationID,
                "text": text,
                "clarification_of": clarificationOf,
            ],
            token: token,
            headers: ["Idempotency-Key": idempotencyKey],
            accepting: [200, 202],
            as: OperationReceipt.self
        )
    }

    public func operation(
        operationID: String, token: String
    ) async throws -> OperationReceipt {
        try await send(
            method: "GET",
            path: "/v1/operations/\(operationID)",
            token: token,
            accepting: [200, 202],
            as: OperationReceipt.self
        )
    }

    /// Ask the server to cancel. The reply is the operation's *current* state:
    /// once a source submit may have happened this only records the request, and
    /// the accounting outcome still comes from the server.
    public func cancelOperation(
        operationID: String, token: String
    ) async throws -> OperationReceipt {
        try await send(
            method: "DELETE",
            path: "/v1/operations/\(operationID)",
            token: token,
            accepting: [200, 202],
            as: OperationReceipt.self
        )
    }

    /// One page of history. `direction: .newer` requires a cursor server-side, so
    /// asking for it without one is refused here rather than as a 400.
    public func timelinePage(
        conversationID: String,
        cursor: String? = nil,
        direction: TimelineDirection = .older,
        limit: Int? = nil,
        token: String
    ) async throws -> TimelinePageResponse {
        if direction == .newer && cursor == nil {
            throw AgentClientError.cursorRequired
        }
        var query = [URLQueryItem(name: "direction", value: direction.rawValue)]
        if let cursor { query.append(URLQueryItem(name: "cursor", value: cursor)) }
        if let limit { query.append(URLQueryItem(name: "limit", value: String(limit))) }
        return try await send(
            method: "GET",
            path: "/v1/conversations/\(conversationID)/events",
            token: token,
            query: query,
            as: TimelinePageResponse.self
        )
    }

    // --- transport -----------------------------------------------------------

    private func send<Response: Decodable>(
        method: String,
        path: String,
        body: [String: String?]? = nil,
        token: String? = nil,
        headers: [String: String] = [:],
        query: [URLQueryItem] = [],
        accepting: Set<Int> = [200, 201],
        as type: Response.Type
    ) async throws -> Response {
        guard let url = Self.url(path: path, query: query, relativeTo: baseURL) else {
            throw AgentClientError.invalidBaseURL
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = 20
        if let token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        for (name, value) in headers {
            request.setValue(value, forHTTPHeaderField: name)
        }
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(
                withJSONObject: body.mapValues { $0 as Any? ?? NSNull() }
            )
        }

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw AgentClientError.transport(String(describing: error))
        }
        guard let http = response as? HTTPURLResponse else {
            throw AgentClientError.malformedResponse
        }
        if accepting.contains(http.statusCode) {
            do {
                return try JSONDecoder().decode(Response.self, from: data)
            } catch {
                throw AgentClientError.malformedResponse
            }
        }
        throw AgentClientError.from(status: http.statusCode, body: data)
    }

    private static func url(
        path: String, query: [URLQueryItem], relativeTo base: URL
    ) -> URL? {
        guard let resolved = URL(string: path, relativeTo: base) else { return nil }
        guard !query.isEmpty else { return resolved }
        guard var components = URLComponents(url: resolved, resolvingAgainstBaseURL: true)
        else { return nil }
        components.queryItems = query
        return components.url
    }
}

// --- responses ---------------------------------------------------------------

public struct EnrolledDevice: Decodable, Sendable {
    public let deviceID: String
    public let displayName: String
    public let thumbprint: String
    public let scopes: [String]
    public let allowedToolsVersion: String

    private enum CodingKeys: String, CodingKey {
        case deviceID = "device_id"
        case displayName = "display_name"
        case thumbprint = "device_key_thumbprint"
        case scopes
        case allowedToolsVersion = "allowed_tools_version"
    }
}

public struct DeviceChallenge: Decodable, Sendable {
    public let challengeID: String
    public let deviceID: String
    public let nonce: String
    public let audience: String
    public let expiresAt: String

    private enum CodingKeys: String, CodingKey {
        case challengeID = "challenge_id"
        case deviceID = "device_id"
        case nonce
        case audience
        case expiresAt = "expires_at"
    }
}

public struct IssuedToken: Decodable, Sendable {
    public let accessToken: String
    public let tokenType: String
    public let expiresIn: Int
    public let deviceID: String
    public let scopes: [String]
    public let allowedToolsVersion: String

    private enum CodingKeys: String, CodingKey {
        case accessToken = "access_token"
        case tokenType = "token_type"
        case expiresIn = "expires_in"
        case deviceID = "device_id"
        case scopes
        case allowedToolsVersion = "allowed_tools_version"
    }
}

public struct Capabilities: Decodable, Sendable {
    public let allowedToolsVersion: String
    public let tools: [Tool]
    /// The one canonical Timeline every enrolled device resolves to
    /// (`CAP-001` design 4.2.2). It is **required**: the client has no other way
    /// to learn it and must never invent one, so a service that omits it fails
    /// here rather than at the first message.
    public let conversationID: String

    public struct Tool: Decodable, Sendable {
        public let alias: String
        public let summary: String?
        public let riskLevel: String?

        private enum CodingKeys: String, CodingKey {
            case alias
            case summary
            case riskLevel = "risk_level"
        }
    }

    private enum CodingKeys: String, CodingKey {
        case allowedToolsVersion = "allowed_tools_version"
        case tools
        case conversationID = "conversation_id"
    }
}

public struct DeviceList: Decodable, Sendable {
    public let devices: [DeviceSummary]
}

public struct DeviceSummary: Decodable, Sendable, Identifiable {
    public let deviceID: String
    public let displayName: String
    public let status: String
    public let isSelf: Bool
    public let hasPushToken: Bool
    public let allowedToolsVersion: String
    public let createdAt: String
    public let revokedAt: String?

    public var id: String { deviceID }

    private enum CodingKeys: String, CodingKey {
        case deviceID = "device_id"
        case displayName = "display_name"
        case status
        case isSelf = "is_self"
        case hasPushToken = "has_push_token"
        case allowedToolsVersion = "allowed_tools_version"
        case createdAt = "created_at"
        case revokedAt = "revoked_at"
    }
}

public struct PushTokenState: Decodable, Sendable {
    public let deviceID: String
    public let hasPushToken: Bool

    private enum CodingKeys: String, CodingKey {
        case deviceID = "device_id"
        case hasPushToken = "has_push_token"
    }
}

// --- errors ------------------------------------------------------------------

public enum AgentClientError: Error, Equatable {
    case invalidBaseURL
    /// The enrollment code was unknown, already used or expired.
    case enrollmentRejected
    /// This device cannot obtain a token: unknown, revoked, or the signature or
    /// challenge was refused. Never a reason to retry with the same challenge.
    case deviceRejected
    /// The presented access token was not accepted. One refresh is worth trying.
    case unauthenticated
    case forbidden(code: String?)
    case badRequest(code: String?)
    /// This `Idempotency-Key` is already bound to a different request. Retrying
    /// with the same key can never succeed, and minting a new one for the same
    /// intent would risk a second write.
    case idempotencyConflict
    /// The server does not know this id — a Timeline that names nothing here, or
    /// an operation this device does not own.
    case notFound(code: String?)
    case serverError(status: Int)
    case transport(String)
    case malformedResponse
    /// `direction=newer` needs a cursor: "everything newer than nothing" is the
    /// whole Timeline, which is not a page. Refused before the request.
    case cursorRequired

    /// The server's stable error code, where it sent one. Callers use it to tell
    /// `INVALID_CURSOR` (recoverable by reloading) from a plain bad request.
    public var errorCode: String? {
        switch self {
        case .forbidden(let code), .badRequest(let code), .notFound(let code):
            return code
        case .idempotencyConflict:
            return "IDEMPOTENCY_CONFLICT"
        default:
            return nil
        }
    }

    static func from(status: Int, body: Data) -> AgentClientError {
        let code = Self.errorCode(in: body)
        switch status {
        case 400: return .badRequest(code: code)
        case 401: return code == "DEVICE_AUTH_REJECTED" ? .deviceRejected : .unauthenticated
        case 403: return code == "ENROLLMENT_REJECTED" ? .enrollmentRejected : .forbidden(code: code)
        case 404: return .notFound(code: code)
        case 409: return .idempotencyConflict
        default: return .serverError(status: status)
        }
    }

    private static func errorCode(in body: Data) -> String? {
        guard
            let object = try? JSONSerialization.jsonObject(with: body) as? [String: Any],
            let error = object["error"] as? [String: Any],
            let code = error["code"] as? String
        else { return nil }
        return code
    }
}
