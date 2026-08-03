import Foundation

/// `DEV-040`: turning an APNs device token into a server registration.
///
/// The Apple-facing half — asking for authorisation and calling
/// `registerForRemoteNotifications` — lives in the app shell, because it cannot
/// run headlessly. Everything that can be wrong lives here, where `swift test`
/// reaches it: how the raw token becomes the hex string the server stores, and
/// when it is worth sending at all.
///
/// The registration is deliberately **not** treated as a one-time step. iOS
/// reissues a device token on reinstall, on restore to a new device, and
/// occasionally for its own reasons, and a token the server holds after that is
/// silently dead — pushes are accepted by Apple and land nowhere. So the app
/// registers on every launch and this type decides whether anything needs to be
/// sent.
public enum PushRegistration {

    /// APNs hands back opaque bytes; design 5.1's route carries lowercase hex.
    ///
    /// `String(describing:)` on `Data` produces `32 bytes` and `.description`
    /// is no better — a mistake worth naming here, because the result looks
    /// plausible in a log and is rejected by Apple much later.
    public static func hexToken(from raw: Data) -> String {
        raw.map { String(format: "%02x", $0) }.joined()
    }

    /// Whether to send this token, given what was last confirmed to the server.
    ///
    /// Sending an unchanged token on every launch would be harmless but noisy;
    /// *not* sending a changed one is the actual failure, so anything other
    /// than an exact match sends.
    public static func shouldUpload(
        newToken: String, lastConfirmed: String?
    ) -> Bool {
        guard let lastConfirmed, !lastConfirmed.isEmpty else { return true }
        return newToken != lastConfirmed
    }

    /// A token the server would refuse, rejected before a request is spent.
    ///
    /// Apple's tokens are 32 bytes today and 100+ on some estates, so the
    /// length is not pinned — only that it is non-empty, even-length lowercase
    /// hex, which is what the wire contract says.
    public static func isWellFormed(_ token: String) -> Bool {
        guard !token.isEmpty, token.count % 2 == 0 else { return false }
        return token.allSatisfy { $0.isHexDigit && !$0.isUppercase }
    }
}
