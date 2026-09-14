import Foundation

/// Opt-in acceptance storage. Production continues to use its existing store.
/// The public initializer can only select this app's private Application Support.
public final class AcceptanceFileStore: CredentialStore, @unchecked Sendable {
    private let root: URL
    private let lock = NSLock()
    private var closed = false
    private let beforeReplace: () throws -> Void
    private let fm = FileManager.default
    private static let keys: Set<String> = [
        CredentialKey.enrollment, CredentialKey.pendingChatSend,
        CredentialKey.pendingDuplicateDecisions, CredentialKey.pendingMediaSend,
        CredentialKey.claimedOverrideActions, CredentialKey.deviceKeyBlob,
        CredentialKey.deviceKeyKind, CredentialKey.deviceID, CredentialKey.baseURL,
        "acceptance-protection-probe"
    ]
    public enum Failure: Error { case invalidPath, closed, unexpectedFile }

    public convenience init() throws {
        let support = try FileManager.default.url(for: .applicationSupportDirectory,
            in: .userDomainMask, appropriateFor: nil, create: true)
        try self.init(testDirectory: support.appendingPathComponent("ADKAcceptance", isDirectory: true))
    }
    // Internal so tests can inspect a disposable directory; not a runtime path option.
    init(testDirectory: URL, beforeReplace: @escaping () throws -> Void = {}) throws {
        root = testDirectory
        self.beforeReplace = beforeReplace
        if fm.fileExists(atPath: root.path) {
            try validate(root, directory: true)
        } else {
            try fm.createDirectory(at: root, withIntermediateDirectories: false,
                                   attributes: [.posixPermissions: 0o700])
        }
        try protect(root)
    }
    private func validate(_ url: URL, directory: Bool = false) throws {
        let attrs = try fm.attributesOfItem(atPath: url.path)
        guard attrs[.type] as? FileAttributeType == (directory ? .typeDirectory : .typeRegular) else {
            throw Failure.invalidPath
        }
    }
    private func exists(_ url: URL) throws -> Bool {
        do { try validate(url); return true }
        catch let error as NSError where error.domain == NSCocoaErrorDomain && error.code == NSFileReadNoSuchFileError { return false }
    }
    private func protect(_ url: URL) throws {
        #if os(iOS)
        try fm.setAttributes([.protectionKey: FileProtectionType.complete], ofItemAtPath: url.path)
        #endif
        var item = url
        var values = URLResourceValues(); values.isExcludedFromBackup = true
        try item.setResourceValues(values)
    }
    private func path(_ key: String) throws -> URL {
        guard !closed else { throw Failure.closed }
        guard Self.keys.contains(key) else { throw Failure.invalidPath }
        try validate(root, directory: true)
        return root.appendingPathComponent(key)
    }
    public func read(_ key: String) throws -> Data? {
        lock.lock(); defer { lock.unlock() }
        let url = try path(key)
        do { try validate(url) }
        catch let error as NSError where error.domain == NSCocoaErrorDomain && error.code == NSFileReadNoSuchFileError {
            return nil
        }
        return try Data(contentsOf: url)
    }
    public func write(_ key: String, value: Data) throws {
        lock.lock(); defer { lock.unlock() }
        let destination = try path(key)
        _ = try exists(destination)
        let temporary = root.appendingPathComponent(".pending-" + UUID().uuidString)
        defer { try? fm.removeItem(at: temporary) }
        #if os(iOS)
        try value.write(to: temporary, options: [.withoutOverwriting, .completeFileProtection])
        #else
        try value.write(to: temporary, options: .withoutOverwriting)
        #endif
        try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: temporary.path)
        try protect(temporary)
        try beforeReplace()
        let fd = open(temporary.path, O_RDONLY | O_NOFOLLOW)
        guard fd >= 0 else { throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno)) }
        let synced = fsync(fd); close(fd)
        guard synced == 0 else { throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno)) }
        // Same-directory rename is atomic and preserves the protected inode.
        guard rename(temporary.path, destination.path) == 0 else { throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno)) }
    }
    public func delete(_ key: String) throws {
        lock.lock(); defer { lock.unlock() }
        let url = try path(key)
        if try exists(url) { try fm.removeItem(at: url)
        }
    }
    /// Synthetic bytes only, using the same replacement path as enrollment.
    public func prepareProtectionProbe() throws -> String {
        let key = "acceptance-protection-probe"
        try write(key, value: Data("before".utf8))
        try write(key, value: Data("after".utf8))
        lock.lock(); defer { lock.unlock() }
        let url = try path(key)
        let attrs = try fm.attributesOfItem(atPath: url.path)
        let excluded = try url.resourceValues(forKeys: [.isExcludedFromBackupKey]).isExcludedFromBackup == true
        let permissions = (attrs[.posixPermissions] as? NSNumber)?.intValue == 0o600
        #if os(iOS)
        let complete = attrs[.protectionKey] as? FileProtectionType == .complete
        #else
        let complete = false
        #endif
        return "替换后：complete=\(complete)，排除备份=\(excluded)，0600=\(permissions)"
    }
    public func readProtectionProbe() throws -> Bool {
        try read("acceptance-protection-probe") == Data("after".utf8)
    }
    /// Called only after the acceptance UI has drained its operations.
    /// Old references permanently refuse access, including after a new store is opened.
    public func closeAndClear() throws {
        lock.lock(); defer { lock.unlock() }
        closed = true
        try validate(root, directory: true)
        let files = try fm.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)
        for file in files {
            guard Self.keys.contains(file.lastPathComponent) || file.lastPathComponent.hasPrefix(".pending-") else { throw Failure.unexpectedFile }
            try validate(file)
        }
        for file in files { try fm.removeItem(at: file) }
    }
}
