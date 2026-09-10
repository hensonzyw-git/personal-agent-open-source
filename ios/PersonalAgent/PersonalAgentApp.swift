import SwiftUI

/// `DEV-029`: the iPhone shell.
///
/// This build deliberately contains **only** device identity and service status.
/// Chat is `DEV-030` and the duplicate/review screens are `DEV-031`; shipping a
/// half-working chat box now would make it impossible to tell an enrollment
/// problem from a model problem during the first real rollout.
@main
struct PersonalAgentApp: App {
    #if !ACCEPTANCE
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @State private var model = AppModel()

    /// §13.1a: the all-day round-trip probe runs on the real device with the
    /// `--allday-probe` launch argument (Xcode scheme → Run → Arguments).
    /// It replaces the whole app surface for that launch — the probe must
    /// work before enrollment, so it never depends on a session.
    private let alldayProbe =
        ProcessInfo.processInfo.arguments.contains("--allday-probe")
    #endif

    var body: some Scene {
        WindowGroup {
            #if ACCEPTANCE
            // The isolated acceptance build. `AppModel` — and with it the
            // device session, the enrollment, the Keychain namespace and the
            // EventKit mirror engine — is never constructed on this path. The
            // two branches are compile-time siblings, not a run-time choice:
            // see `AcceptanceScene`.
            AcceptanceScene()
            #else
            if alldayProbe {
                AllDayProbeView()
            } else {
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
            #endif
        }
    }
}
