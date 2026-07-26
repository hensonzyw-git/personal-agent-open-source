import Foundation

/// Enrollment, token refresh and the authenticated calls of `DEV-029`.
///
/// Refresh is coalesced through a single in-flight `Task`, because two screens
/// refreshing at once would otherwise each mint a challenge and each spend one.
/// Being an `actor` is **not** enough for that on its own: an actor method that
/// awaits the network suspends and lets the next caller straight past the cache
/// check, so eight concurrent calls produced eight challenges until the in-flight
/// task was added. A test pins the count at one.
///
/// What it refuses to do is as important as what it does:
///
/// - it **never signs a challenge that is not for this device**, and never one
///   whose audience is not the fixed one. A challenge arrives from the network,
///   so it is input, not instruction;
/// - it retries an unauthenticated call **once**, with one forced refresh. A
///   second refusal propagates, because a loop against a revoked device is how a
///   client burns a battery and hides the real state;
/// - it never deletes the device key on its own. A revoked device is reported, so
///   Henson can see it and re-enroll deliberately; wiping the key would turn a
///   server-side revocation into a silent local reset.
public actor DeviceSession {
    private struct StoredEnrollment: Codable {
        let deviceKeyBlob: Data
        let deviceKeyKind: String
        let deviceID: String
        let baseURL: String
    }

    public enum State: Equatable, Sendable {
        case notEnrolled
        case enrolled(deviceID: String)
        /// The server refuses to issue tokens for this device.
        case rejected(deviceID: String)
    }

    private let client: AgentClient
    private let store: CredentialStore
    private let identityFactory: DeviceIdentityFactory
    private let now: @Sendable () -> Date
    /// Refresh this long before the token actually expires, so a call started at
    /// the edge of the window does not arrive expired.
    private let refreshMargin: TimeInterval

    private var identity: (any DeviceIdentity)?
    private var deviceID: String?
    private var keyKind: DeviceKeyKind?
    private var token: (value: String, expiresAt: Date, scopes: [String], toolsVersion: String)?
    private var rejected = false
    /// The one refresh in flight, if any. Concurrent callers await it instead of
    /// minting a second challenge that would spend the first one.
    private var refreshTask: Task<String, Error>?

    public init(
        client: AgentClient,
        store: CredentialStore,
        identityFactory: DeviceIdentityFactory = DeviceIdentityFactory(),
        refreshMargin: TimeInterval = 60,
        now: @escaping @Sendable () -> Date = { Date() }
    ) {
        self.client = client
        self.store = store
        self.identityFactory = identityFactory
        self.refreshMargin = refreshMargin
        self.now = now
    }

    public var state: State {
        guard let deviceID else { return .notEnrolled }
        return rejected ? .rejected(deviceID: deviceID) : .enrolled(deviceID: deviceID)
    }

    public var deviceKeyKind: DeviceKeyKind? { keyKind }

    /// Read the service URL before a session exists. The current envelope is
    /// preferred; the separate legacy item keeps the first simulator enrollment
    /// recoverable long enough to migrate it in `restore()`.
    public static func storedBaseURL(in store: CredentialStore) throws -> URL? {
        if let data = try store.read(CredentialKey.enrollment) {
            let stored = try decodeStoredEnrollment(data)
            return URL(string: stored.baseURL)
        }
        guard
            let data = try store.read(CredentialKey.baseURL),
            let text = String(data: data, encoding: .utf8)
        else {
            return nil
        }
        return URL(string: text)
    }

    /// Reopen a previous enrollment from the Keychain. Returns the state it found.
    @discardableResult
    public func restore() throws -> State {
        if let data = try store.read(CredentialKey.enrollment) {
            let stored = try Self.decodeStoredEnrollment(data)
            return try restore(stored)
        }

        // Migrate the first DEV-029 simulator format. The new envelope is written
        // before any legacy item is touched, so a failed migration leaves the
        // last known-good enrollment recoverable.
        guard
            let blob = try store.read(CredentialKey.deviceKeyBlob),
            let idData = try store.read(CredentialKey.deviceID),
            let kindData = try store.read(CredentialKey.deviceKeyKind),
            let urlData = try store.read(CredentialKey.baseURL),
            let id = String(data: idData, encoding: .utf8),
            let kindName = String(data: kindData, encoding: .utf8),
            let url = String(data: urlData, encoding: .utf8)
        else {
            return .notEnrolled
        }
        let migrated = StoredEnrollment(
            deviceKeyBlob: blob,
            deviceKeyKind: kindName,
            deviceID: id,
            baseURL: url
        )
        try store.write(
            CredentialKey.enrollment,
            value: try JSONEncoder().encode(migrated)
        )
        return try restore(migrated)
    }

    /// Claim a one-time enrollment code with a freshly generated device key.
    ///
    /// Nothing is persisted until the server has accepted the key, so a refused
    /// code leaves no half-enrolled state behind.
    @discardableResult
    public func enroll(code: String, displayName: String) async throws -> EnrolledDevice {
        let created = try identityFactory.create()
        let enrolled = try await client.claimEnrollment(
            code: code,
            publicKeyB64U: created.identity.publicKeyB64U,
            displayName: displayName
        )
        let stored = StoredEnrollment(
            deviceKeyBlob: created.blob,
            deviceKeyKind: created.kind.rawValue,
            deviceID: enrolled.deviceID,
            baseURL: client.baseURL.absoluteString
        )
        do {
            try store.write(
                CredentialKey.enrollment,
                value: try JSONEncoder().encode(stored)
            )
        } catch {
            // The one-time code has already been consumed and the server device
            // is active. Use the still-live key in memory to revoke it instead of
            // leaving an orphan carrying whatever scopes enrollment granted.
            let revoked = await revokeAfterPersistenceFailure(
                identity: created.identity,
                deviceID: enrolled.deviceID
            )
            throw DeviceSessionError.localPersistenceFailed(
                deviceID: enrolled.deviceID,
                serverDeviceRevoked: revoked
            )
        }
        identity = created.identity
        keyKind = created.kind
        deviceID = enrolled.deviceID
        token = nil
        rejected = false
        return enrolled
    }

    /// Forget this enrollment locally. Only ever called because the user asked:
    /// the server-side revocation is a separate, explicit action.
    public func forgetLocally() throws {
        for key in [
            CredentialKey.deviceKeyBlob,
            CredentialKey.deviceKeyKind,
            CredentialKey.deviceID,
            CredentialKey.baseURL,
            // Delete the authoritative envelope last. If removing a legacy item
            // fails, the complete enrollment remains recoverable.
            CredentialKey.enrollment,
        ] {
            try store.delete(key)
        }
        identity = nil
        keyKind = nil
        deviceID = nil
        token = nil
        rejected = false
    }

    // --- authenticated calls -------------------------------------------------

    public func capabilities() async throws -> Capabilities {
        try await authorized { try await self.client.capabilities(token: $0) }
    }

    public func devices() async throws -> DeviceList {
        try await authorized { try await self.client.devices(token: $0) }
    }

    /// Revoke *this* device server side. The key stays until the user chooses to
    /// forget it, so the app can still show why it stopped working.
    public func revokeSelf() async throws -> DeviceSummary {
        guard let deviceID else { throw DeviceSessionError.notEnrolled }
        let summary = try await authorized {
            try await self.client.revokeDevice(deviceID: deviceID, token: $0)
        }
        token = nil
        rejected = true
        return summary
    }

    private func authorized<T>(_ call: (String) async throws -> T) async throws -> T {
        let first = try await accessToken(forceRefresh: false)
        do {
            return try await call(first)
        } catch AgentClientError.unauthenticated {
            // Exactly one retry, on a token minted after the refusal.
            let second = try await accessToken(forceRefresh: true)
            return try await call(second)
        }
    }

    /// The current token, refreshed if it is missing, expiring or forced.
    public func accessToken(forceRefresh: Bool = false) async throws -> String {
        if rejected { throw AgentClientError.deviceRejected }
        guard identity != nil, deviceID != nil else { throw DeviceSessionError.notEnrolled }
        if !forceRefresh, let token, token.expiresAt > now() {
            return token.value
        }
        // A forced refresh joins an in-flight one rather than racing it: that
        // refresh mints its own fresh challenge, so its token is not the one that
        // was just refused. If it still is, the retry fails and propagates -- one
        // retry, never a loop.
        if let existing = refreshTask {
            return try await existing.value
        }
        let task = Task { try await self.mintToken() }
        refreshTask = task
        defer { refreshTask = nil }
        return try await task.value
    }

    private func mintToken() async throws -> String {
        guard let identity, let deviceID else { throw DeviceSessionError.notEnrolled }

        let challenge: DeviceChallenge
        do {
            challenge = try await client.requestChallenge(deviceID: deviceID)
        } catch AgentClientError.deviceRejected {
            rejected = true
            throw AgentClientError.deviceRejected
        }
        // The challenge came off the network: check it names this device and the
        // audience this app is built for before signing anything with it.
        guard challenge.deviceID == deviceID else {
            throw DeviceSessionError.challengeNotForThisDevice
        }
        guard challenge.audience == DeviceWireContract.audience else {
            throw DeviceSessionError.unexpectedAudience(challenge.audience)
        }
        guard
            let nonce = DeviceWireContract.base64URLDecode(challenge.nonce),
            nonce.count == DeviceWireContract.nonceByteCount
        else {
            throw DeviceSessionError.malformedNonce
        }

        let message = try DeviceWireContract.signingInput(
            challengeID: challenge.challengeID,
            nonceB64U: challenge.nonce,
            deviceID: deviceID
        )
        let signature = try identity.signature(for: message)
        guard signature.count == DeviceWireContract.joseSignatureByteCount else {
            throw WireContractError.malformedSignature
        }

        let issued: IssuedToken
        do {
            issued = try await client.requestToken(
                challenge: challenge,
                signatureB64U: DeviceWireContract.base64URLEncode(signature)
            )
        } catch AgentClientError.deviceRejected {
            rejected = true
            throw AgentClientError.deviceRejected
        }
        guard issued.deviceID == deviceID, !issued.accessToken.isEmpty else {
            throw AgentClientError.malformedResponse
        }
        // The expiry is computed from *this* device's clock and the server's
        // `expires_in`, so a wrong local clock can shorten the cached lifetime but
        // never extend it past what the server granted.
        let lifetime = max(0, TimeInterval(issued.expiresIn) - refreshMargin)
        token = (
            value: issued.accessToken,
            expiresAt: now().addingTimeInterval(lifetime),
            scopes: issued.scopes,
            toolsVersion: issued.allowedToolsVersion
        )
        rejected = false
        return issued.accessToken
    }

    public var currentScopes: [String] { token?.scopes ?? [] }
    public var currentToolsVersion: String? { token?.toolsVersion }

    private func restore(_ stored: StoredEnrollment) throws -> State {
        guard
            !stored.deviceID.isEmpty,
            let kind = DeviceKeyKind(rawValue: stored.deviceKeyKind),
            let storedURL = URL(string: stored.baseURL),
            storedURL.scheme == "http" || storedURL.scheme == "https"
        else {
            throw DeviceSessionError.storedEnrollmentMalformed
        }
        identity = try identityFactory.reopen(blob: stored.deviceKeyBlob, kind: kind)
        deviceID = stored.deviceID
        keyKind = kind
        return .enrolled(deviceID: stored.deviceID)
    }

    private static func decodeStoredEnrollment(_ data: Data) throws -> StoredEnrollment {
        do {
            return try JSONDecoder().decode(StoredEnrollment.self, from: data)
        } catch {
            throw DeviceSessionError.storedEnrollmentMalformed
        }
    }

    private func revokeAfterPersistenceFailure(
        identity: any DeviceIdentity,
        deviceID: String
    ) async -> Bool {
        do {
            let challenge = try await client.requestChallenge(deviceID: deviceID)
            guard
                challenge.deviceID == deviceID,
                challenge.audience == DeviceWireContract.audience,
                let nonce = DeviceWireContract.base64URLDecode(challenge.nonce),
                nonce.count == DeviceWireContract.nonceByteCount
            else {
                return false
            }
            let message = try DeviceWireContract.signingInput(
                challengeID: challenge.challengeID,
                nonceB64U: challenge.nonce,
                deviceID: deviceID
            )
            let signature = try identity.signature(for: message)
            guard signature.count == DeviceWireContract.joseSignatureByteCount else {
                return false
            }
            let issued = try await client.requestToken(
                challenge: challenge,
                signatureB64U: DeviceWireContract.base64URLEncode(signature)
            )
            guard issued.deviceID == deviceID, !issued.accessToken.isEmpty else {
                return false
            }
            let revoked = try await client.revokeDevice(
                deviceID: deviceID,
                token: issued.accessToken
            )
            return revoked.deviceID == deviceID && revoked.status == "revoked"
        } catch {
            return false
        }
    }
}

public enum DeviceSessionError: Error, Equatable {
    case notEnrolled
    case challengeNotForThisDevice
    case unexpectedAudience(String)
    case malformedNonce
    case storedEnrollmentMalformed
    case localPersistenceFailed(deviceID: String, serverDeviceRevoked: Bool)
}
