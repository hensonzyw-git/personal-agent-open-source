import AVFAudio
import Foundation
import Speech
import Testing
@testable import PersonalAgentKit

@Suite("Voice audio callback isolation")
struct VoiceAudioTapTests {
    private nonisolated static func isBackgroundThread() -> Bool {
        !Thread.isMainThread
    }

    @MainActor
    @Test("a tap created by the UI can run on a background executor")
    func backgroundDelivery() async throws {
        guard #available(macOS 26.0, iOS 26.0, *) else { return }
        let pair = AsyncStream<AnalyzerInput>.makeStream()
        let tap = VoiceAudioTap.make(continuation: pair.continuation)
        let ranOffMain = await Task.detached {
            let format = AVAudioFormat(standardFormatWithSampleRate: 16_000, channels: 1)!
            let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 16)!
            buffer.frameLength = 16
            tap(buffer, AVAudioTime(sampleTime: 0, atRate: 16_000))
            pair.continuation.finish()
            return Self.isBackgroundThread()
        }.value
        #expect(ranOffMain)
        var frames: [UInt32] = []
        for await input in pair.stream { frames.append(input.buffer.frameLength) }
        #expect(frames == [16])
    }

    @MainActor
    @Test("late callbacks after finish cannot enter a new recording")
    func finishedGeneration() async {
        guard #available(macOS 26.0, iOS 26.0, *) else { return }
        let old = AsyncStream<AnalyzerInput>.makeStream()
        let tap = VoiceAudioTap.make(continuation: old.continuation)
        old.continuation.finish()
        let next = AsyncStream<AnalyzerInput>.makeStream()
        await Task.detached {
            let format = AVAudioFormat(standardFormatWithSampleRate: 16_000, channels: 1)!
            let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 16)!
            tap(buffer, AVAudioTime(sampleTime: 0, atRate: 16_000))
        }.value
        next.continuation.finish()
        var oldCount = 0
        var nextCount = 0
        for await _ in old.stream { oldCount += 1 }
        for await _ in next.stream { nextCount += 1 }
        #expect(oldCount == 0)
        #expect(nextCount == 0)
    }
}
