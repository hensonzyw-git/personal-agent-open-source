/// One physical hold has exactly one terminal event: release or cancel.
public struct VoiceHoldState {
    public private(set) var isHolding = false
    private var initialY = 0.0
    public init() {}
    public mutating func begin(atY y: Double = 0) -> Bool {
        guard !isHolding else { return false }
        isHolding = true
        initialY = y
        return true
    }
    public mutating func cancelIfMoved(toY y: Double) -> Bool {
        guard isHolding, y - initialY <= -50 else { return false }
        return end()
    }
    public mutating func end() -> Bool {
        guard isHolding else { return false }
        isHolding = false
        return true
    }
}
