#if ACCEPTANCE
import PersonalAgentKit
import SwiftUI

/// The isolated acceptance build's root — the whole of it, including the
/// composition that makes it isolated.
///
/// ## Why this file is behind `#if ACCEPTANCE`
///
/// The 2026-09-10 review would not release the ordinary App build for
/// installation: the cards it changed could only be checked by driving a real
/// conversation against the real backend, with the real calendar on the other
/// end of one of them. This build removes that dependency — but only if the
/// ordinary build cannot reach it.
///
/// Nothing here is a run-time switch. There is no launch argument, no
/// `UserDefaults` key, and no default that a shipped binary could fall into:
/// the condition is set by exactly one build configuration (`Acceptance`, which
/// the `PersonalAgent-Acceptance` scheme alone uses), and this file does not
/// exist in any other product. `ios/scripts/check_acceptance_isolation.sh`
/// asserts that from the outside, on the built products.
///
/// ## What it deliberately does not compose
///
/// The production root builds an `AppModel`, which owns a `DeviceSession`, an
/// enrollment, a Keychain namespace, and a `CalendarMirrorSyncEngine` over
/// `EventKitCalendarStore`. **None of those is constructed here.** There is no
/// session to enroll, no token to leak, no mirror to upload, and — the one that
/// matters most — no `EventKitCalendarStore` for a device action to reach. The
/// credential store is in-memory, so this build touches no Keychain item the
/// ordinary app also uses, and the archive is a file in its own container.
///
/// The device-action executor is `AcceptanceDeviceExecutor`, which answers from
/// a fixed map and never opens EventKit. That is belt-and-braces: the seeded
/// archive hands out no actions at all (`deviceActions` is empty on every
/// seeded receipt), so the composition is the second line and the fixture is
/// the first.
///
/// ## What a person sees, and what that does and does not prove
///
/// The cards are drawn by the **shipping** `ChatView` over the **shipping**
/// `TimelineEvent` / `OperationReceipt` decoders, from the fixed script in
/// `AcceptanceScenario`. So a card that is wrong here is wrong in production
/// too — that is the point of the build and the whole of its claim.
///
/// It is not a claim about the server. Nothing here validates policy, model
/// behaviour, idempotency, or a real calendar, and the walk-through in
/// `docs/Calendar验收构建与清单_v0.1.md` is written in those terms.
struct AcceptanceScene: View {
    @State private var timeline: AcceptanceTimeline?
    @State private var model: ChatModel?
    @State private var failure: String?
    @State private var showingChecklist = true
    @State private var working = false

    var body: some View {
        NavigationStack {
            Group {
                if let model {
                    ChatView(model: model, review: nil)
                } else if let failure {
                    ContentUnavailableView {
                        Label("验收构建无法启动", systemImage: "exclamationmark.triangle")
                    } description: {
                        Text(failure)
                    }
                } else {
                    ProgressView("正在装载验收脚本…")
                }
            }
            .navigationTitle("验收")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("清单") { showingChecklist = true }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button("重置") { Task { await reset() } }
                        .disabled(model == nil || working)
                }
            }
        }
        .tint(.accentBrand)
        .sheet(isPresented: $showingChecklist) {
            AcceptanceChecklistView(items: AcceptanceChecklist.items)
        }
        .task { await start() }
    }

    /// Build the isolated composition once, then open the seeded Timeline.
    ///
    /// Order matters: `open` is `ChatModel`'s own entry point, so the cards are
    /// produced by the path the ordinary app uses rather than by anything
    /// written for this build.
    private func start() async {
        guard model == nil, failure == nil else { return }
        do {
            let location = try AcceptanceArchiveLocation.applicationSupport()
            let backend = AcceptanceTimeline(location: location)
            let chat = ChatTimeline(
                backend: backend,
                // In-memory on purpose: this build must not read or write any
                // Keychain item the ordinary app also uses, and a pending-send
                // slot that survived a relaunch here would be a slot for an
                // operation that never existed on any server.
                store: InMemoryCredentialStore(),
                deviceActionExecutor: AcceptanceDeviceExecutor()
            )
            let chatModel = ChatModel(
                timeline: chat, mediaBackend: backend, store: InMemoryCredentialStore()
            ) { $0.localizedDescription }
            timeline = backend
            model = chatModel
            await chatModel.open(conversationID: AcceptanceScenario.conversationID)
        } catch {
            failure = error.localizedDescription
        }
    }

    /// Re-seed the archive and re-open it.
    ///
    /// The reset rewrites the file, so the reload goes through the same
    /// `loadLatest` path a cold launch does — a reset that only reset the
    /// in-memory copy would leave the file holding whatever the last run
    /// appended, and the next launch would load that instead.
    private func reset() async {
        guard let timeline, let model else { return }
        working = true
        defer { working = false }
        do {
            try await timeline.resetToSeed()
            await model.open(conversationID: AcceptanceScenario.conversationID)
        } catch {
            failure = error.localizedDescription
        }
    }
}

/// The walk-through, on the phone, next to the cards it describes.
///
/// The same list `docs/Calendar验收构建与清单_v0.1.md` carries, and the same
/// list `AcceptanceScenarioTests` holds against the seeded script — one list in
/// three places would drift, so the document is generated from this one and the
/// test fails if an item names an event the script does not produce.
struct AcceptanceChecklistView: View {
    let items: [AcceptanceChecklistItem]
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Text(
                        "这是一个不连服务器、不读日历、不含模型的构建。"
                            + "下面的每一条都写在卡片上，请照着看。"
                    )
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                }
                ForEach(items) { item in
                    VStack(alignment: .leading, spacing: 6) {
                        Text(item.title).font(.callout.weight(.medium))
                        Text(item.pass).font(.footnote)
                        if let identifier = item.expectedIdentifier {
                            // Printed rather than left to the prose: the person
                            // is comparing this value against what the card
                            // shows, and a value quoted in a sentence is one
                            // transcription away from being unchecked.
                            Text("标识符　\(identifier)")
                                .font(.footnote.monospaced())
                                .foregroundStyle(.secondary)
                        }
                    }
                    .padding(.vertical, 4)
                }
            }
            .navigationTitle("验收清单")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("开始") { dismiss() }
                }
            }
        }
    }
}
#endif
