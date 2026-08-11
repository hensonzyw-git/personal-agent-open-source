import Foundation

extension Capabilities {
    /// The ledger the service names, if it names one this client is willing to open.
    ///
    /// Every 打开飞书账本 button on every surface resolves through here. The rule is
    /// a security rule, not a formatting one, so it exists once: the value arrives
    /// from the service and ends up in `openURL`, and a client that opened whatever
    /// it was handed would follow `http://` — or a custom scheme — into whatever the
    /// device has registered for it.
    ///
    /// - `https` only, compared case-insensitively. `URL` does **not** normalise the
    ///   scheme -- `URL(string: "HTTPS://…")?.scheme` is `"HTTPS"` -- while RFC 3986
    ///   makes schemes case-insensitive, so a literal `== "https"` rejects a legal
    ///   URL and the surface then claims 服务端未提供账本链接 about a service that
    ///   provided one. Fails closed, which is why it went unnoticed.
    /// - A host is required. `https:///ledger` parses successfully and points
    ///   nowhere.
    /// - Absent, unparseable and rejected all return `nil`. Callers re-read this on
    ///   every refresh, so a value that stops validating retracts the jump instead
    ///   of leaving the last good one on screen: stale and missing both mean "the
    ///   service names no ledger now".
    public var validatedLedgerURL: URL? {
        guard let raw = ledgerURL,
              let url = URL(string: raw),
              url.scheme?.lowercased() == "https",
              let host = url.host(), !host.isEmpty
        else { return nil }
        return url
    }
}
