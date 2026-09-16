import EventKit
import SwiftUI

/// §13.1a — the EventKit all-day round-trip probe (one-shot instrument).
///
/// The approved design (`docs/Calendar域技术方案_v1.0.md` §3.2) freezes the
/// mirror's all-day read-back algorithm on this probe's outcome: whether
/// EventKit preserves the absolute instant a Tokyo-midnight all-day event is
/// constructed with (H1) or stores it floating against the device timezone
/// (H2). That question cannot be answered on a Mac or against a test fake,
/// so this screen exists for the real device to answer it. It is reachable
/// only with the `--allday-probe` launch argument, never from a product path.
///
/// Boundaries: it never talks to the server; it only ever creates, reads and
/// deletes `PA-PROBE`-titled events inside its own dedicated probe calendar
/// (nothing outside that calendar is touched, and nothing outside it is
/// deleted); the write pass is cleanup-then-create so pressing it twice
/// cannot accumulate duplicates. The log it produces is the evidence — copy
/// it verbatim into `docs/evidence/`, conclusions are drawn there, not here.
@MainActor
@Observable
final class AllDayProbeModel {
    static let probeCalendarTitle = "PersonalAgent 全天探针"
    static let probeEventPrefix = "PA-PROBE"

    private struct Spec {
        let title: String
        let days: Int
        let setsTimeZone: Bool
    }

    /// The write algorithm under test is the product's (design §3.2):
    /// DateComponents built in the target timezone, end date exclusive. The
    /// design leaves "does also setting `EKEvent.timeZone` change what the
    /// store persists?" to this matrix, so both variants are written — the
    /// product algorithm stays the one that does not set it.
    private static let specs: [Spec] = [
        Spec(title: "PA-PROBE 单日 不设时区", days: 1, setsTimeZone: false),
        Spec(title: "PA-PROBE 单日 设东京时区", days: 1, setsTimeZone: true),
        Spec(title: "PA-PROBE 三日 不设时区", days: 3, setsTimeZone: false),
        Spec(title: "PA-PROBE 三日 设东京时区", days: 3, setsTimeZone: true),
    ]

    var log = ""
    var busy = false
    /// Deleting the probe calendar removes every probe event with it; the
    /// dialog is the one place this screen asks before a destructive step.
    var confirmingDeletion = false

    private let store = EKEventStore()
    private var probeCalendar: EKCalendar?

    /// Tokyo midnight for the given date, built with a gregorian calendar
    /// pinned to Asia/Tokyo. Tokyo has no DST, which is what makes 00:00 an
    /// unambiguous probe instant. The end date is exclusive (Q5: 「10-01 到
    /// 10-03」 = three days → ends 10-04 00:00).
    private func tokyoMidnight(year: Int, month: Int, day: Int) -> Date {
        var gregorian = Calendar(identifier: .gregorian)
        gregorian.timeZone = TimeZone(identifier: "Asia/Tokyo")!
        var comps = DateComponents()
        comps.year = year
        comps.month = month
        comps.day = day
        comps.hour = 0
        comps.minute = 0
        return gregorian.date(from: comps)!
    }

    private func append(_ line: String) {
        log += line + "\n"
    }

    private func requestAccess() async -> Bool {
        (try? await store.requestFullAccessToEvents()) ?? false
    }

    // MARK: steps

    /// Finds or creates the dedicated probe calendar on the same source the
    /// device would use for new events — the probe must measure the store
    /// type Henson's real calendars actually live on, and the source type is
    /// part of the evidence. A failed creation is an error, never a fallback
    /// into a real calendar.
    func ensureProbeCalendar() async {
        busy = true
        defer { busy = false }
        guard await requestAccess() else {
            append("ERROR: 日历访问被拒（requestFullAccessToEvents）")
            return
        }
        append("== 探针日历 ==")
        if let existing = store.calendars(for: .event).first(where: {
            $0.title == Self.probeCalendarTitle
        }) {
            probeCalendar = existing
            append("已存在: \(existing.title) source=\(existing.source.title) type=\(existing.source.sourceType.rawValue)")
            return
        }
        guard let source = store.defaultCalendarForNewEvents?.source else {
            append("ERROR: 无可用的日历 source（defaultCalendarForNewEvents 为空）")
            return
        }
        let calendar = EKCalendar(for: .event, eventStore: store)
        calendar.title = Self.probeCalendarTitle
        calendar.source = source
        do {
            try store.saveCalendar(calendar, commit: true)
            probeCalendar = calendar
            append("已创建: \(calendar.title) source=\(source.title) type=\(source.sourceType.rawValue)")
        } catch {
            append("ERROR: 创建探针日历失败: \(error.localizedDescription)")
        }
    }

    /// Cleanup-then-create inside the probe calendar only, then read the
    /// events back **from the store** (a fresh fetch, not the in-memory
    /// objects — what the store persisted is the thing under test) and dump
    /// every field the read-back algorithm could hang on.
    func writeAndReadBack() async {
        busy = true
        defer { busy = false }
        guard let calendar = probeCalendar else {
            append("ERROR: 先执行「① 探针日历」")
            return
        }
        append("")
        append("== 写入并读回（设备时区: \(TimeZone.current.identifier)）==")
        append("写入算法: DateComponents(Asia/Tokyo) 2027-01-01 00:00 起，结束日期排他；"
            + "product 算法不设 event.timeZone，另一变体设 Asia/Tokyo 供矩阵对照")
        let removed = removeProbeEvents(in: calendar)
        append("清理旧探针事件: \(removed) 条（仅限探针日历内的 PA-PROBE 前缀）")
        do {
            for spec in Self.specs {
                try write(spec: spec, calendar: calendar)
            }
            try store.commit()
            append("已写入 \(Self.specs.count) 条")
        } catch {
            append("ERROR: 写入失败: \(error.localizedDescription)")
            return
        }
        let windowStart = tokyoMidnight(year: 2026, month: 12, day: 1)
        let windowEnd = tokyoMidnight(year: 2027, month: 2, day: 1)
        let predicate = store.predicateForEvents(
            withStart: windowStart, end: windowEnd, calendars: [calendar]
        )
        let events = store.events(matching: predicate)
            .filter { $0.title?.hasPrefix(Self.probeEventPrefix) == true }
        append("读回（store 重新取回）: \(events.count) 条")
        for event in events.sorted(by: { $0.title ?? "" < $1.title ?? "" }) {
            describe(event, at: TimeZone.current)
        }
    }

    /// The phase-B pass: Henson changes the device timezone by hand (Settings
    /// → 通用 → 日期与时间, e.g. America/New_York), then this rereads the same
    /// probe events. Whether `startDate` shifts with the device timezone is
    /// exactly the H1/H2 fork.
    func rereadOnly() async {
        busy = true
        defer { busy = false }
        guard let calendar = probeCalendar else {
            append("ERROR: 先执行「① 探针日历」")
            return
        }
        append("")
        append("== 仅读回（换时区后；设备时区: \(TimeZone.current.identifier)）==")
        let windowStart = tokyoMidnight(year: 2026, month: 12, day: 1)
        let windowEnd = tokyoMidnight(year: 2027, month: 2, day: 1)
        let predicate = store.predicateForEvents(
            withStart: windowStart, end: windowEnd, calendars: [calendar]
        )
        let events = store.events(matching: predicate)
            .filter { $0.title?.hasPrefix(Self.probeEventPrefix) == true }
        append("读回: \(events.count) 条")
        for event in events.sorted(by: { $0.title ?? "" < $1.title ?? "" }) {
            describe(event, at: TimeZone.current)
        }
    }

    func deleteProbeCalendar() async {
        busy = true
        defer { busy = false }
        confirmingDeletion = false
        guard let calendar = probeCalendar ?? store.calendars(for: .event)
            .first(where: { $0.title == Self.probeCalendarTitle }) else {
            append("探针日历不存在，无需删除")
            return
        }
        do {
            try store.removeCalendar(calendar, commit: true)
            probeCalendar = nil
            append("已删除探针日历（含其中全部探针事件）")
        } catch {
            append("ERROR: 删除探针日历失败: \(error.localizedDescription)")
        }
    }

    // MARK: write + describe

    private func write(spec: Spec, calendar: EKCalendar) throws {
        let start = tokyoMidnight(year: 2027, month: 1, day: 1)
        let endDay = 1 + spec.days
        let end = tokyoMidnight(year: 2027, month: 1, day: endDay)
        let event = EKEvent(eventStore: store)
        event.title = spec.title
        event.startDate = start
        event.endDate = end
        event.isAllDay = true
        if spec.setsTimeZone {
            event.timeZone = TimeZone(identifier: "Asia/Tokyo")
        }
        event.calendar = calendar
        try store.save(event, span: .thisEvent, commit: false)
    }

    /// Removes this probe's own events — prefix-matched, probe calendar
    /// only — so the write pass is idempotent.
    private func removeProbeEvents(in calendar: EKCalendar) -> Int {
        let windowStart = tokyoMidnight(year: 2026, month: 12, day: 1)
        let windowEnd = tokyoMidnight(year: 2027, month: 2, day: 1)
        let predicate = store.predicateForEvents(
            withStart: windowStart, end: windowEnd, calendars: [calendar]
        )
        let stale = store.events(matching: predicate)
            .filter { $0.title?.hasPrefix(Self.probeEventPrefix) == true }
        for event in stale {
            try? store.remove(event, span: .thisEvent, commit: false)
        }
        if !stale.isEmpty {
            try? store.commit()
        }
        return stale.count
    }

    /// The dump: every field the §3.2 read-back algorithm could hang on,
    /// raw. The same instant is projected through the anchor timezone
    /// (Asia/Tokyo) and the device timezone — the difference between those
    /// two projections is what H1 vs H2 means.
    private func describe(_ event: EKEvent, at deviceTimeZone: TimeZone) {
        append("-- \(event.title ?? "(untitled)") --")
        append("eventIdentifier: \(String(describing: event.eventIdentifier))")
        append("isAllDay: \(event.isAllDay)")
        append("event.timeZone: \(event.timeZone?.identifier ?? "nil")")
        append("startDate: \(iso(event.startDate)) (epoch \(Int(event.startDate.timeIntervalSince1970)))")
        append("  → as Asia/Tokyo:    \(dayComponents(event.startDate, in: TimeZone(identifier: "Asia/Tokyo")!))")
        append("  → as device(\(deviceTimeZone.identifier)): \(dayComponents(event.startDate, in: deviceTimeZone))")
        append("endDate:   \(iso(event.endDate)) (epoch \(Int(event.endDate.timeIntervalSince1970)))")
        append("  → as Asia/Tokyo:    \(dayComponents(event.endDate, in: TimeZone(identifier: "Asia/Tokyo")!))")
        append("  → as device(\(deviceTimeZone.identifier)): \(dayComponents(event.endDate, in: deviceTimeZone))")
        if let calendar = event.calendar {
            append(
                "calendar: \(calendar.title) source=\(calendar.source.title) "
                    + "type=\(calendar.source.sourceType.rawValue)"
            )
        }
    }

    private func iso(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.string(from: date)
    }

    private func dayComponents(_ date: Date, in timeZone: TimeZone) -> String {
        var gregorian = Calendar(identifier: .gregorian)
        gregorian.timeZone = timeZone
        let c = gregorian.dateComponents(
            [.year, .month, .day, .hour, .minute, .second], from: date
        )
        return String(
            format: "%04d-%02d-%02d %02d:%02d:%02d",
            c.year ?? 0, c.month ?? 0, c.day ?? 0,
            c.hour ?? 0, c.minute ?? 0, c.second ?? 0
        )
    }
}

/// Launched only with `--allday-probe` (see `PersonalAgentApp`). The screen
/// is a runbook in button form: ① calendar → ② write+readback → (change the
/// device timezone by hand) → ③ reread → ④ clean up. The log is the only
/// output that matters; everything else on this screen is a guardrail.
struct AllDayProbeView: View {
    @State private var model = AllDayProbeModel()

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    Text(
                        "一次性测量工具：只写自己的探针日历（PA-PROBE 前缀），"
                            + "不连服务器。产出日志整段复制进 docs/evidence/。"
                    )
                    .font(.footnote)
                    .foregroundStyle(.secondary)

                    stepButton("① 创建/定位探针日历", enabled: !model.busy) {
                        await model.ensureProbeCalendar()
                    }
                    stepButton("② 写入 4 个测试事件并读回（先清理旧探针事件）", enabled: !model.busy) {
                        await model.writeAndReadBack()
                    }
                    stepButton("③ 换设备时区后仅读回", enabled: !model.busy) {
                        await model.rereadOnly()
                    }
                    stepButton("④ 删除探针日历（含全部探针事件）", enabled: !model.busy, role: .destructive) {
                        model.confirmingDeletion = true
                    }

                    if !model.log.isEmpty {
                        Text(model.log)
                            .font(.system(.caption2, design: .monospaced))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .accessibilityLabel("探针日志，长按选择复制")
                    }
                }
                .padding()
            }
            .navigationTitle("全天探针 §13.1a")
            .navigationBarTitleDisplayMode(.inline)
            .confirmationDialog(
                "删除探针日历会连同删除其中全部探针事件，且不可撤销。继续？",
                isPresented: $model.confirmingDeletion,
                titleVisibility: .visible
            ) {
                Button("删除探针日历", role: .destructive) {
                    Task { await model.deleteProbeCalendar() }
                }
                Button("取消", role: .cancel) {}
            }
        }
    }

    private func stepButton(
        _ title: String, enabled: Bool, role: ButtonRole? = nil,
        action: @escaping () async -> Void
    ) -> some View {
        Button(role: role) {
            Task { await action() }
        } label: {
            Text(title)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .buttonStyle(.bordered)
        .disabled(!enabled)
    }
}
