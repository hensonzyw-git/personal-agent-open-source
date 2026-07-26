import SwiftUI

/// `DEV-029`: the iPhone shell.
///
/// This build deliberately contains **only** device identity and service status.
/// Chat is `DEV-030` and the duplicate/review screens are `DEV-031`; shipping a
/// half-working chat box now would make it impossible to tell an enrollment
/// problem from a model problem during the first real rollout.
@main
struct PersonalAgentApp: App {
    @State private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            RootView(model: model)
                .task { await model.start() }
        }
    }
}
