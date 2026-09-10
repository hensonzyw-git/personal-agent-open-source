import Foundation

#if canImport(Security)
import Security
#endif

/// Where the app keeps one enrollment envelope: the Secure Enclave key blob,
/// the `device_id` the server assigned, and the service URL.
///
/// None of it is a long-lived credential for Feishu, GLM or the infrastructure --
/// the iPhone is a thin client and design 5.1 keeps it that way. The key blob is
/// still device-bound and the `device_id` still names this installation, so both
/// live in the Keychain with `WhenUnlockedThisDeviceOnly` accessibility and never
/// in `UserDefaults`.
public protocol CredentialStore: Sendable {
    func read(_ key: String) throws -> Data?
    func write(_ key: String, value: Data) throws
    func delete(_ key: String) throws
}

public enum CredentialStoreError: Error {
    case osStatus(String, Int32)
}

public enum CredentialKey {
    /// One atomically replaceable envelope containing everything required to
    /// reopen an enrollment. Keeping these fields in separate Keychain items
    /// allowed a write failure to leave a new server device paired with only
    /// part of its local identity.
    public static let enrollment = "device-enrollment-v1"
    /// `DEV-030`: the one unfinished chat message, if any. It holds the
    /// idempotency key that makes a reconnect resume the same operation instead of
    /// starting a second write, so it belongs in the same durable store as the
    /// device identity rather than in `UserDefaults`.
    public static let pendingChatSend = "pending-chat-send-v1"
    /// `DEV-031`: the duplicate decisions whose reply was never confirmed. Each
    /// holds the idempotency key that makes a retry replay the server's recorded
    /// outcome instead of starting a second decision, so they belong in the same
    /// durable store as the chat send slot.
    public static let pendingDuplicateDecisions = "pending-duplicate-decisions-v1"
    /// The device actions a 「仍要创建」 override has already claimed. The server
    /// derives *one* operation per override, so the second tap of a double tap
    /// reads the same parked projection back and is handed the same action —
    /// and an override action carries `skip_local_dedup`, which switches off the
    /// only other thing that could refuse the second write. This marker is what
    /// keeps that to one event; it has to outlive the report, because the
    /// dangerous tap is the one that arrives while the first report is in
    /// flight.
    public static let claimedOverrideActions = "claimed-override-actions-v1"
    /// The instant the device last completed a *whole* calendar mirror window
    /// (the review-R5 engine's marker). It decides staleness on the client and
    /// lives in the same durable store so a restart neither re-syncs for
    /// nothing nor believes a sync that never finished.
    public static let calendarMirrorSyncedAt = "calendar-mirror-synced-at-v1"
    /// Design §9.1's change sequence: how much the local calendar has moved
    /// since this device last completed a whole mirror window, and how much of
    /// that movement has been uploaded.
    ///
    /// Two monotonic counters rather than the obvious dirty boolean, because a
    /// boolean has a race the counters do not (review R3-F13): a pass that
    /// captured the world at sequence 7 and then watched changes 8 and 9 arrive
    /// mid-upload must not clear a flag on success — it covered 7, and 8 and 9
    /// are still owed. `lastChangeSeq > syncedChangeSeq` is the same predicate
    /// with no "when exactly do I clear this" question to get wrong, and it
    /// survives a restart because both numbers are on disk.
    public static let calendarChangeSeq = "calendar-change-seq-v1"
    public static let calendarSyncedChangeSeq = "calendar-synced-change-seq-v1"
    /// Legacy keys from the first DEV-029 simulator build. `DeviceSession`
    /// migrates them on read and deletes them when the user forgets locally.
    public static let deviceKeyBlob = "device-key-blob"
    public static let deviceKeyKind = "device-key-kind"
    public static let deviceID = "device-id"
    public static let baseURL = "service-base-url"
}

#if canImport(Security)
/// The real store. `kSecAttrAccessibleWhenUnlockedThisDeviceOnly` is design 4.1's
/// requirement: nothing here should survive into a backup or another device.
public struct KeychainCredentialStore: CredentialStore {
    private let service: String

    public init(service: String = "org.example.PersonalAgent") {
        self.service = service
    }

    private func query(_ key: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
        ]
    }

    public func read(_ key: String) throws -> Data? {
        var request = query(key)
        request[kSecReturnData as String] = true
        request[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        let status = SecItemCopyMatching(request as CFDictionary, &item)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = item as? Data else {
            throw CredentialStoreError.osStatus("read \(key)", status)
        }
        return data
    }

    public func write(_ key: String, value: Data) throws {
        // Replace an existing item in place. Delete-then-add creates a gap: if
        // the add fails, the last known-good enrollment has already been lost.
        // SecItemUpdate is atomic from this client's point of view and preserves
        // the old value on failure.
        let updated = SecItemUpdate(
            query(key) as CFDictionary,
            [kSecValueData as String: value] as CFDictionary
        )
        if updated == errSecSuccess { return }
        guard updated == errSecItemNotFound else {
            throw CredentialStoreError.osStatus("update \(key)", updated)
        }

        var request = query(key)
        request[kSecValueData as String] = value
        request[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
        let status = SecItemAdd(request as CFDictionary, nil)
        guard status == errSecSuccess else {
            throw CredentialStoreError.osStatus("write \(key)", status)
        }
    }

    public func delete(_ key: String) throws {
        let status = SecItemDelete(query(key) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw CredentialStoreError.osStatus("delete \(key)", status)
        }
    }
}
#endif

/// For tests and for `swift test` on a machine with no usable keychain.
public final class InMemoryCredentialStore: CredentialStore, @unchecked Sendable {
    private let lock = NSLock()
    private var storage: [String: Data] = [:]

    public init(_ initial: [String: Data] = [:]) {
        storage = initial
    }

    public func read(_ key: String) throws -> Data? {
        lock.withLock { storage[key] }
    }

    public func write(_ key: String, value: Data) throws {
        lock.withLock { storage[key] = value }
    }

    public func delete(_ key: String) throws {
        lock.withLock { storage[key] = nil }
    }
}
