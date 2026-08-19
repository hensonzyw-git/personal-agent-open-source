import UIKit
import UserNotifications

/// `DEV-040`: the bridge between iOS's remote-notification callbacks and
/// `PushCoordinator`.
///
/// SwiftUI's `App` lifecycle does not receive `application(_:didRegister...)`,
/// so an `UIApplicationDelegateAdaptor` is the one place those callbacks land.
/// The coordinator itself lives on the app model, where it can reach the
/// device session; this delegate only forwards the bytes iOS hands back.
final class AppDelegate: NSObject, UIApplicationDelegate {
    /// Set by `AppModel` once the coordinator exists, so the callbacks iOS
    /// fires during `registerForRemoteNotifications` reach it.
    var pushCoordinator: PushCoordinator?

    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        pushCoordinator?.didRegister(deviceToken: deviceToken)
    }

    func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        pushCoordinator?.didFailToRegister(error: error)
    }

    func applicationDidBecomeActive(_ application: UIApplication) {
        Self.clearBadge()
    }

    /// Clear the icon badge through every path iOS can derive it from.
    ///
    /// `UIApplication.applicationIconBadgeNumber` is deprecated and its setter
    /// is a no-op on modern iOS (the 2026-08-20 probe read the badge back as 1
    /// after assigning 0); `UNUserNotificationCenter.setBadgeCount(_:)` is the
    /// API that actually clears it. The delivered notification is removed too so
    /// iOS cannot re-derive the badge from it. Called on
    /// `applicationDidBecomeActive` and again from the SwiftUI `.task`, because
    /// neither alone has proven enough.
    static func clearBadge() {
        UIApplication.shared.applicationIconBadgeNumber = 0
        let centre = UNUserNotificationCenter.current()
        centre.setBadgeCount(0)
        centre.removeAllDeliveredNotifications()
    }
}
