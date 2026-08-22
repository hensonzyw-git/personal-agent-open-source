/// The frozen systemic-risk daily card, sealed by the risk-monitor job with the
/// scores read once at build time. `asOf`/`state` are mandatory — a card without
/// either refuses to decode rather than render a half card. The three scores and
/// the action conclusion may be absent (`null`) on a day whose surface produced
/// no value, so they are optional.
public struct RiskReportSnapshot: Sendable, Equatable {
    public let asOf: String
    public let state: String
    public let mbs: Double?
    public let css: Double?
    public let afrs: Double?
    public let action: String?
    /// The per-indicator breakdown behind MBS/CSS. Optional so a card sealed by
    /// an older backend (before this field existed) still decodes and renders.
    public let components: RiskComponents?

    public init(
        asOf: String,
        state: String,
        mbs: Double?,
        css: Double?,
        afrs: Double?,
        action: String?,
        components: RiskComponents?
    ) {
        self.asOf = asOf
        self.state = state
        self.mbs = mbs
        self.css = css
        self.afrs = afrs
        self.action = action
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
}

extension RiskReportSnapshot: Decodable {
    private enum CodingKeys: String, CodingKey {
        case asOf = "as_of"
        case state
        case mbs
        case css
        case afrs
        case action
        case components
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        asOf = try container.decode(String.self, forKey: .asOf)
        state = try container.decode(String.self, forKey: .state)
        mbs = try container.decodeIfPresent(Double.self, forKey: .mbs)
        css = try container.decodeIfPresent(Double.self, forKey: .css)
        afrs = try container.decodeIfPresent(Double.self, forKey: .afrs)
        action = try container.decodeIfPresent(String.self, forKey: .action)
        components = try container.decodeIfPresent(RiskComponents.self, forKey: .components)
        if asOf.isEmpty || state.isEmpty {
            throw DecodingError.dataCorruptedError(
                forKey: .asOf,
                in: container,
                debugDescription: "a risk card needs an as_of date and a state"
            )
        }
    }
}
