"""OBYS tokens must not cross the standalone and AADS auth boundaries."""

import os
import unittest
from unittest.mock import patch

import jwt

from app import auth


class ObysTokenIsolationTests(unittest.TestCase):
    def test_standalone_requires_issuer_audience_and_valid_signature(self):
        with patch.dict(os.environ, {"OBYS_STANDALONE": "1"}):
            obys_token = auth.create_token("user-1", "user@example.test")
            self.assertEqual(auth.verify_token(obys_token)["sub"], "user-1")

            claims = jwt.decode(
                obys_token, auth.SECRET_KEY, algorithms=[auth.ALGORITHM],
                audience=auth.OBYS_TOKEN_AUDIENCE,
            )
            for missing in ("iss", "aud", "iat", "exp"):
                without_claim = {key: value for key, value in claims.items() if key != missing}
                token = jwt.encode(without_claim, auth.SECRET_KEY, algorithm=auth.ALGORITHM)
                self.assertIsNone(auth.verify_token(token), missing)

            wrong_audience = {**claims, "aud": "aads"}
            self.assertIsNone(auth.verify_token(jwt.encode(
                wrong_audience, auth.SECRET_KEY, algorithm=auth.ALGORITHM,
            )))
            self.assertIsNone(auth.verify_token(jwt.encode(
                claims, "different-signing-key", algorithm=auth.ALGORITHM,
            )))

    def test_aads_rejects_obys_token_and_obys_rejects_legacy_token(self):
        with patch.dict(os.environ, {"OBYS_STANDALONE": "1"}):
            obys_token = auth.create_token("user-1", "user@example.test")
        with patch.dict(os.environ, {"OBYS_STANDALONE": "0"}):
            aads_token = auth.create_token("user-1", "user@example.test")
            self.assertIsNone(auth.verify_token(obys_token))
            self.assertIsNotNone(auth.verify_token(aads_token))
        with patch.dict(os.environ, {"OBYS_STANDALONE": "1"}):
            self.assertIsNone(auth.verify_token(aads_token))


if __name__ == "__main__":
    unittest.main()
