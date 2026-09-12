import Testing
@testable import PersonalAgentKit

@Suite("Voice hold ownership")
struct VoiceHoldStateTests {
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
