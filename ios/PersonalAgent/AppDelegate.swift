import UIKit

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
}
