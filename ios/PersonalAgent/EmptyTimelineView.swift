import PersonalAgentKit
import SwiftUI

/// §1c: what the chat screen shows before the Timeline has anything in it.
///
/// It is deliberately a plain value-in, closure-out view rather than something that
/// reads `ChatModel`. The empty state depends on nothing but the granted tools, and
/// keeping it that way is the only reason it can be previewed at all: on a device
/// that has been in use for weeks this screen is unreachable, so a version wired to
/// the live model could never be looked at before shipping.
struct EmptyTimelineView: View {
    /// The server's answer from `/v1/capabilities`. Never a list compiled here.
    let tools: [Capabilities.Tool]
    /// Fills the composer. §1c's chips do not send: 记一笔 is not a complete
    /// instruction without an amount, and one tap that writes to a real ledger is
    /// the wrong default on a surface whose contract is that writes are reviewable
    /// before they happen.
    let onPickPrompt: (String) -> Void

    /// Domains in the product plan that have no tools yet. Drawn greyed out on
    /// purpose: a later domain should arrive as a row turning live, not as a new
    /// navigation level (§4 C1).
    private static let plannedDomains = ["知识库", "健康", "衣橱"]
    private static let quickPrompts = ["记一笔", "查本月支出"]
    private static let domainNames = ["finance": "财务"]

    /// Which domains to show and what belongs to each is decided by
    /// `Capabilities.userFacingDomains` in `PersonalAgentKit`, where it is covered by
    /// tests. Those are safety rules — an omitted grant is invisible to the reader —
    /// and they do not belong in a layer with no test target.
    ///
    /// What stays here is the only genuinely presentational part: turning a raw
    /// alias domain into a name. An unnamed domain keeps its raw prefix, so a new
    /// one shows up as an odd-looking row rather than not at all.
    private var groups: [CapabilityDomain] {
        Capabilities.userFacingDomains(from: tools)
    }

    private func displayName(_ domain: CapabilityDomain) -> String {
        Self.domainNames[domain.id] ?? domain.id
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            VStack(alignment: .leading, spacing: 8) {
                Text("说一句话就行。")
                    .font(.title2.weight(.semibold))
                Text("缺字段我会问你。写入前不会替你猜归属，写完给你可核验的回执。")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }

            // Omitted entirely before the first capabilities read. An unread set is
            // not an empty one, and 「现在能做的：（空）」 would be a false statement
            // about a device that may well have every tool granted.
            if !groups.isEmpty {
                VStack(alignment: .leading, spacing: 12) {
                    Text("现在能做的")
                        .font(.footnote.weight(.medium))
                        .foregroundStyle(.secondary)
                    ForEach(groups) { group in
                        row(
                            displayName(group),
                            group.entries.joined(separator: " · "),
                            available: true
                        )
                    }
                    ForEach(Self.plannedDomains, id: \.self) { name in
                        row(name, nil, available: false)
                    }
                }
            }

            HStack(spacing: 8) {
                ForEach(Self.quickPrompts, id: \.self) { prompt in
                    Button(prompt) { onPickPrompt(prompt) }
                        .font(.footnote)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 8)
                        .background(Color.surface)
                        .foregroundStyle(.accentText)
                        .clipShape(Capsule())
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, 28)
    }

    private func row(_ name: String, _ detail: String?, available: Bool) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(name)
                .font(.callout.weight(available ? .medium : .regular))
                .foregroundStyle(available ? Color.ink : Color.secondary)
            if let detail {
                Text(detail)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
            if !available {
                Text("未开放")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 2)
        .opacity(available ? 1 : 0.55)
    }
}

// MARK: - Previews

/// Synthetic tools, built by decoding the real wire shape rather than by calling a
/// memberwise initialiser. Two reasons: `Capabilities.Tool`'s synthesised init is
/// internal to the package and unreachable here, and going through the actual
/// decoder means preview data that drifts from the wire contract fails instead of
/// quietly previewing something the service could never send.
///
/// The aliases follow the service's shape; the summaries are placeholders, not its
/// real wording.
private func decodeTools(_ json: String) -> [Capabilities.Tool] {
    (try? JSONDecoder().decode(
        [Capabilities.Tool].self, from: Data(json.utf8)
    )) ?? []
}

private let previewTools = decodeTools("""
[
  {"alias": "finance.log_expense",  "summary": "记一笔支出",       "risk_level": "R2"},
  {"alias": "finance.log_income",   "summary": "记一笔收入",       "risk_level": "R2"},
  {"alias": "finance.query_expense","summary": "按时间与分类查账", "risk_level": "R1"},
  {"alias": "meta.capabilities",    "summary": "读取能力清单",     "risk_level": "R0"}
]
""")

#Preview("空态 · 浅色") {
    EmptyTimelineView(tools: previewTools) { _ in }
        .padding()
        .background(Color.screenBackground)
}

#Preview("空态 · 深色") {
    EmptyTimelineView(tools: previewTools) { _ in }
        .padding()
        .background(Color.screenBackground)
        .preferredColorScheme(.dark)
}

/// The pre-first-read case: 现在能做的 must be absent, not empty.
#Preview("空态 · 能力未读到") {
    EmptyTimelineView(tools: []) { _ in }
        .padding()
        .background(Color.screenBackground)
}

/// An unknown domain keeps its raw prefix instead of vanishing.
#Preview("空态 · 未知域") {
    EmptyTimelineView(
        tools: previewTools + decodeTools("""
        [{"alias": "wardrobe.suggest_outfit", "summary": "推荐穿搭", "risk_level": "R1"}]
        """)
    ) { _ in }
        .padding()
        .background(Color.screenBackground)
}
