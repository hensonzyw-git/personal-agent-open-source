/// The frozen systemic-risk daily card, sealed by the risk-monitor job with the
/// scores read once at build time. `asOf`/`state` are mandatory — a card without
/// either refuses to decode rather than render a half card. The four scores and
/// the action conclusion may be absent (`null`) on a day whose surface produced
/// no value, so they are optional.
public struct RiskReportSnapshot: Sendable, Equatable {
    public let asOf: String
    public let state: String
    public let mbs: Double?
    public let css: Double?
    public let afrs: Double?
    public let ratesCredit: Double?
    public let action: String?
    /// ``ok`` or ``data_quality_warning``. Optional so an older card still
    /// decodes; when ``data_quality_warning`` the card shows a degradation badge.
    public let qualityStatus: String?
    /// Days ``asOf`` lags today; a value over 7 marks the card as stale. Optional.
    public let staleDays: Int?
    /// True when a score jumped suspiciously since the previous day. Optional.
    public let anomalous: Bool?
    /// The per-indicator breakdown behind MBS/CSS. Optional, and tolerant: an
    /// absent *or malformed* value degrades to `nil` (a score-only card) rather
    /// than failing the whole decode.
    public let components: RiskComponents?

    public init(
        asOf: String,
        state: String,
        mbs: Double?,
        css: Double?,
        afrs: Double?,
        action: String?,
        qualityStatus: String?,
        staleDays: Int?,
        anomalous: Bool?,
        components: RiskComponents?,
        ratesCredit: Double? = nil
    ) {
        self.asOf = asOf
        self.state = state
        self.mbs = mbs
        self.css = css
        self.afrs = afrs
        self.ratesCredit = ratesCredit
        self.action = action
        self.qualityStatus = qualityStatus
        self.staleDays = staleDays
        self.anomalous = anomalous
        self.components = components
    }
}

/// One indicator row on the risk card, already formatted by the backend into a
/// human-readable ``label``/``value`` pair plus a ``band`` for colour-coding.
public struct RiskComponent: Sendable, Equatable, Decodable {
    public let label: String
    public let value: String
    public let band: String
}

/// The two indicator groups. Each is a flat list of rows; the backend owns the
/// ordering (MBS then CSS).
public struct RiskComponents: Sendable, Equatable, Decodable {
    public let mbs: [RiskComponent]
    public let css: [RiskComponent]
    /// RCS is optional so cards sealed before the Treasury module existed still
    /// decode and render their original MBS/CSS breakdown.
    public let ratesCredit: [RiskComponent]?

    private enum CodingKeys: String, CodingKey {
        case mbs
        case css
        case ratesCredit = "rates_credit"
    }
}

extension RiskReportSnapshot: Decodable {
    private enum CodingKeys: String, CodingKey {
        case asOf = "as_of"
        case state
        case mbs
        case css
        case afrs
        case ratesCredit = "rates_credit"
        case action
        case qualityStatus = "quality_status"
        case staleDays = "stale_days"
        case anomalous
        case components
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        asOf = try container.decode(String.self, forKey: .asOf)
        state = try container.decode(String.self, forKey: .state)
        mbs = try container.decodeIfPresent(Double.self, forKey: .mbs)
        css = try container.decodeIfPresent(Double.self, forKey: .css)
        afrs = try container.decodeIfPresent(Double.self, forKey: .afrs)
        ratesCredit = try container.decodeIfPresent(Double.self, forKey: .ratesCredit)
        action = try container.decodeIfPresent(String.self, forKey: .action)
        qualityStatus = try container.decodeIfPresent(String.self, forKey: .qualityStatus)
        staleDays = try container.decodeIfPresent(Int.self, forKey: .staleDays)
        anomalous = try container.decodeIfPresent(Bool.self, forKey: .anomalous)
        // Tolerant: an absent OR malformed components degrades to nil (a
        // score-only card), never an .unrecognised event.
        components = try? container.decode(RiskComponents.self, forKey: .components)
        if asOf.isEmpty || state.isEmpty {
            throw DecodingError.dataCorruptedError(
                forKey: .asOf,
                in: container,
                debugDescription: "a risk card needs an as_of date and a state"
            )
        }
    }
}
