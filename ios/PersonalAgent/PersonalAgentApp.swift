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
                .task {
                    // The delegate is wired before `start` runs so iOS's
                    // remote-notification callbacks always have a coordinator
                    // to reach. `start` creates the coordinator once a session
                    // exists, because uploading a token needs a device id and
                    // an access token.
                    model.bindPushCoordinator(into: appDelegate)
                    await model.start()
                    await model.registerPushIfPermitted()
                }
        }
    }
}
