import PersonalAgentKit
import SwiftUI
import UIKit

/// A single recognizer owns begin/release/cancel. There is no Button action
/// fired by the same finger-up that could cancel an in-flight finalization.
struct VoiceHoldButton: UIViewRepresentable {
    var enabled: Bool
    var active: Bool
    var began: () -> Void
    var released: () -> Void
    var cancelled: () -> Void

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeUIView(context: Context) -> HoldButton {
        let button = HoldButton(type: .custom)
        let gesture = UILongPressGestureRecognizer(target: context.coordinator,
                                                  action: #selector(Coordinator.changed(_:)))
        gesture.minimumPressDuration = 0.2
        button.addGestureRecognizer(gesture)
        button.activate = { [weak coordinator = context.coordinator] in
            coordinator?.accessibleActivation()
        }
        return button
    }

    func updateUIView(_ button: HoldButton, context: Context) {
        context.coordinator.owner = self
        if !enabled { context.coordinator.cancel() }
        button.isEnabled = enabled
        button.setImage(UIImage(systemName: active ? "mic.fill" : "mic",
                                withConfiguration: UIImage.SymbolConfiguration(pointSize: 22)),
                        for: .normal)
        button.tintColor = active ? .systemRed : .label
        button.accessibilityLabel = "语音输入"
        button.accessibilityHint = active ? "松开结束录音" : "长按说话，松开后编辑文字"
    }

    static func dismantleUIView(_ uiView: HoldButton, coordinator: Coordinator) {
        coordinator.cancel()
    }

    final class HoldButton: UIButton {
        var activate: (() -> Void)?
        override func accessibilityActivate() -> Bool {
            guard isEnabled else { return false }
            activate?()
            return true
        }
    }

    @MainActor
    final class Coordinator: NSObject {
        var owner: VoiceHoldButton
        private var hold = VoiceHoldState()
        init(_ owner: VoiceHoldButton) { self.owner = owner }

        @objc func changed(_ gesture: UILongPressGestureRecognizer) {
            switch gesture.state {
            case .began:
                if owner.enabled, hold.begin() { owner.began() }
            case .ended:
                if hold.end() { owner.released() }
            case .cancelled, .failed:
                cancel()
            default: break
            }
        }

        func cancel() {
            if hold.end() { owner.cancelled() }
        }

        func accessibleActivation() {
            if hold.end() { owner.released() }
            else if owner.enabled, hold.begin() { owner.began() }
        }
    }
}
