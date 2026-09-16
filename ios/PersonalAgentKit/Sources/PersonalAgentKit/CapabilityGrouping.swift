import Foundation

/// One alias domain's worth of granted tools, in the order the service listed them.
///
/// `id` is the **raw** alias prefix, never a display name. Naming is the client's
/// job; deciding what was granted is the service's, and keeping those apart is what
/// lets an unrecognised domain survive to be shown.
public struct CapabilityDomain: Equatable, Sendable, Identifiable {
    public let id: String
    /// Each tool's `summary`, or its alias where the service gave no summary. Never
    /// empty for a domain that appears at all.
    public let entries: [String]

    public init(id: String, entries: [String]) {
        self.id = id
        self.entries = entries
    }
}

extension Capabilities {
    /// Domains the user should be shown, grouped from the granted tool list.
    ///
    /// Three rules, all of them safety rules rather than layout preferences:
    ///
    /// 1. **An unknown domain is kept, under its raw prefix.** This is the same rule
    ///    the Timeline applies to an unrecognised event: a list that silently omits
    ///    a granted capability misrepresents what the device may do, and the reader
    ///    has no way to notice the omission.
    /// 2. **A tool with no summary falls back to its alias.** A row that renders
    ///    blank is indistinguishable from a row that is missing.
    /// 3. **Excluded domains match the whole prefix, never a substring.** `meta` is
    ///    infrastructure; a future `metadata.*` or `meta_finance.*` domain is not,
    ///    and must not disappear because its name starts with the same four letters.
    ///
    /// Service order is preserved: the service decides what comes first, and
    /// re-sorting here would invent an emphasis it did not express.
    ///
    /// - Parameter excluding: alias domains to leave out of the user-facing list.
    ///   Defaults to `meta`. Pass an empty set to group everything.
    public static func userFacingDomains(
        from tools: [Tool],
        excluding excluded: Set<String> = ["meta"]
    ) -> [CapabilityDomain] {
        var order: [String] = []
        var entries: [String: [String]] = [:]
        for tool in tools {
            let domain = Self.domain(ofAlias: tool.alias)
            guard !excluded.contains(domain) else { continue }
            if entries[domain] == nil { order.append(domain) }
            entries[domain, default: []].append(tool.summary ?? tool.alias)
        }
        return order.map { CapabilityDomain(id: $0, entries: entries[$0] ?? []) }
    }

    /// The part before the first `.`. An alias with no dot is its own domain rather
    /// than being discarded -- the client does not get to decide that a shape it did
    /// not expect is not a capability.
    static func domain(ofAlias alias: String) -> String {
        guard let head = alias.split(separator: ".", maxSplits: 1).first else {
            return alias
        }
        return String(head)
    }

    /// The client's display name for one alias domain.
    ///
    /// Same honesty rule as the capability rows: an unknown domain keeps its raw
    /// prefix, so a future domain shows up as an odd-looking name rather than a
    /// blank one. `finance` is the only domain the current contract names.
    public static func displayName(forDomain domain: String) -> String {
        switch domain {
        case "finance": return "财务"
        case "calendar": return "日历"
        default: return domain
        }
    }

    /// One granted tool's human-readable name, e.g. `finance.log_expense` →
    /// 「财务 · 记一笔支出」.
    ///
    /// §3a's 轨迹文字 needs the tool the server named in a receipt; a receipt
    /// carries only the alias, so the summary (and the domain name) come from the
    /// granted set. When the alias is not in the set — a tool granted no longer,
    /// or a history event that carries no tool — the alias is the honest fallback,
    /// the same fallback the capability rows use.
    public static func displayName(forAlias alias: String, tools: [Tool]) -> String {
        let summary = tools.first { $0.alias == alias }?.summary
        if let summary, !summary.isEmpty {
            return "\(displayName(forDomain: domain(ofAlias: alias))) · \(summary)"
        }
        return alias
    }
}
