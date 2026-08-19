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
        // The icon badge is a lock-screen convenience, not a durable pending
        // counter: once the app is open, the ambient bar and the Timeline already
        // show what is outstanding, so the badge clears. Both APIs are used
        // because iOS can derive the badge from a still-pending notification and
        // `setBadgeCount` (iOS 16+) clears that path too.
        application.applicationIconBadgeNumber = 0
        UNUserNotificationCenter.current().setBadgeCount(0)
    }
}
