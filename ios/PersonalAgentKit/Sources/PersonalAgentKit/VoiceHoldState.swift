/// One physical hold has exactly one terminal event: release or cancel.
public struct VoiceHoldState {
    public private(set) var isHolding = false
    public init() {}
    public mutating func begin() -> Bool {
        guard !isHolding else { return false }
        isHolding = true
        return true
    }
    public mutating func end() -> Bool {
        guard isHolding else { return false }
        isHolding = false
        return true
    }
}
