import Testing
@testable import PersonalAgentKit

@Suite("Voice hold ownership")
struct VoiceHoldStateTests {
    @Test("upward cancellation is sticky even after moving back and releasing")
    func upwardCancel() {
        var hold = VoiceHoldState()
        let began = hold.begin(atY: 200)
        let belowThreshold = hold.cancelIfMoved(toY: 151)
        let cancel = hold.cancelIfMoved(toY: 150)
        let returnToOrigin = hold.cancelIfMoved(toY: 200)
        let release = hold.end()
        #expect([began, belowThreshold, cancel, returnToOrigin, release] == [true, false, true, false, false])
    }

    @Test("downward movement and small upward motion still allow one release")
    func normalRelease() {
        var hold = VoiceHoldState()
        let began = hold.begin(atY: 200)
        let down = hold.cancelIfMoved(toY: 300)
        let up = hold.cancelIfMoved(toY: 170)
        let release = hold.end()
        #expect([began, down, up, release] == [true, false, false, true])
    }
    @Test("one release cannot be followed by a second cancel of the same hold")
    func releaseThenCancel() {
        var hold = VoiceHoldState()
        let events = [hold.begin(), hold.begin(), hold.end(), hold.end()]
        #expect(events == [true, false, true, false])
    }
    @Test("cancel consumes the hold and the next hold can start normally")
    func cancelThenRelease() {
        var hold = VoiceHoldState()
        let events = [hold.end(), hold.begin(), hold.end(), hold.end(), hold.begin(), hold.end()]
        #expect(events == [false, true, true, false, true, true])
    }
}
