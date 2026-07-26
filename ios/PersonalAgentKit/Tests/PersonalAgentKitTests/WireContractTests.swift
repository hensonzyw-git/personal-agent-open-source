import CryptoKit
import Foundation
import Testing

@testable import PersonalAgentKit

/// The Swift half of the cross-language check design 4.2.1 asks for.
///
/// It reads the **same file** the Python tests read -- there is no copy of the
/// vectors in this package, because two copies of a frozen contract drift and the
/// whole point is that they cannot. If this suite is ever run outside the repo,
/// the vector-dependent tests fail rather than pass silently.
struct Vectors {
    let curve: String
    let privateKeyScalarHex: String
    let publicKeyX963B64U: String
    let thumbprintB64U: String
    let challengeID: String
    let deviceID: String
    let nonceB64U: String
    let signingInput: String
    let signingInputSHA256B64U: String
    let signatureDERB64U: String
    let signatureJOSEB64U: String

    static func load() -> Vectors? {
        // …/ios/PersonalAgentKit/Tests/PersonalAgentKitTests/WireContractTests.swift
        let here = URL(fileURLWithPath: #filePath)
        let repoRoot = here
            .deletingLastPathComponent()  // PersonalAgentKitTests
            .deletingLastPathComponent()  // Tests
            .deletingLastPathComponent()  // PersonalAgentKit
            .deletingLastPathComponent()  // ios
            .deletingLastPathComponent()  // repo root
        let path = repoRoot.appendingPathComponent(
            "src/personal_agent/auth/vectors/device_auth_vectors.json"
        )
        guard
            let data = try? Data(contentsOf: path),
            let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        func string(_ key: String) -> String { json[key] as? String ?? "" }
        return Vectors(
            curve: string("curve"),
            privateKeyScalarHex: string("private_key_scalar_hex"),
            publicKeyX963B64U: string("public_key_x963_b64u"),
            thumbprintB64U: string("device_key_thumbprint_b64u"),
            challengeID: string("challenge_id"),
            deviceID: string("device_id"),
            nonceB64U: string("nonce_b64u"),
            signingInput: string("signing_input_utf8"),
            signingInputSHA256B64U: string("signing_input_sha256_b64u"),
            signatureDERB64U: string("signature_der_b64u"),
            signatureJOSEB64U: string("signature_jose_b64u")
        )
    }
}

private func hexData(_ hex: String) -> Data {
    var data = Data()
    var index = hex.startIndex
    while index < hex.endIndex {
        let next = hex.index(index, offsetBy: 2)
        data.append(UInt8(hex[index..<next], radix: 16)!)
        index = next
    }
    return data
}

@Suite("The frozen cross-language wire contract")
struct WireContractTests {
    let vectors: Vectors?

    init() {
        vectors = Vectors.load()
    }

    @Test("the vector private key derives the vector public key, byte for byte")
    func publicKeyEncoding() throws {
        let vectors = try #require(vectors)
        let key = try P256.Signing.PrivateKey(
            rawRepresentation: hexData(vectors.privateKeyScalarHex)
        )
        let encoded = DeviceWireContract.base64URLEncode(
            key.publicKey.x963Representation
        )
        #expect(encoded == vectors.publicKeyX963B64U)
        #expect(key.publicKey.x963Representation.count == 65)
        #expect(key.publicKey.x963Representation.first == 0x04)
    }

    @Test("the thumbprint is SHA-256 over the 65 raw bytes")
    func thumbprint() throws {
        let vectors = try #require(vectors)
        let raw = DeviceWireContract.base64URLDecode(vectors.publicKeyX963B64U)!
        #expect(try DeviceWireContract.thumbprint(publicKeyX963: raw) == vectors.thumbprintB64U)
    }

    @Test("the signing input is the five lines, with no trailing newline")
    func signingInput() throws {
        let vectors = try #require(vectors)
        let message = try DeviceWireContract.signingInput(
            challengeID: vectors.challengeID,
            nonceB64U: vectors.nonceB64U,
            deviceID: vectors.deviceID
        )
        #expect(String(data: message, encoding: .utf8) == vectors.signingInput)
        #expect(
            DeviceWireContract.base64URLEncode(Data(SHA256.hash(data: message)))
                == vectors.signingInputSHA256B64U
        )
    }

    @Test("DER converts to exactly the vector's JOSE signature")
    func derToJose() throws {
        let vectors = try #require(vectors)
        let der = DeviceWireContract.base64URLDecode(vectors.signatureDERB64U)!
        let jose = try DeviceWireContract.joseSignature(fromDER: der)
        #expect(jose.count == 64)
        #expect(DeviceWireContract.base64URLEncode(jose) == vectors.signatureJOSEB64U)
    }

    @Test("the vector signature verifies against the vector public key")
    func signatureVerifies() throws {
        let vectors = try #require(vectors)
        let raw = DeviceWireContract.base64URLDecode(vectors.publicKeyX963B64U)!
        let publicKey = try P256.Signing.PublicKey(x963Representation: raw)
        let message = try DeviceWireContract.signingInput(
            challengeID: vectors.challengeID,
            nonceB64U: vectors.nonceB64U,
            deviceID: vectors.deviceID
        )
        let jose = try DeviceWireContract.joseSignature(
            fromDER: DeviceWireContract.base64URLDecode(vectors.signatureDERB64U)!
        )
        let signature = try P256.Signing.ECDSASignature(rawRepresentation: jose)
        #expect(publicKey.isValidSignature(signature, for: message))
    }

    @Test("base64url on the wire never carries padding")
    func noPadding() {
        for length in 1...64 {
            let encoded = DeviceWireContract.base64URLEncode(
                Data(repeating: 0xAB, count: length)
            )
            #expect(!encoded.contains("="))
            #expect(!encoded.contains("+"))
            #expect(!encoded.contains("/"))
        }
    }

    @Test("a signing input field with a newline is refused, not escaped")
    func newlineInField() {
        #expect(throws: WireContractError.malformedSigningInput) {
            try DeviceWireContract.signingInput(
                challengeID: "a\nb", nonceB64U: "n", deviceID: "d"
            )
        }
        #expect(throws: WireContractError.malformedSigningInput) {
            try DeviceWireContract.signingInput(
                challengeID: "a", nonceB64U: "", deviceID: "d"
            )
        }
    }

    @Test("a public key of the wrong length has no thumbprint")
    func shortPublicKey() {
        #expect(throws: WireContractError.malformedPublicKey) {
            try DeviceWireContract.thumbprint(publicKeyX963: Data(repeating: 4, count: 64))
        }
    }

    @Test("garbage is not silently converted to a signature")
    func garbageSignature() {
        #expect(throws: WireContractError.malformedSignature) {
            try DeviceWireContract.joseSignature(fromDER: Data([0x00, 0x01, 0x02]))
        }
    }

    @Test("a software identity produces a 64-byte signature the wire accepts")
    func softwareIdentitySigns() throws {
        let identity = SoftwareDeviceIdentity()
        let message = try DeviceWireContract.signingInput(
            challengeID: "c", nonceB64U: "n", deviceID: "d"
        )
        let signature = try identity.signature(for: message)
        #expect(signature.count == 64)
        let publicKey = try P256.Signing.PublicKey(x963Representation: identity.publicKeyX963)
        #expect(
            publicKey.isValidSignature(
                try P256.Signing.ECDSASignature(rawRepresentation: signature),
                for: message
            )
        )
        #expect(identity.thumbprint != nil)
    }
}
