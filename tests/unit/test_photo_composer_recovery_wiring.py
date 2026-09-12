"""Static composer ownership regression, not iOS UI/runtime acceptance."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_durable_photo_transfer_consumes_only_the_captured_selection():
    source = (ROOT / "ios/PersonalAgent/ChatModel.swift").read_text()
    transfer = source.split("private func uploadAndSend(", 1)[1].split(
        "private func resumePendingPhotoSend", 1
    )[0]
    assert transfer.index("try savePendingPhotoSend(pending)") < transfer.index(
        "if preparedPhoto?.selectionID == photo.selectionID"
    ) < transfer.index("preparedPhoto = nil") < transfer.index(
        "return try await continuePendingPhotoSend(&pending)"
    )
    send = source.split("func send() async", 1)[1].split("func preparePhoto", 1)[0]
    assert "preparedPhoto = nil" not in send
    assert "unresolved == nil, !hasPendingPhotoSend, draft.isEmpty" in send


def test_selection_identity_is_independent_of_content_hash():
    source = (ROOT / "ios/PersonalAgent/PhotoPreparation.swift").read_text()
    assert "let selectionID = UUID()" in source
    assert "let sha256: String" in source
