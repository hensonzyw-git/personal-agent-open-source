import Foundation
import Testing

@testable import PersonalAgentKit

/// §1c's capability list decides what the app tells Henson it can do. The failure
/// that matters is not a mis-drawn row -- it is a granted capability that never
/// appears, because nothing on screen would reveal the omission.
///
/// The fixtures go through the real decoder rather than a memberwise initialiser, so
/// a change to the wire keys fails here instead of leaving these tests asserting
/// against a shape the service no longer sends.
@Suite("The §1c capability grouping")
struct CapabilityGroupingTests {
    private func tools(_ json: String) throws -> [Capabilities.Tool] {
        try JSONDecoder().decode([Capabilities.Tool].self, from: Data(json.utf8))
    }

    @Test("tools group by alias domain, in the order the service listed them")
    func groupsInServiceOrder() throws {
        let grouped = Capabilities.userFacingDomains(from: try tools("""
        [
          {"alias": "finance.log_expense", "summary": "记一笔支出"},
          {"alias": "wardrobe.suggest",    "summary": "推荐穿搭"},
          {"alias": "finance.query",       "summary": "查账"}
        ]
        """))

        #expect(grouped.map(\.id) == ["finance", "wardrobe"])
        #expect(grouped[0].entries == ["记一笔支出", "查账"])
        #expect(grouped[1].entries == ["推荐穿搭"])
    }

    @Test("an unknown domain is kept under its raw prefix, never dropped")
    func keepsUnknownDomain() throws {
        let grouped = Capabilities.userFacingDomains(from: try tools("""
        [{"alias": "seismology.predict", "summary": "预测地震"}]
        """))

        #expect(grouped.map(\.id) == ["seismology"])
    }

    @Test("meta is excluded as infrastructure")
    func excludesMeta() throws {
        let grouped = Capabilities.userFacingDomains(from: try tools("""
        [
          {"alias": "meta.capabilities", "summary": "读取能力清单"},
          {"alias": "finance.log_expense", "summary": "记一笔支出"}
        ]
        """))

        #expect(grouped.map(\.id) == ["finance"])
    }

    /// The exclusion is a whole-prefix match. A domain that merely *starts with*
    /// `meta` is a different domain, and losing it would be the exact failure this
    /// suite exists to catch.
    @Test("exclusion matches the whole domain, not a prefix of it")
    func exclusionIsNotSubstringMatching() throws {
        let grouped = Capabilities.userFacingDomains(from: try tools("""
        [
          {"alias": "metadata.read",     "summary": "读元数据"},
          {"alias": "meta_finance.read", "summary": "读财务元信息"},
          {"alias": "meta.capabilities", "summary": "读取能力清单"}
        ]
        """))

        #expect(grouped.map(\.id) == ["metadata", "meta_finance"])
    }

    @Test("a tool with no summary falls back to its alias rather than a blank row")
    func fallsBackToAlias() throws {
        let grouped = Capabilities.userFacingDomains(from: try tools("""
        [{"alias": "finance.log_expense"}]
        """))

        #expect(grouped[0].entries == ["finance.log_expense"])
    }

    @Test("an alias with no dot is its own domain")
    func aliasWithoutDot() throws {
        let grouped = Capabilities.userFacingDomains(from: try tools("""
        [{"alias": "ping", "summary": "存活探测"}]
        """))

        #expect(grouped.map(\.id) == ["ping"])
        #expect(grouped[0].entries == ["存活探测"])
    }

    /// An alias whose domain is empty (`".log"`) is malformed. It must still not
    /// vanish: an unreadable grant is not an absent one.
    @Test("a malformed alias is still surfaced")
    func malformedAliasSurvives() throws {
        let grouped = Capabilities.userFacingDomains(from: try tools("""
        [{"alias": ".log_expense", "summary": "记一笔"}]
        """))

        #expect(grouped.count == 1)
        #expect(grouped[0].entries == ["记一笔"])
    }

    @Test("no tools yields no domains, which the caller must not read as none granted")
    func emptyInput() {
        #expect(Capabilities.userFacingDomains(from: []).isEmpty)
    }

    @Test("an empty exclusion set groups everything, including meta")
    func excludingNothing() throws {
        let grouped = Capabilities.userFacingDomains(
            from: try tools("""
            [
              {"alias": "meta.capabilities", "summary": "读取能力清单"},
              {"alias": "finance.log_expense", "summary": "记一笔支出"}
            ]
            """),
            excluding: []
        )

        #expect(grouped.map(\.id) == ["meta", "finance"])
    }

    // --- §3a 轨迹文字: the tool a receipt names, shown as 财务 · 记一笔支出 ----

    @Test("a granted tool is shown as 域 · 摘要 for the §3a 轨迹文字")
    func displayNameJoinsDomainAndSummary() throws {
        let tools = try self.tools("""
        [
          {"alias": "finance.log_expense", "summary": "记一笔支出"},
          {"alias": "finance.log_income",  "summary": "记一笔收入"}
        ]
        """)

        #expect(
            Capabilities.displayName(forAlias: "finance.log_expense", tools: tools)
                == "财务 · 记一笔支出"
        )
        #expect(
            Capabilities.displayName(forAlias: "finance.log_income", tools: tools)
                == "财务 · 记一笔收入"
        )
    }

    @Test("an unknown domain keeps its raw prefix in the 轨迹文字")
    func displayNameKeepsUnknownDomain() throws {
        let tools = try self.tools("""
        [{"alias": "seismology.predict", "summary": "预测地震"}]
        """)

        #expect(
            Capabilities.displayName(forAlias: "seismology.predict", tools: tools)
                == "seismology · 预测地震"
        )
    }

    @Test("an alias absent from the granted set falls back to the alias itself")
    func displayNameFallsBackToAlias() throws {
        let tools = try self.tools("""
        [{"alias": "finance.log_expense", "summary": "记一笔支出"}]
        """)

        #expect(
            Capabilities.displayName(forAlias: "finance.log_income", tools: tools)
                == "finance.log_income"
        )
    }

    @Test("a tool with no summary falls back to the alias, the same fallback the capability rows use")
    func displayNameWithoutSummary() throws {
        let tools = try self.tools("""
        [{"alias": "finance.log_expense"}]
        """)

        #expect(
            Capabilities.displayName(forAlias: "finance.log_expense", tools: tools)
                == "finance.log_expense"
        )
    }
}
