import Foundation

/// The wording of one 人工核对 card, chosen by the operation's domain
/// (design §10, gap 4).
///
/// The card exists because the state cannot answer its own question: the system
/// could not establish whether the write landed, and only a person looking at the
/// *destination* can. Which destination that is, is the domain — a calendar write
/// cannot be checked in the ledger — so the domain decides where the person is
/// sent, and a wrong fork records a human conclusion about the wrong thing. The
/// server refuses to contradict a recorded resolution afterwards, so a
/// misdirected tap is not a cosmetic error.
///
/// It lives in the Kit rather than in the view for the same reason the calendar
/// row's rendering rules do: this is a rule, and a rule that only exists inside a
/// SwiftUI body can be compiled but never tested. The view renders these strings;
/// it does not choose them.
///
/// The buttons send the same two `ManualResolution` values in both domains,
/// because the fact a person reports — the record exists, or it does not — is the
/// same fact either way. Only the words and the destination differ.
///
/// The ledger copy is also the fallback for an operation with no recorded domain.
/// That is not a claim that such an operation is a ledger write; nothing here
/// knows that. It is the copy those cards have always carried, and a missing
/// domain only occurs on cards recorded before the field existed — see
/// `OperationReceipt.calendarDomain`.
public struct ManualReviewCopy: Sendable, Equatable {
    /// Where the person must go and look.
    public let instruction: String
    /// What the id on this card actually identifies. A ledger row and an event
    /// are not the same kind of thing, and labelling an event identifier 记录 ID
    /// would name the wrong object.
    public let recordLabel: String
    public let writtenButton: String
    public let notWrittenButton: String
    public let writtenConclusion: String
    public let notWrittenConclusion: String

    /// What the person concluded, in this domain's words. The wire values are
    /// the server's; only the sentence is ours. A value this build cannot name is
    /// still a conclusion that was recorded, and rendering it as unanswered would
    /// invite a second, contradicting tap.
    public func conclusion(forWire wire: String) -> String {
        switch wire {
        case ManualResolution.confirmedWritten.rawValue: return writtenConclusion
        case ManualResolution.confirmedNotWritten.rawValue: return notWrittenConclusion
        default: return "服务端结论 \(wire)"
        }
    }

    public static let ledger = ManualReviewCopy(
        instruction: "请先在飞书账本里核对这一笔（复核页有「打开飞书账本」），再选择结论。选择只记录你看到的事实，不会改动账本。",
        recordLabel: "记录 ID",
        writtenButton: "账本里有这笔",
        notWrittenButton: "账本里没有",
        writtenConclusion: "账本里有这笔",
        notWrittenConclusion: "账本里没有这笔"
    )

    public static let calendar = ManualReviewCopy(
        instruction: "请打开 iPhone 日历，在你指定的目标日历里核对这条日程，再选择结论。选择只记录你看到的事实，不会改动日历。",
        recordLabel: "事件标识符",
        writtenButton: "日历里有这条日程",
        notWrittenButton: "日历里没有",
        writtenConclusion: "日历里有这条日程",
        notWrittenConclusion: "日历里没有这条日程"
    )

    /// The fork. Exactly one value selects the calendar card; everything else —
    /// including a domain this build has never heard of, and no domain at all —
    /// draws the ledger card, which is the card every such operation drew before
    /// the fork existed. A new domain therefore inherits the *old* behaviour
    /// rather than the calendar one, which is the safe default: the ledger
    /// wording at least names a destination the user knows, while borrowing the
    /// calendar wording for a third domain would send them to the wrong one.
    public static func forDomain(_ domain: String?) -> ManualReviewCopy {
        domain == OperationReceipt.calendarDomain ? .calendar : .ledger
    }
}
