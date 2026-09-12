import AVFAudio
import Observation
import PersonalAgentKit
import Speech

/// Local-only, hold-to-talk transcription. Audio buffers are passed directly to
/// Apple's on-device SpeechAnalyzer and are never added to the chat request or
/// written to disk. The composed text remains a normal, user-editable draft.
@MainActor
@Observable
final class VoiceInput {
    private var state = VoiceInputStateMachine()

    var isRecording: Bool { state.phase == .recording }
    var isPreparing: Bool { state.phase == .permission || state.phase == .preparing }
    var isActive: Bool { isPreparing || isRecording || state.phase == .finalizing }
    var transcript = ""
    var errorMessage: String?

    private let engine = AVAudioEngine()
    private var continuation: AsyncStream<AnalyzerInput>.Continuation?
    private var analyzer: SpeechAnalyzer?
    private var resultTask: Task<Void, Never>?
    private var analysisTask: Task<Void, Never>?
    private var finalSegments: [String] = []
    private var hasVolatileTail = false
    private var deadlineTask: Task<Void, Never>?
    private var finalizationTask: Task<Void, Never>?
    private var finishWaiter: CheckedContinuation<String, Never>?
    // The notification center owns this token; it is touched only in init and
    // deinit, which Swift runs outside this class's main-actor isolation.
    nonisolated(unsafe) private var interruptionObserver: NSObjectProtocol?

    init() {
        interruptionObserver = NotificationCenter.default.addObserver(
            forName: AVAudioSession.interruptionNotification,
            object: AVAudioSession.sharedInstance(),
            queue: .main
        ) { [weak self] notification in
            guard let type = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
                  AVAudioSession.InterruptionType(rawValue: type) == .began
            else { return }
            Task { @MainActor [weak self] in self?.interrupt() }
        }
    }

    deinit {
        if let interruptionObserver {
            NotificationCenter.default.removeObserver(interruptionObserver)
        }
    }

    func start() async {
        guard let generation = state.requestPermission() else { return }
        errorMessage = nil
        transcript = ""
        finalSegments = []
        hasVolatileTail = false
        armDeadline(seconds: 60, generation: generation)
        guard await microphoneAllowed() else {
            if state.generation == generation, state.phase == .permission {
                _ = state.permissionResult(false, generation: generation)
                teardown(clearTranscript: true)
                errorMessage = "未获得麦克风权限，无法开始语音输入。"
            }
            return
        }
        // A hold can be cancelled while the permission sheet or a language
        // asset download is on screen.  Its continuation must not start a
        // fresh recording after the cancellation has invalidated this token.
        guard state.permissionResult(true, generation: generation) else { return }
        guard SpeechTranscriber.isAvailable else {
            if state.fail(.unsupported, generation: generation) {
                errorMessage = "此设备当前不支持本机语音识别，请改为手动输入。"
            }
            return
        }
        do {
            // The current feature contract is Mandarin (zh-CN). Never silently
            // substitute a nearby locale: that would turn an unsupported
            // language into an unlabelled quality regression.
            guard let locale = await SpeechTranscriber.supportedLocale(
                equivalentTo: Locale(identifier: "zh-CN")
            ) else {
                if state.fail(.unsupported, generation: generation) {
                    errorMessage = "此设备未安装支持的中文语音识别，请先下载语言包或手动输入。"
                }
                return
            }
            let transcriber = SpeechTranscriber(locale: locale, preset: .progressiveTranscription)
            let modules: [any SpeechModule] = [transcriber]
            // Installation is explicit user-initiated (the hold gesture) and
            // only concerns Apple's local language asset. We never fall back to
            // cloud recognition when it is unsupported or unavailable.
            if await AssetInventory.status(forModules: modules) != .installed,
               let request = try await AssetInventory.assetInstallationRequest(supporting: modules) {
                try await request.downloadAndInstall()
            }
            guard state.generation == generation, state.phase == .preparing else { return }

            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.record, mode: .measurement)
            try session.setActive(true)
            let input = engine.inputNode
            let format = input.outputFormat(forBus: 0)
            guard let analysisFormat = await SpeechAnalyzer.bestAvailableAudioFormat(
                compatibleWith: modules
            ) else { throw VoiceAudioConverter.Failure.invalidFormat }
            guard state.generation == generation, state.phase == .preparing else { return }
            let converter = try VoiceAudioConverter(source: format, destination: analysisFormat)
            let analyzer = SpeechAnalyzer(modules: modules)
            try await analyzer.prepareToAnalyze(in: analysisFormat)
            guard state.generation == generation, state.phase == .preparing else { return }
            let pair = AsyncStream<AnalyzerInput>.makeStream()
            continuation = pair.continuation
            self.analyzer = analyzer
            input.installTap(onBus: 0, bufferSize: 1_024, format: format,
                             block: VoiceAudioTap.make(
                                continuation: pair.continuation, converter: converter,
                                onFailure: { [weak self] in
                                    Task { @MainActor [weak self] in
                                        self?.fail("录音格式转换失败，请重新录制。", generation: generation)
                                    }
                                }))
            try engine.start()
            guard state.beganRecording(generation: generation) else {
                teardown(clearTranscript: true)
                return
            }
            armDeadline(seconds: 120, generation: generation)
            analysisTask = Task { [weak self] in
                do {
                    try await analyzer.start(inputSequence: pair.stream)
                } catch is CancellationError {
                    return
                } catch {
                    self?.fail("本机语音识别启动失败，请改为手动输入。", generation: generation)
                }
            }
            resultTask = Task { [weak self] in
                do {
                    for try await result in transcriber.results {
                        self?.accept(result, generation: generation)
                    }
                } catch is CancellationError {
                    return
                } catch {
                    self?.fail("语音转写中断，请改为手动输入。", generation: generation)
                }
            }
        } catch {
            if state.fail(.unsupported, generation: generation) {
                teardown(clearTranscript: true)
                errorMessage = "本机语音包不可用，请联网下载系统语言包后重试，或手动输入。"
            }
        }
    }

    /// End input and wait for the analyzer's final result. A release is not an
    /// authorization to send partial speech: the caller receives only the final,
    /// still-editable transcript.
    func finish() async -> String {
        let generation = state.generation
        guard state.beginFinalizing(generation: generation) else { return "" }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        continuation?.finish()
        continuation = nil
        let currentAnalyzer = analyzer
        let currentAnalysis = analysisTask
        let currentResults = resultTask
        armDeadline(seconds: 15, generation: generation)
        return await withCheckedContinuation { waiter in
            finishWaiter = waiter
            finalizationTask = Task { [weak self] in
                do {
                    try await currentAnalyzer?.finalizeAndFinishThroughEndOfInput()
                    await currentAnalysis?.value
                    await currentResults?.value
                    guard let self, self.state.generation == generation,
                          self.state.phase == .finalizing else { return }
                    guard let final = self.state.acceptFinalText(
                            self.finalSegments.joined(separator: " "), generation: generation,
                            hasVolatileTail: self.hasVolatileTail
                          ) else {
                        self.errorMessage = "没有完整的最终识别结果，请重新录制。"
                        self.teardown(clearTranscript: true)
                        return
                    }
                    let completion = self.finishWaiter
                    self.finishWaiter = nil
                    self.teardown(clearTranscript: true)
                    self.state.resetEditable()
                    completion?.resume(returning: final)
                } catch {
                    self?.fail("语音识别未能完成，请重新录制。", generation: generation)
                }
            }
        }
    }

    func cancel() {
        state.cancel()
        teardown(clearTranscript: true)
    }

    func interrupt() {
        let generation = state.generation
        guard state.fail(.interrupted, generation: generation) else { return }
        errorMessage = "语音输入被系统中断，请重新录制。"
        teardown(clearTranscript: true)
    }

    func consumeTranscript() -> String {
        guard state.phase == .editable else {
            transcript = ""
            return ""
        }
        let result = transcript.trimmingCharacters(in: .whitespacesAndNewlines)
        transcript = ""
        state.resetEditable()
        return result
    }

    private func teardown(clearTranscript: Bool) {
        deadlineTask?.cancel()
        deadlineTask = nil
        finalizationTask?.cancel()
        finalizationTask = nil
        finishWaiter?.resume(returning: "")
        finishWaiter = nil
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        continuation?.finish()
        continuation = nil
        analysisTask?.cancel()
        resultTask?.cancel()
        analyzer = nil
        analysisTask = nil
        resultTask = nil
        if clearTranscript {
            transcript = ""
            finalSegments = []
            hasVolatileTail = false
        }
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    private func armDeadline(seconds: UInt64, generation: Int) {
        deadlineTask?.cancel()
        deadlineTask = Task { [weak self] in
            do { try await Task.sleep(nanoseconds: seconds * 1_000_000_000) }
            catch { return }
            guard let self, self.state.generation == generation, self.isActive else { return }
            _ = self.state.fail(.timedOut, generation: generation)
            self.errorMessage = "语音输入超时，请重新录制。"
            self.teardown(clearTranscript: true)
        }
    }

    private func accept(_ result: SpeechTranscriber.Result, generation: Int) {
        guard state.generation == generation,
              state.phase == .recording || state.phase == .finalizing
        else { return }
        if result.isFinal {
            hasVolatileTail = false
            // We do not fabricate a confidence threshold.  If the OS presents
            // alternatives, the contract requires the user to re-record rather
            // than silently choosing one interpretation.
            guard result.alternatives.isEmpty else {
                fail("语音识别出现多个候选，请重新录制或手动输入。", generation: generation)
                return
            }
            finalSegments.append(String(result.text.characters))
            transcript = finalSegments.joined(separator: " ")
        } else {
            hasVolatileTail = true
            transcript = (finalSegments + [String(result.text.characters)])
                .joined(separator: " ")
        }
    }

    private func fail(_ message: String, generation: Int) {
        guard state.generation == generation, isActive else { return }
        guard state.fail(.interrupted, generation: generation) else { return }
        errorMessage = message
        teardown(clearTranscript: false)
    }

    private func microphoneAllowed() async -> Bool {
        await withCheckedContinuation { continuation in
            AVAudioApplication.requestRecordPermission { allowed in
                continuation.resume(returning: allowed)
            }
        }
    }
}
