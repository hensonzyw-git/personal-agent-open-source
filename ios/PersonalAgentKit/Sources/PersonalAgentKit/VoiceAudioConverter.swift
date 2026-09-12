import AVFAudio
import Foundation

/// Each recording owns a converter. Its mutable conversion state is protected
/// by the lock even if the audio framework invokes callbacks concurrently.
public final class VoiceAudioConverter: @unchecked Sendable {
    public enum Failure: Error { case invalidFormat, formatChanged, conversionFailed }
    private let lock = NSLock()
    private let converter: AVAudioConverter
    private let source: AVAudioFormat
    private let destination: AVAudioFormat

    private final class Input: @unchecked Sendable {
        private let lock = NSLock()
        private let buffer: AVAudioPCMBuffer
        private var supplied = false

        init(_ buffer: AVAudioPCMBuffer) { self.buffer = buffer }

        func next(_ status: UnsafeMutablePointer<AVAudioConverterInputStatus>) -> AVAudioBuffer? {
            lock.lock()
            defer { lock.unlock() }
            guard !supplied else {
                status.pointee = .noDataNow
                return nil
            }
            supplied = true
            status.pointee = .haveData
            return buffer
        }
    }

    public init(source: AVAudioFormat, destination: AVAudioFormat) throws {
        guard source.sampleRate > 0, source.channelCount > 0,
              destination.sampleRate > 0, destination.channelCount > 0,
              let converter = AVAudioConverter(from: source, to: destination) else {
            throw Failure.invalidFormat
        }
        self.source = source
        self.destination = destination
        self.converter = converter
    }

    public func convert(_ buffer: AVAudioPCMBuffer) throws -> AVAudioPCMBuffer {
        lock.lock()
        defer { lock.unlock() }
        guard buffer.format == source else { throw Failure.formatChanged }
        let capacity = ceil(Double(buffer.frameLength) * destination.sampleRate / source.sampleRate) + 16
        guard capacity > 0, capacity < Double(UInt32.max),
              let output = AVAudioPCMBuffer(pcmFormat: destination,
                                            frameCapacity: AVAudioFrameCount(capacity)) else {
            throw Failure.conversionFailed
        }
        let input = Input(buffer)
        var error: NSError?
        let status = converter.convert(to: output, error: &error) { _, inputStatus in
            input.next(inputStatus)
        }
        guard error == nil, status != .error else { throw Failure.conversionFailed }
        // The output owns its bytes; AVAudioEngine may reuse the input buffer
        // immediately after the tap returns while analysis consumes asynchronously.
        return output
    }
}
