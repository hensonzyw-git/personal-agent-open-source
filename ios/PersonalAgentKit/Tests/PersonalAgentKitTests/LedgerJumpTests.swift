import Foundation
import Testing

@testable import PersonalAgentKit

/// The ledger jump is the one place a service-supplied string becomes a URL the
/// device opens. Every 打开飞书账本 button resolves through it, so the rejection
/// cases are the point of the suite — an accepted `http://` or custom scheme is a
/// value from the network deciding what the phone launches.
@Suite("The ledger jump target")
struct LedgerJumpTests {
    private func capabilities(ledgerURL: String?) throws -> Capabilities {
        let ledger = ledgerURL.map { "\"\($0)\"" } ?? "null"
        return try JSONDecoder().decode(Capabilities.self, from: Data("""
        {
          "allowed_tools_version": "v1",
          "tools": [],
          "conversation_id": "conv-1",
          "ledger_url": \(ledger)
        }
        """.utf8))
    }

    @Test("an https URL is accepted")
    func acceptsHTTPS() throws {
        #expect(
            try capabilities(ledgerURL: "https://example.feishu.cn/base/abc")
                .validatedLedgerURL?.absoluteString
                == "https://example.feishu.cn/base/abc"
        )
    }

    /// RFC 3986 makes schemes case-insensitive, and `URL` does not normalise them:
    /// `URL(string: "HTTPS://…")?.scheme` is `"HTTPS"`. A literal `== "https"`
    /// therefore rejects a legal URL, and the surface reports 服务端未提供账本链接
    /// about a service that did provide one. It fails closed, so nothing breaks
    /// loudly -- which is exactly why it survived in shipped code until this test.
    @Test("an uppercase scheme is accepted; URL does not normalise it for us")
    func acceptsUppercaseScheme() throws {
        #expect(try capabilities(ledgerURL: "HTTPS://example.feishu.cn/base/abc")
            .validatedLedgerURL != nil)
    }

    @Test("plain http is rejected")
    func rejectsHTTP() throws {
        #expect(try capabilities(ledgerURL: "http://example.feishu.cn/base/abc")
            .validatedLedgerURL == nil)
    }

    /// The case that matters most: a non-web scheme hands the device off to
    /// whatever app claims it.
    @Test("a non-web scheme is rejected", arguments: [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "feishu://open/base/abc",
        "data:text/html,<script>",
    ])
    func rejectsOtherSchemes(raw: String) throws {
        #expect(try capabilities(ledgerURL: raw).validatedLedgerURL == nil)
    }

    @Test("a URL with no host is rejected even though it parses")
    func rejectsHostlessURL() throws {
        #expect(try capabilities(ledgerURL: "https:///base/abc")
            .validatedLedgerURL == nil)
    }

    @Test("a schemeless value is rejected rather than assumed to be https")
    func rejectsSchemeless() throws {
        #expect(try capabilities(ledgerURL: "example.feishu.cn/base/abc")
            .validatedLedgerURL == nil)
    }

    @Test("no ledger named means no jump")
    func absentIsNil() throws {
        #expect(try capabilities(ledgerURL: nil).validatedLedgerURL == nil)
    }

    @Test("an empty string is rejected")
    func emptyIsNil() throws {
        #expect(try capabilities(ledgerURL: "").validatedLedgerURL == nil)
    }

    @Test("a legacy capability response defaults image input closed")
    func missingImagesDefaultsClosed() throws {
        let decoded = try capabilities(ledgerURL: nil)
        #expect(!decoded.images.enabled)
        #expect(decoded.images.maxContentBytes == nil)
        #expect(decoded.images.allowedMIMEs.isEmpty)
    }

    @Test("image limits are server facts, not client defaults")
    func decodesImageLimits() throws {
        let decoded = try JSONDecoder().decode(Capabilities.self, from: Data("""
        {
          "allowed_tools_version": "v1", "tools": [], "conversation_id": "conv-1",
          "images": {"enabled": true, "max_content_bytes": 123, "max_dimension": 45,
                     "allowed_mimes": ["image/jpeg"]}
        }
        """.utf8))
        #expect(decoded.images.enabled)
        #expect(decoded.images.maxContentBytes == 123)
        #expect(decoded.images.maxDimension == 45)
        #expect(decoded.images.allowedMIMEs == ["image/jpeg"])
    }

    @Test("disabled images without media limits preserve the tool catalog")
    func disabledImagesKeepTools() throws {
        let decoded = try JSONDecoder().decode(Capabilities.self, from: Data("""
        {"allowed_tools_version":"v1","tools":[{"alias":"meta.capabilities"}],
         "conversation_id":"conv-1","images":{"enabled":false}}
        """.utf8))
        #expect(decoded.tools.map(\.alias) == ["meta.capabilities"])
        #expect(!decoded.images.enabled)
        #expect(decoded.images.allowedMIMEs.isEmpty)
        #expect(decoded.images.maxContentBytes == nil)
        #expect(decoded.images.maxDimension == nil)
    }

    @Test("malformed image facts and enabled images without MIME facts are refused",
          arguments: [
            #"{"enabled":true}"#,
            #"{"enabled":false,"allowed_mimes":42}"#,
            #"{"enabled":"false"}"#,
            #"{"allowed_mimes":[]}"#,
            #"{"enabled":false,"max_dimension":"large"}"#
          ])
    func rejectsInvalidImageFacts(_ images: String) {
        #expect(throws: DecodingError.self) {
            try JSONDecoder().decode(Capabilities.ImageInputCapability.self,
                                     from: Data(images.utf8))
        }
    }
}
