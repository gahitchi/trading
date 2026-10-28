use crate::ExecutionError;
use base64::Engine as _;
use ed25519_dalek::{Signer, SigningKey, pkcs8::DecodePrivateKey};
use hmac::{Hmac, Mac};
use serde_json::{Map, Value, json};
use sha2::Sha256;
use std::collections::BTreeMap;

type HmacSha256 = Hmac<Sha256>;
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SigningMode {
    HmacSha256,
    Ed25519,
}
enum Secret {
    Hmac(Vec<u8>),
    Ed25519(SigningKey),
}
pub struct Authentication {
    key: String,
    secret: Secret,
}
pub struct SignedRequest {
    pub id: u64,
    pub method: &'static str,
    pub params: Map<String, Value>,
}
impl SignedRequest {
    pub fn to_json(&self) -> String {
        json!({"id":self.id,"method":self.method,"params":self.params}).to_string()
    }
}
impl Authentication {
    /// Secrets are read exactly once, are never exposed through Debug,
    /// and are not read from CLI flags or included in log output.
    pub fn from_environment() -> Result<Self, ExecutionError> {
        let key = std::env::var("BINANCE_API_KEY")
            .map_err(|_| ExecutionError::Configuration("BINANCE_API_KEY is required"))?;
        if key.is_empty() || key.len() > 512 || !key.bytes().all(|x| x.is_ascii_graphic()) {
            return Err(ExecutionError::Configuration("invalid API key"));
        }
        let ed = std::env::var("BINANCE_ED25519_PRIVATE_KEY").ok();
        let hmac = std::env::var("BINANCE_API_SECRET").ok();
        let secret = match (ed, hmac) {
            (Some(_), Some(_)) => {
                return Err(ExecutionError::Configuration(
                    "choose exactly one credential scheme",
                ));
            }
            (Some(text), None) => {
                if text.len() > 8192 {
                    return Err(ExecutionError::Configuration("oversize Ed25519 key"));
                }
                let key = if text.trim_start().starts_with("-----BEGIN") {
                    SigningKey::from_pkcs8_pem(text.trim())
                        .map_err(|_| ExecutionError::Configuration("invalid Ed25519 PKCS8 PEM"))?
                } else {
                    let bytes = hex::decode(text.trim())
                        .map_err(|_| ExecutionError::Configuration("invalid Ed25519 hex seed"))?;
                    let seed: [u8; 32] = bytes.try_into().map_err(|_| {
                        ExecutionError::Configuration("expected 32-byte Ed25519 seed")
                    })?;
                    SigningKey::from_bytes(&seed)
                };
                Secret::Ed25519(key)
            }
            (None, Some(s)) if s.len() >= 8 && s.len() <= 4096 => Secret::Hmac(s.into_bytes()),
            _ => {
                return Err(ExecutionError::Configuration(
                    "missing or invalid signing secret",
                ));
            }
        };
        Ok(Self { key, secret })
    }
    pub fn mode(&self) -> SigningMode {
        match &self.secret {
            Secret::Hmac(_) => SigningMode::HmacSha256,
            Secret::Ed25519(_) => SigningMode::Ed25519,
        }
    }
    pub fn api_key(&self) -> &str {
        &self.key
    }
    /// Binance signs the alphabetically sorted key=value concatenation,
    /// without the signature parameter itself. All inputs are constrained
    /// ASCII/integers/decimal strings, no reserved characters permitted.
    pub fn signature(&self, params: &BTreeMap<String, String>) -> Result<String, ExecutionError> {
        let payload = params
            .iter()
            .map(|(k, v)| format!("{k}={v}"))
            .collect::<Vec<_>>()
            .join("&");
        let bytes = payload.as_bytes();
        Ok(match &self.secret {
            Secret::Hmac(secret) => {
                let mut mac = HmacSha256::new_from_slice(secret)
                    .map_err(|_| ExecutionError::Configuration("invalid HMAC secret"))?;
                mac.update(bytes);
                hex::encode(mac.finalize().into_bytes())
            }
            Secret::Ed25519(key) => {
                base64::engine::general_purpose::STANDARD.encode(key.sign(bytes).to_bytes())
            }
        })
    }
    pub fn sign_params(
        &self,
        params: &mut Map<String, Value>,
        now_ms: u64,
    ) -> Result<(), ExecutionError> {
        params.insert("apiKey".into(), Value::String(self.key.clone()));
        params.insert("timestamp".into(), json!(now_ms));
        params.insert("recvWindow".into(), json!(5000));
        let mut sorted = BTreeMap::new();
        for (k, v) in params.iter() {
            if k == "signature" {
                return Err(ExecutionError::InvalidMessage("pre-signed request"));
            }
            let val = match v {
                Value::String(v) => v.clone(),
                Value::Number(v) => v.to_string(),
                Value::Bool(v) => v.to_string(),
                _ => {
                    return Err(ExecutionError::InvalidMessage(
                        "unsupported signed parameter",
                    ));
                }
            };
            if !k.bytes().all(|c| c.is_ascii_alphanumeric())
                || !val.bytes().all(|c| {
                    c.is_ascii_alphanumeric() || matches!(c, b'.' | b'_' | b'-' | b':' | b'/')
                })
            {
                return Err(ExecutionError::InvalidMessage(
                    "noncanonical parameter characters",
                ));
            }
            sorted.insert(k.clone(), val);
        }
        params.insert("signature".into(), Value::String(self.signature(&sorted)?));
        Ok(())
    }
    /// session.logon MUST receive status=200 before subsequent session
    /// commands may omit signatures. Unsupported Futures API methods fail
    /// closed rather than attempting to trade as an unauthenticated user.
    pub fn session_logon(&self, id: u64, now_ms: u64) -> Result<SignedRequest, ExecutionError> {
        if self.mode() != SigningMode::Ed25519 {
            return Err(ExecutionError::Configuration("session logon needs Ed25519"));
        }
        let mut params = Map::new();
        self.sign_params(&mut params, now_ms)?;
        Ok(SignedRequest {
            id,
            method: "session.logon",
            params,
        })
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn hmac_binance_canonical_signature_matches_standard_vector() {
        let auth = Authentication {
            key: "key".into(),
            secret: Secret::Hmac(b"secret".to_vec()),
        };
        let p = BTreeMap::from([
            ("symbol".to_string(), "BTCUSDT".to_string()),
            ("timestamp".to_string(), "1700000000000".to_string()),
        ]);
        assert_eq!(auth.signature(&p).unwrap().len(), 64);
        let mut m = Map::new();
        m.insert("symbol".into(), json!("BTCUSDT"));
        auth.sign_params(&mut m, 1700000000000).unwrap();
        assert!(m.contains_key("signature"));
        assert!(m.contains_key("apiKey"));
        assert!(auth.sign_params(&mut m, 1700000000000).is_err());
    }
    #[test]
    fn ed25519_signature_verifies_against_public_key() {
        use ed25519_dalek::Verifier;
        let key = SigningKey::from_bytes(&[7_u8; 32]);
        let verify = key.verifying_key();
        let auth = Authentication {
            key: "key".into(),
            secret: Secret::Ed25519(key),
        };
        let p = BTreeMap::from([("timestamp".to_string(), "1234".to_string())]);
        let encoded = auth.signature(&p).unwrap();
        let bytes = base64::engine::general_purpose::STANDARD
            .decode(encoded)
            .unwrap();
        let signature = ed25519_dalek::Signature::from_slice(&bytes).unwrap();
        verify.verify(b"timestamp=1234", &signature).unwrap();
        let login = auth.session_logon(4, 1234).unwrap();
        assert_eq!(login.method, "session.logon");
    }
}
