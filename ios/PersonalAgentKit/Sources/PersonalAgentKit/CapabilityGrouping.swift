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
}
