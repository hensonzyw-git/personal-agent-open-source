import Foundation
import PersonalAgentKit
import UIKit
import UserNotifications

/// `DEV-040`: the Apple-facing half of push registration.
///
/// Deliberately thin. Everything that can be *wrong* — hex encoding, whether a
/// token needs sending — lives in `PersonalAgentKit.PushRegistration`, where
/// `swift test` reaches it. What is left here is the part that only exists
/// inside a running app: asking the user, asking iOS, and receiving the
/// callback.
///
/// Two behaviours worth stating because both are easy to get wrong:
///
/// - **it registers on every launch, not once.** iOS reissues a device token on
///   reinstall and on restore to a new device. A server holding the old one
///   still gets `200` from Apple while the push lands nowhere, which is the
///   worst shape of failure: silent and indistinguishable from "no reviews";
/// - **a refusal is remembered as a refusal, not retried.** If the user says no
///   to notifications, this stops. The review card is in the app either way; a
///   push is a convenience, and re-prompting is not one.
@MainActor
final class PushCoordinator: NSObject {
    /// What the server last confirmed it stored. Persisted so an unchanged
    /// token does not produce a request on every cold start.
    private static let lastConfirmedKey = "push.lastConfirmedToken"

    private let upload: (String) async throws -> Void
    private var pending: CheckedContinuation<Data, Error>?

    init(upload: @escaping (String) async throws -> Void) {
        self.upload = upload
        super.init()
    }

    enum PushError: Error {
        case notAuthorised
        case malformedToken
    }

    /// Ask, register, upload — or return without doing anything, quietly.
    ///
    /// Errors are swallowed by the caller on purpose: failing to register a
    /// push token must never block a launch or hide the chat surface behind an
    /// error, because everything the app does still works without it.
    func registerIfPermitted() async throws {
        let centre = UNUserNotificationCenter.current()
        let granted = try await centre.requestAuthorization(options: [
            .alert, .badge, .sound,
        ])
        guard granted else { throw PushError.notAuthorised }

        let raw = try await withCheckedThrowingContinuation { continuation in
            pending = continuation
            UIApplication.shared.registerForRemoteNotifications()
        }

        let token = PushRegistration.hexToken(from: raw)
        guard PushRegistration.isWellFormed(token) else {
            throw PushError.malformedToken
        }

        let lastConfirmed = UserDefaults.standard.string(
            forKey: Self.lastConfirmedKey
        )
        guard
            PushRegistration.shouldUpload(
                newToken: token, lastConfirmed: lastConfirmed
            )
        else { return }

        try await upload(token)
        // Recorded only after the server confirmed it, so a failed upload is
        // retried on the next launch rather than remembered as done.
        UserDefaults.standard.set(token, forKey: Self.lastConfirmedKey)
    }

    /// Called by the app delegate with iOS's answer.
    func didRegister(deviceToken: Data) {
        pending?.resume(returning: deviceToken)
        pending = nil
    }

    func didFailToRegister(error: Error) {
        pending?.resume(throwing: error)
        pending = nil
    }

    /// Forget what the server was told. Used when the device is revoked or
    /// re-enrolled, so the next launch re-registers rather than assuming the
    /// new device id already has this token.
    static func forgetConfirmedToken() {
        UserDefaults.standard.removeObject(forKey: lastConfirmedKey)
    }
}
