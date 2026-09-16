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
    /// The confirmation dialog that stands between a tap and a recorded human
    /// fact. Its words are the last thing read before the conclusion is written,
    /// so which destination they name decides whether the person looks in the
    /// right place on the way in — the card's own rule, one tap later.
    public let confirmButton: String
    public let confirmMessage: String
    private let writtenConfirmPrompt: String
    private let notWrittenConfirmPrompt: String
    private let unknownConfirmPrompt: String

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

    /// The dialog's title for one conclusion.
    ///
    /// A value this build cannot name gets the neutral prompt rather than either
    /// conclusion's: the two buttons can only produce the two known values, so an
    /// unknown one means a server that has learned a third, and asking the person
    /// to confirm the *written* wording for a conclusion nobody has seen yet is
    /// an invention they would then confirm.
    public func confirmPrompt(forWire wire: String) -> String {
        switch wire {
        case ManualResolution.confirmedWritten.rawValue: return writtenConfirmPrompt
        case ManualResolution.confirmedNotWritten.rawValue: return notWrittenConfirmPrompt
        default: return unknownConfirmPrompt
        }
    }

    public static let ledger = ManualReviewCopy(
        instruction: "请先在飞书账本里核对这一笔（复核页有「打开飞书账本」），再选择结论。选择只记录你看到的事实，不会改动账本。",
        recordLabel: "记录 ID",
        writtenButton: "账本里有这笔",
        notWrittenButton: "账本里没有",
        writtenConclusion: "账本里有这笔",
        notWrittenConclusion: "账本里没有这笔",
        confirmButton: "确认，我已在账本里核对过",
        confirmMessage: "这个结论记录后不能在应用里改判：服务端会拒绝相反的答复。它只写在这次操作旁边，不会改动账本。",
        writtenConfirmPrompt: "确认飞书账本里已经有这一笔？",
        notWrittenConfirmPrompt: "确认飞书账本里没有这一笔？",
        unknownConfirmPrompt: "确认你已经核对过这个结论？"
    )

    public static let calendar = ManualReviewCopy(
        instruction: "请打开 iPhone 日历，在你指定的目标日历里核对这条日程，再选择结论。选择只记录你看到的事实，不会改动日历。",
        recordLabel: "事件标识符",
        writtenButton: "日历里有这条日程",
        notWrittenButton: "日历里没有",
        writtenConclusion: "日历里有这条日程",
        notWrittenConclusion: "日历里没有这条日程",
        confirmButton: "确认，我已在日历里核对过",
        confirmMessage: "这个结论记录后不能在应用里改判：服务端会拒绝相反的答复。它只写在这次操作旁边，不会改动日历。",
        writtenConfirmPrompt: "确认日历里有这条日程？",
        notWrittenConfirmPrompt: "确认日历里没有这条日程？",
        unknownConfirmPrompt: "确认你已经核对过这个结论？"
    )

    /// The words for a `manual_review_resolved` marker, whose domain is the one
    /// frozen into the event when it was appended.
    ///
    /// **Not** `forDomain`. A card's missing domain means a projection from
    /// before the field existed, and every such operation is a ledger write, so
    /// the ledger copy is the honest fallback there. A *marker*'s missing domain
    /// means the same thing historically, but the entry cannot be backfilled and
    /// the marker is read long after the operation is gone — and a calendar
    /// resolution appended by any build that did not write the field would carry
    /// 账本 for good. So a missing domain renders neutrally: it states the
    /// conclusion without naming a destination it cannot prove, which is the only
    /// claim the entry actually supports.
    public static func forResolvedMarker(_ domain: String?) -> ManualReviewCopy {
        domain == OperationReceipt.calendarDomain ? .calendar : .neutral
    }

    /// Neither destination's words. Used only for a marker whose domain was never
    /// recorded, and for a domain this build does not know — borrowing either
    /// domain's wording would be a claim about where the person looked.
    public static let neutral = ManualReviewCopy(
        instruction: "这条核对结论记录时没有写下核对的目标，请以你当时看到的事实为准。",
        recordLabel: "标识符",
        writtenButton: "目标里确实有",
        notWrittenButton: "目标里没有",
        writtenConclusion: "你核对到它已经存在（核对目标未记录）",
        notWrittenConclusion: "你核对到它不存在（核对目标未记录）",
        confirmButton: "确认，我已经核对过",
        confirmMessage: "这个结论记录后不能在应用里改判：服务端会拒绝相反的答复。它只写在这次操作旁边，不会改动任何东西。",
        writtenConfirmPrompt: "确认你核对到它已经存在？",
        notWrittenConfirmPrompt: "确认你核对到它不存在？",
        unknownConfirmPrompt: "确认你已经核对过这个结论？"
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

/// Which operations the loaded Timeline says have already been answered.
///
/// One statement of one rule, because two statements of it produced a defect.
/// The 2026-09-10 review found the acceptance checklist asking a person to look
/// for a 人工核对 prompt that the build could not show: the script seeded a
/// legacy record *and* a marker answering it, and the card hides its guidance
/// and buttons once the operation is answered. The rule itself is a single
/// fold — a `manual_review_resolved` event answers the operation it names — and
/// it had been written out in the view model and, silently, again in the
/// fixture, where nothing checked the two against each other.
///
/// It lives here, rather than in the view model, so the acceptance tests can ask
/// the same question the screen asks. `ChatModel.mirror` folds through this;
/// `AcceptanceScenarioTests` uses it to assert that a card the checklist calls
/// answerable really still is. Two copies would drift the same way twice.
///
/// The domain is deliberately not returned. What this answers is *whether* a
/// conclusion has been recorded; the wording fork is `ManualReviewCopy`'s, and
/// a caller that wanted the domain would be re-deriving the fork from a map.
public enum ManualReviewResolutionIndex {

    /// `operation_id` → the recorded resolution wire value. Timeline order, so
    /// a later marker for the same operation wins.
    public static func answered(by events: [TimelineEvent]) -> [String: String] {
        var answered: [String: String] = [:]
        for event in events {
            guard case .manualReviewResolved(let resolution, _) = event.kind,
                  let operationID = event.operationID
            else { continue }
            answered[operationID] = resolution
        }
        return answered
    }
}
