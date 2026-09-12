import AVFAudio
import Speech
import Testing
@testable import PersonalAgentKit

@Suite("Voice audio format boundary")
struct VoiceAudioConverterTests {
    @Test("real float microphone buffers convert to independent signed int16 audio",
          arguments: [44_100.0, 48_000.0])
    func convertsMicrophonePCM(_ rate: Double) throws {
        let source = AVAudioFormat(standardFormatWithSampleRate: rate, channels: 2)!
        let destination = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000,
                                        channels: 1, interleaved: false)!
        let converter = try VoiceAudioConverter(source: source, destination: destination)
        let input = AVAudioPCMBuffer(pcmFormat: source, frameCapacity: 4_800)!
        input.frameLength = 4_800
        for channel in 0..<2 {
            input.floatChannelData![channel].update(repeating: 0.25, count: 4_800)
        }
        let output = try converter.convert(input)
        #expect(output.format == destination)
        #expect(output.format.commonFormat == .pcmFormatInt16)
        #expect(output.frameLength > 1_000)
        let count = Int(output.frameLength)
        let before = Array(UnsafeBufferPointer(start: output.int16ChannelData![0], count: count))
        #expect(before.contains { $0 > 1_000 })
        input.floatChannelData![0].update(repeating: 0, count: 4_800)
        input.floatChannelData![1].update(repeating: 0, count: 4_800)
        _ = try converter.convert(input)
        #expect(Array(UnsafeBufferPointer(start: output.int16ChannelData![0], count: count)) == before)
    }

    @Test("a route format change is refused before entering the converter")
    func rejectsFormatChange() throws {
        let source = AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1)!
        let other = AVAudioFormat(standardFormatWithSampleRate: 44_100, channels: 1)!
        let converter = try VoiceAudioConverter(source: source, destination: source)
        let input = AVAudioPCMBuffer(pcmFormat: other, frameCapacity: 16)!
        #expect(throws: VoiceAudioConverter.Failure.self) { try converter.convert(input) }
    }

    @MainActor
    @Test("the production tap converts on a background executor before yielding")
    func backgroundConversion() async throws {
        guard #available(macOS 26.0, iOS 26.0, *) else { return }
        let source = AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1)!
        let destination = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000,
                                        channels: 1, interleaved: false)!
        let converter = try VoiceAudioConverter(source: source, destination: destination)
        let pair = AsyncStream<AnalyzerInput>.makeStream()
        let tap = VoiceAudioTap.make(continuation: pair.continuation, converter: converter)
        await Task.detached {
            let format = AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1)!
            let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 4_800)!
            buffer.frameLength = 4_800
            buffer.floatChannelData![0].update(repeating: 0, count: 4_800)
            tap(buffer, AVAudioTime(sampleTime: 0, atRate: 48_000))
            pair.continuation.finish()
        }.value
        var count = 0
        for await input in pair.stream {
            #expect(input.buffer.format.commonFormat == .pcmFormatInt16)
            #expect(input.buffer.format.sampleRate == 16_000)
            count += 1
        }
        #expect(count == 1)
    }
}
