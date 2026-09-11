import Foundation

/// What "pass" means for each card in the isolated acceptance build.
///
/// This lives in the Kit, next to the script that produces the cards, for the
/// reason the calendar row's rendering rules do: a checklist that only exists in
/// a document drifts from the build it describes, and the drift is invisible
/// until someone ticks a box that no longer means anything. Every item here
/// **names the seeded event it is about**, and `AcceptanceChecklistTests` fails
/// if that event does not exist or does not project to the outcome the item
/// claims. A checklist item cannot therefore describe a card the harness does
/// not draw.
///
/// The app shows these through `AcceptanceScene`, so the words on the phone are
/// these words; `docs/Calendar验收构建与清单_v0.1.md` carries the same list for
/// reading away from the device.
public struct AcceptanceChecklistItem: Sendable, Equatable, Identifiable {
    public let id: String
    /// The seeded event this item is about. Never a description of one.
    public let anchorEventID: String
    public let title: String
    /// 看到什么即通过 — stated as something a person can look at and answer yes
    /// or no to, never as "the card works".
    public let pass: String
    /// Whether this item can only be ticked if the anchored card still offers
    /// its two 核对 buttons.
    ///
    /// A field rather than a turn of phrase, and it exists because a turn of
    /// phrase was not enough. The 2026-09-10 review found the checklist asking a
    /// person to look at a 人工核对 prompt the build could not show: the script
    /// had one legacy record and a marker answering it, and the card hides its
    /// guidance and buttons once the operation is answered. Nothing in the file
    /// said which items depended on the buttons still being there, so nothing
    /// could notice. `AcceptanceScenarioTests` now fails if a flag here and the
    /// seeded script disagree.
    public let requiresAnswerableCard: Bool
    /// The external identifier this item is about, when the person is expected
    /// to compare one. A field rather than a sentence for the reason the flag
    /// above is: the 2026-09-10 review found the checklist describing a state the
    /// build could not produce, and the first draft of `write-created` did it
    /// again in the other direction — it promised the event identifier on a
    /// success card's face, which §3c deliberately keeps off it.
    /// `AcceptanceScenarioTests` now ties this value to the operation the anchor
    /// event carries, so the expected identifier cannot drift from the seed.
    public let expectedIdentifier: String?

    public init(
        id: String,
        anchorEventID: String,
        title: String,
        pass: String,
        requiresAnswerableCard: Bool = false,
        expectedIdentifier: String? = nil
    ) {
        self.id = id
        self.anchorEventID = anchorEventID
        self.title = title
        self.pass = pass
        self.requiresAnswerableCard = requiresAnswerableCard
        self.expectedIdentifier = expectedIdentifier
    }
}

public enum AcceptanceChecklist {

    /// The ordered walk-through. The first four are the card types the review
    /// named; the rest are the ones this round's changes are actually about.
    public static let items: [AcceptanceChecklistItem] = [
        AcceptanceChecklistItem(
            id: "list-card",
            anchorEventID: "evt_accept_006",
            title: "列表卡（calendar.query_events）",
            pass: "两行日程，第二行右侧都写日历名「工作」："
                + "第一行标题「东京出差」带一个「已创建」标签，时间写"
                + "「10-01 至 10-03 全天」——**不能**出现 10-04；"
                + "第二行标题「客户拜访」，时间写「10-02 14:00 日本时间 开始」"
                + "——**必须**带着时区名，不能只给本地时间。"
        ),
        AcceptanceChecklistItem(
            id: "write-created",
            anchorEventID: "evt_accept_002",
            title: "日历写入成功（device_result = created）",
            pass: "卡片写「已创建日程」和工具名；卡面上**不能**出现「账本」两个字，"
                + "也**不能**有「仍要创建」按钮。标识符**不在卡面上**——成功卡按"
                + "设计把它收进长按菜单：长按这张卡，点「复制记录 ID」，值应当是"
                + "下面这一行。",
            expectedIdentifier: "EKA-ACCEPT-0001"
        ),
        AcceptanceChecklistItem(
            id: "write-duplicate",
            anchorEventID: "evt_accept_004",
            title: "日历查重命中（device_result = duplicate）",
            pass: "卡片说日历里已有这条日程（不是「账本已存在此记录」），"
                + "并且有一个「仍要创建」按钮。标识符**不在卡面上**（成功卡同"
                + "上）：长按卡片点「复制记录 ID」，值应当是下面这一行。",
            expectedIdentifier: "EKA-ACCEPT-0002"
        ),
        AcceptanceChecklistItem(
            id: "review-calendar",
            anchorEventID: "evt_accept_008",
            title: "人工核对（日历域）",
            pass: "卡片说「请打开 iPhone 日历…核对这条日程」，标识符一栏写"
                + "「事件标识符」，两个按钮是「日历里有这条日程」和「日历里没有」。"
                + "标识符一栏的值应当是下面这一行——这一张是未核对的卡，"
                + "标识符**在卡面上**（核对卡才有这个入口）。",
            requiresAnswerableCard: true,
            expectedIdentifier: "EKA-ACCEPT-0003"
        ),
        AcceptanceChecklistItem(
            id: "confirm-dialog",
            anchorEventID: "evt_accept_008",
            title: "确认弹窗（点上面那张卡的任一结论）",
            pass: "弹窗标题随点的是哪个按钮而变，分别是「确认日历里有这条日程？」"
                + "和「确认日历里没有这条日程？」；正文说「不会改动日历」。"
                + "弹窗里**不能**出现「账本」。",
            requiresAnswerableCard: true
        ),
        AcceptanceChecklistItem(
            id: "review-ledger",
            anchorEventID: "evt_accept_016",
            title: "人工核对（无 domain 的旧记录）",
            pass: "卡片回落到账本措辞（「请先在飞书账本里核对这一笔」），"
                + "并且**两个结论按钮都在**——这一张还没有被核对过。"
                + "这是旧记录的既定行为，不是缺陷。记录 ID 一栏的值应当是"
                + "下面这一行，记下它——重启那一项要用的就是这张卡。",
            requiresAnswerableCard: true,
            expectedIdentifier: "REC-ACCEPT-0002"
        ),
        AcceptanceChecklistItem(
            id: "marker-calendar",
            anchorEventID: "evt_accept_011",
            title: "核对结论标记（日历域）",
            pass: "历史行写「已人工核对：日历里有这条日程」。"
        ),
        AcceptanceChecklistItem(
            id: "marker-neutral",
            anchorEventID: "evt_accept_014",
            title: "核对结论标记（旧事件，无 domain）",
            pass: "历史行写「已人工核对：你核对到它已经存在（核对目标未记录）」，"
                + "**不出现**「账本」，也**不出现**「日历」。这是这一张**已经被"
                + "核对过**的旧记录留下的行——上面「无 domain 的旧记录」那一项"
                + "是另一条还没有被核对的记录。"
        ),
        AcceptanceChecklistItem(
            id: "relaunch",
            anchorEventID: "evt_accept_001",
            title: "杀进程重启后的历史恢复",
            pass: "先在「无 domain 的旧记录」那张卡上点一个结论，看到历史里多出"
                + "一行；再从后台完全划掉 App 重开：**你刚点出来的那一行还在**，"
                + "上面每一张卡片也都还在，顺序不变，列表卡的两行日程与重启前"
                + "逐字相同。**只看「还在」不够**——重新生成的种子会让同样的卡片"
                + "原样出现，所以判据是你自己点出来的那一行是否被保留。"
        ),
    ]

    /// The item ids the 2026-09-10 review named as required coverage. Kept as a
    /// list so a later edit that drops one is a failing test rather than a
    /// quietly shorter walk-through.
    public static let requiredItemIDs: [String] = [
        "list-card", "review-calendar", "review-ledger", "confirm-dialog",
        "relaunch",
    ]
}
