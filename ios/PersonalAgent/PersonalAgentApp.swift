import SwiftUI

/// `DEV-029`: the iPhone shell.
///
/// This build deliberately contains **only** device identity and service status.
/// Chat is `DEV-030` and the duplicate/review screens are `DEV-031`; shipping a
/// half-working chat box now would make it impossible to tell an enrollment
/// problem from a model problem during the first real rollout.
@main
struct PersonalAgentApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @State private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            RootView(model: model)
                // The project predates its asset catalogue, so
                // `ASSETCATALOG_COMPILER_GLOBAL_ACCENT_COLOR_NAME` was never set and
                // an `AccentColor` asset alone does not become the global tint --
                // every stock control would keep rendering in system blue against a
                // design that has exactly one accent (§7). Setting it here keeps the
                // decision in the source rather than in a build setting.
                .tint(.accentBrand)
                .task {
                    // The delegate is wired before `start` runs so iOS's
                    // remote-notification callbacks always have a coordinator
                    // to reach. `start` creates the coordinator once a session
                    // exists, because uploading a token needs a device id and
                    // an access token.
                    model.bindPushCoordinator(into: appDelegate)
                    // Cleared here in addition to `applicationDidBecomeActive`:
                    // a cold launch into the badge state must clear it even if
                    // the delegate callback is the one thing that did not run.
                    AppDelegate.clearBadge()
                    await model.start()
                    await model.registerPushIfPermitted()
                }
        }
    }
}
