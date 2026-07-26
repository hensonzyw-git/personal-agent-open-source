import CryptoKit
import Foundation

/// The device challenge wire contract of technical design 4.2.1, Swift side.
///
/// The Python side of this contract is `personal_agent/auth/device_keys.py`, and
/// the two are checked against one frozen vector file. Nothing here is allowed to
/// be lenient: the server refuses padded base64url, refuses DER signatures and
/// refuses anything that is not exactly 64 bytes of `R || S`, so a "helpful"
/// fallback on this side would only hide a drift until a real iPhone failed to
/// authenticate.
public enum DeviceWireContract {
    public static let authContext = "personal-agent-auth-v1"
    public static let audience = "personal-agent-api"

    public static let publicKeyByteCount = 65
    public static let joseSignatureByteCount = 64
    public static let nonceByteCount = 32

    /// The five lines both sides sign: single LF separators, no trailing newline,
    /// no normalisation of any kind.
    public static func signingInput(
        challengeID: String,
        nonceB64U: String,
        deviceID: String
    ) throws -> Data {
        for field in [challengeID, nonceB64U, deviceID] {
            if field.isEmpty || field.contains("\n") {
                throw WireContractError.malformedSigningInput
            }
        }
        let text = [authContext, challengeID, nonceB64U, deviceID, audience]
            .joined(separator: "\n")
        return Data(text.utf8)
    }

    /// SHA-256 over the 65 raw public key bytes, unpadded base64url. It names the
    /// registered key that issued a token; it is not a per-request proof.
    public static func thumbprint(publicKeyX963: Data) throws -> String {
        guard publicKeyX963.count == publicKeyByteCount else {
            throw WireContractError.malformedPublicKey
        }
        return base64URLEncode(Data(SHA256.hash(data: publicKeyX963)))
    }

    /// Security.framework and the vector file both speak DER; the wire speaks
    /// JOSE. CryptoKit does the conversion, so no hand-rolled ASN.1 parser can
    /// disagree with the server's.
    public static func joseSignature(fromDER der: Data) throws -> Data {
        let signature: P256.Signing.ECDSASignature
        do {
            signature = try P256.Signing.ECDSASignature(derRepresentation: der)
        } catch {
            throw WireContractError.malformedSignature
        }
        let raw = signature.rawRepresentation
        guard raw.count == joseSignatureByteCount else {
            throw WireContractError.malformedSignature
        }
        return raw
    }

    /// Unpadded base64url, the only form the server accepts.
    public static func base64URLEncode(_ raw: Data) -> String {
        raw.base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    /// Decoding is only needed for values that arrive from the server (the
    /// nonce). Padding is added back locally; a padded value is never *sent*.
    public static func base64URLDecode(_ value: String) -> Data? {
        var text = value
            .replacingOccurrences(of: "-", with: "+")
            .replacingOccurrences(of: "_", with: "/")
        let remainder = text.count % 4
        if remainder > 0 {
            text.append(String(repeating: "=", count: 4 - remainder))
        }
        return Data(base64Encoded: text)
    }
}

public enum WireContractError: Error, Equatable {
    case malformedSigningInput
    case malformedPublicKey
    case malformedSignature
}
